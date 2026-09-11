"""Tests for the GCP Cloud Logging integration: config model + classifier + client."""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest import mock

import pytest

from integrations.config_models import GcpLoggingIntegrationConfig
from integrations.gcp_logging import classify
from integrations.gcp_logging.client import (
    _REQUEST_TIMEOUT_SECONDS,
    GcpLoggingClient,
    _redact_error,
)

_LOGGING_READ_SCOPE = "https://www.googleapis.com/auth/logging.read"

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


def test_gcp_logging_config_validates_minimal() -> None:
    cfg = GcpLoggingIntegrationConfig.model_validate({"project_id": "my-project"})
    assert cfg.project_id == "my-project"
    assert cfg.credentials_path == ""
    assert cfg.default_filter == ""
    assert cfg.integration_id == ""
    assert cfg.is_configured is True


def test_gcp_logging_config_is_configured_true_iff_project_id_set() -> None:
    assert GcpLoggingIntegrationConfig(project_id="p-dev").is_configured is True
    # Empty / whitespace-only project_id normalizes to "" → not configured.
    assert GcpLoggingIntegrationConfig(project_id="").is_configured is False
    assert GcpLoggingIntegrationConfig(project_id="   ").is_configured is False


def test_gcp_logging_config_credentials_path_optional() -> None:
    # ADC path: no key file supplied.
    cfg = GcpLoggingIntegrationConfig.model_validate({"project_id": "p-prod"})
    assert cfg.credentials_path == ""
    assert cfg.is_configured is True

    # Explicit service-account key file.
    with_key = GcpLoggingIntegrationConfig.model_validate(
        {"project_id": "p-prod", "credentials_path": "/etc/gcp/sa.json"}
    )
    assert with_key.credentials_path == "/etc/gcp/sa.json"
    assert with_key.is_configured is True


def test_gcp_logging_config_normalizes_whitespace() -> None:
    cfg = GcpLoggingIntegrationConfig.model_validate(
        {
            "project_id": "  my-project  ",
            "credentials_path": "  /etc/gcp/sa.json  ",
            "default_filter": '  severity>="ERROR"  ',
            "integration_id": "  int-1  ",
        }
    )
    assert cfg.project_id == "my-project"
    assert cfg.credentials_path == "/etc/gcp/sa.json"
    assert cfg.default_filter == 'severity>="ERROR"'
    assert cfg.integration_id == "int-1"


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


def test_classify_returns_config_and_key() -> None:
    cfg, key = classify({"project_id": "p-dev", "credentials_path": "/etc/gcp/sa.json"}, "rec-1")
    assert key == "gcp_logging"
    assert cfg is not None
    assert cfg.project_id == "p-dev"
    assert cfg.credentials_path == "/etc/gcp/sa.json"
    # record_id flows in as the integration_id.
    assert cfg.integration_id == "rec-1"


def test_classify_picks_named_keys_only_default_filter() -> None:
    cfg, key = classify(
        {"project_id": "p-dev", "default_filter": 'resource.type="cloud_run_revision"'},
        "rec-2",
    )
    assert key == "gcp_logging"
    assert cfg is not None
    assert cfg.default_filter == 'resource.type="cloud_run_revision"'


def test_classify_missing_project_id_warns_and_skips(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="integrations.gcp_logging"):
        cfg, key = classify({"credentials_path": "/etc/gcp/sa.json"}, "rec-3")
    assert cfg is None
    assert key is None
    assert any("rec-3" in rec.message and "project_id" in rec.message for rec in caplog.records)


def test_classify_extra_key_does_not_drop_instance() -> None:
    # An unexpected credential key must be ignored (explicit key-pick), not
    # trip the strict model and silently vanish the instance.
    cfg, key = classify(
        {"project_id": "p-dev", "region": "us-central1", "unexpected": "x"},
        "rec-4",
    )
    assert key == "gcp_logging"
    assert cfg is not None
    assert cfg.project_id == "p-dev"


# ---------------------------------------------------------------------------
# GCP_LOGGING_INSTANCES env → one record with multiple instances
# ---------------------------------------------------------------------------


def test_gcp_logging_instances_env_produces_one_record_two_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations.catalog import load_env_integrations

    monkeypatch.setenv(
        "GCP_LOGGING_INSTANCES",
        json.dumps(
            [
                {"name": "p-dev", "project_id": "p-dev"},
                {"name": "p-prod", "project_id": "p-prod"},
            ]
        ),
    )
    records = [r for r in load_env_integrations() if r.get("service") == "gcp_logging"]
    assert len(records) == 1
    assert [i["name"] for i in records[0]["instances"]] == ["p-dev", "p-prod"]


def test_gcp_logging_survives_effective_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a configured gcp_logging must survive the real
    load_env_integrations -> resolve_effective_integrations pipeline and not be
    dropped by the EffectiveIntegrations model_fields filter. Membership in
    DIRECT_CLASSIFIED_EFFECTIVE_SERVICES alone does not catch this — the entry is
    published then silently filtered out unless the model has a gcp_logging field.
    """
    from integrations.catalog import load_env_integrations, resolve_effective_integrations

    monkeypatch.setenv(
        "GCP_LOGGING_INSTANCES",
        json.dumps([{"name": "p", "project_id": "p"}]),
    )

    effective = resolve_effective_integrations(env_integrations=load_env_integrations())

    assert "gcp_logging" in effective, sorted(effective)


# ---------------------------------------------------------------------------
# GcpLoggingClient — mocked discovery service (no GCP creds needed)
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Stand-in for a googleapiclient request object."""

    def __init__(self, result: object) -> None:
        self._result = result

    def execute(self) -> object:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeEntries:
    """Records ``list`` bodies and replays queued page responses.

    Exposes only ``list`` — matching the sole method the client is allowed to
    call. (The authoritative read-only guarantee is asserted against the client
    *source* in ``test_client_source_only_calls_entries_list``, not against this
    fake, which could not prove anything about the client itself.)
    """

    def __init__(self, pages: list[object]) -> None:
        self._pages = list(pages)
        self.list_bodies: list[dict[str, Any]] = []

    def list(self, *, body: dict[str, Any]) -> _FakeRequest:
        self.list_bodies.append(dict(body))
        return _FakeRequest(self._pages.pop(0))


class _PerpetualEntries:
    """Always returns the same response — models a server that keeps handing
    back a ``nextPageToken`` even with an empty ``entries`` array."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.list_bodies: list[dict[str, Any]] = []

    def list(self, *, body: dict[str, Any]) -> _FakeRequest:
        self.list_bodies.append(dict(body))
        return _FakeRequest(self._response)


class _FakeService:
    def __init__(self, entries: _FakeEntries | _PerpetualEntries) -> None:
        self._entries = entries
        self.closed = False

    def entries(self) -> _FakeEntries | _PerpetualEntries:
        return self._entries

    def close(self) -> None:
        self.closed = True


def _patch_service(
    service: _FakeService,
) -> tuple[mock._patch, mock._patch, _FakeService, mock.Mock]:
    """Patch ``google.auth.default`` + ``googleapiclient.discovery.build``.

    Returns the two patchers (as context managers), the fake service the build
    call yields, and the ``default`` mock so tests can assert on the scopes.
    """
    default_mock = mock.Mock(return_value=("adc-creds", "adc-project"))
    auth_patch = mock.patch("google.auth.default", default_mock)
    build_patch = mock.patch("googleapiclient.discovery.build", return_value=service)
    return auth_patch, build_patch, service, default_mock


def _patch_gcp(pages: list[object]) -> tuple[mock._patch, mock._patch, _FakeService, mock.Mock]:
    return _patch_service(_FakeService(_FakeEntries(pages)))


def test_client_requests_adc_creds_with_logging_read_scope() -> None:
    auth_patch, build_patch, _service, default_mock = _patch_gcp([{"entries": []}])
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch as build_mock:
        client = GcpLoggingClient(cfg)
        client.query(["projects/p-dev"], "")
    # ADC path used (no credentials_path) and the logging.read scope was requested.
    default_mock.assert_called_once_with(scopes=[_LOGGING_READ_SCOPE])
    _, build_kwargs = build_mock.call_args
    assert build_kwargs["cache_discovery"] is False
    # Creds are wrapped in a timeout-configured AuthorizedHttp (bounded request).
    authed_http = build_kwargs["http"]
    assert authed_http.credentials == "adc-creds"
    assert authed_http.http.timeout == _REQUEST_TIMEOUT_SECONDS


def test_client_service_account_path_requests_scope() -> None:
    auth_patch, build_patch, _service, default_mock = _patch_gcp([{"entries": []}])
    sa_creds = object()
    sa_patch = mock.patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=sa_creds,
    )
    cfg = GcpLoggingIntegrationConfig(project_id="p-prod", credentials_path="/etc/gcp/sa.json")
    with auth_patch, build_patch as build_mock, sa_patch as sa_mock:
        GcpLoggingClient(cfg).query(["projects/p-prod"], "")
    # Key-file path taken with the mandatory scope; ADC not consulted.
    sa_mock.assert_called_once_with("/etc/gcp/sa.json", scopes=[_LOGGING_READ_SCOPE])
    default_mock.assert_not_called()
    _, build_kwargs = build_mock.call_args
    assert build_kwargs["http"].credentials is sa_creds


def test_query_multi_project_pagination_and_cap() -> None:
    # Two pages of 3 entries each; max_entries caps the total at 5.
    page1 = {
        "entries": [{"insertId": f"a{i}"} for i in range(3)],
        "nextPageToken": "tok-2",
    }
    page2 = {"entries": [{"insertId": f"b{i}"} for i in range(3)]}
    auth_patch, build_patch, service, _default = _patch_gcp([page1, page2])
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        client = GcpLoggingClient(cfg)
        entries = client.query(
            ["projects/p-dev", "projects/p-prod"],
            'resource.type="cloud_run_revision"',
            page_size=3,
            max_entries=5,
        )
    # Cap enforced across pages.
    assert len(entries) == 5
    assert [e["insertId"] for e in entries] == ["a0", "a1", "a2", "b0", "b1"]
    bodies = service.entries().list_bodies
    # Two pages fetched; multi-project resourceNames forwarded verbatim.
    assert len(bodies) == 2
    assert bodies[0]["resourceNames"] == ["projects/p-dev", "projects/p-prod"]
    assert bodies[0]["filter"] == 'resource.type="cloud_run_revision"'
    assert bodies[0]["orderBy"] == "timestamp desc"
    # Second call carries the pageToken from the first response.
    assert bodies[1]["pageToken"] == "tok-2"


def test_query_stops_when_no_next_page_token() -> None:
    auth_patch, build_patch, service, _default = _patch_gcp([{"entries": [{"insertId": "only"}]}])
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        entries = GcpLoggingClient(cfg).query(["projects/p-dev"], "")
    assert [e["insertId"] for e in entries] == ["only"]
    assert len(service.entries().list_bodies) == 1


def test_probe_access_success_returns_passed_probe_shape() -> None:
    auth_patch, build_patch, service, _default = _patch_gcp([{"entries": [{"insertId": "x"}]}])
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        result = GcpLoggingClient(cfg).probe_access()
    assert result.status == "passed"
    assert result.ok is True
    assert "p-dev" in result.detail
    assert result.metadata["project_id"] == "p-dev"
    # Probe queries its own project with a pageSize-1 recent-window filter.
    body = service.entries().list_bodies[0]
    assert body["resourceNames"] == ["projects/p-dev"]
    assert body["pageSize"] == 1
    assert body["filter"].startswith("timestamp>=")


def test_probe_access_failure_returns_failed_redacted_never_raises() -> None:
    auth_patch, build_patch, _service, _default = _patch_gcp(
        [RuntimeError("boom secret-token=abc123")]
    )
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        result = GcpLoggingClient(cfg).probe_access()
    assert result.status == "failed"
    # Redacted: the raw exception text (which could carry secrets) is not leaked.
    assert "secret-token" not in result.detail
    assert "RuntimeError" in result.detail


def test_probe_access_missing_when_not_configured() -> None:
    result = GcpLoggingClient(GcpLoggingIntegrationConfig(project_id="")).probe_access()
    assert result.status == "missing"


def test_client_source_only_calls_entries_list() -> None:
    # Authoritative read-only guarantee: assert against the client *source*, not a
    # fake. The only Logging method the module ever invokes is ``entries().list``,
    # and no write/admin verb (create/update/patch/delete/write) is called anywhere.
    import inspect
    import re

    from integrations.gcp_logging import client as client_mod

    source = inspect.getsource(client_mod)
    called_methods = set(re.findall(r"\.entries\(\)\.(\w+)\(", source))
    assert called_methods == {"list"}, f"unexpected Logging methods called: {called_methods}"
    assert not re.search(r"\.(create|update|patch|delete|write)\(", source), (
        "a write/admin method call is present in the client source"
    )


def test_query_page_cap_bounds_empty_but_tokened_pages() -> None:
    # Server keeps returning empty entries WITH a perpetual nextPageToken. Without
    # the page cap this loops forever; with it the loop stops at max_pages.
    perpetual = _PerpetualEntries({"entries": [], "nextPageToken": "always"})
    auth_patch, build_patch, service, _default = _patch_service(_FakeService(perpetual))
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        entries = GcpLoggingClient(cfg).query(["projects/p-dev"], "", max_pages=3)
    assert entries == []
    # Bounded: exactly max_pages requests, then returns what it has (no raise).
    assert len(perpetual.list_bodies) == 3


def test_probe_access_bounded_under_perpetual_token() -> None:
    # Same pathological server; probe_access must terminate (not hang) and, since
    # no error was raised, report a bounded success.
    perpetual = _PerpetualEntries({"entries": [], "nextPageToken": "always"})
    auth_patch, build_patch, _service, _default = _patch_service(_FakeService(perpetual))
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        result = GcpLoggingClient(cfg).probe_access()
    assert result.status == "passed"
    # The probe's page fetches are bounded by the default page cap.
    from integrations.gcp_logging.client import _DEFAULT_MAX_PAGES

    assert 0 < len(perpetual.list_bodies) <= _DEFAULT_MAX_PAGES


def test_context_manager_closes_service() -> None:
    auth_patch, build_patch, service, _default = _patch_gcp([{"entries": []}])
    cfg = GcpLoggingIntegrationConfig(project_id="p-dev")
    with auth_patch, build_patch:
        with GcpLoggingClient(cfg) as client:
            client.query(["projects/p-dev"], "")
        assert service.closed is True


def test_redact_error_httperror_hides_body() -> None:
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 403
        reason = "Forbidden"

    err = HttpError(_Resp(), b'{"error": {"message": "leaked project detail"}}')
    detail = _redact_error(err)
    assert "403" in detail
    assert "leaked project detail" not in detail
