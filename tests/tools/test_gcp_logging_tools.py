"""Tests for the GCP Cloud Logging investigation tool (gcp_logging_query).

The ``GcpLoggingClient`` is always mocked (patched at
``integrations.gcp_logging.tools.GcpLoggingClient``) — no GCP creds or network.
Covers: happy path, the no-silent-cross-project-default guard, the read-only
contract, model-visibility (injected params pruned), extract_params wiring, and
the single-instance flat-config fallback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from integrations.gcp_logging.client import GcpLoggingClient
from integrations.gcp_logging.tools import gcp_logging_query
from tools.registry import get_registered_tools

_PATCH_TARGET = "integrations.gcp_logging.tools.GcpLoggingClient"


def _instance(project_id: str, *, credentials_path: str = "", default_filter: str = "") -> dict:
    """One ``_all_gcp_logging_instances`` entry (name == project id, K1)."""
    return {
        "name": project_id,
        "tags": {},
        "config": {
            "project_id": project_id,
            "credentials_path": credentials_path,
            "default_filter": default_filter,
        },
        "integration_id": project_id,
    }


def _mock_client_class(query_return: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    """Patched ``GcpLoggingClient`` class + the context-managed client instance.

    The instance is ``spec``-restricted to the real client's attributes so that
    touching any method other than ``query``/``probe_access``/context-manager
    raises — a structural read-only guarantee, not just a call assertion.
    """
    client = MagicMock(spec=GcpLoggingClient)
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.query.return_value = query_return
    cls = MagicMock(return_value=client)
    return cls, client


# ---------------------------------------------------------------------------
# (a) happy path
# ---------------------------------------------------------------------------


def test_run_happy_path_returns_entries_for_resolved_project() -> None:
    cls, client = _mock_client_class([{"insertId": "e1"}, {"insertId": "e2"}])
    with patch(_PATCH_TARGET, cls):
        result = gcp_logging_query.run(
            project="p-dev",
            resource_type="cloud_run_revision",
            resource_name="checkout",
            _instances=[_instance("p-dev")],
            _flat_config={},
        )
    assert result["available"] is True
    assert result["source"] == "gcp_logging"
    assert result["project"] == "p-dev"
    assert result["entries"] == [{"insertId": "e1"}, {"insertId": "e2"}]
    assert result["total"] == 2
    # query() called with resource_names(project_id) + a filter naming the resource.
    args, kwargs = client.query.call_args
    assert args[0] == ["projects/p-dev"]
    assert 'resource.type="cloud_run_revision"' in args[1]
    assert 'resource.labels.service_name="checkout"' in args[1]
    assert kwargs["max_entries"] == 50


# ---------------------------------------------------------------------------
# (b) no silent cross-project default
# ---------------------------------------------------------------------------


def test_multiple_instances_no_project_errors_listing_both_ids() -> None:
    cls, client = _mock_client_class([])
    with patch(_PATCH_TARGET, cls):
        result = gcp_logging_query.run(
            project="",
            _instances=[_instance("p-dev"), _instance("p-prod")],
            _flat_config={},
        )
    assert result["available"] is False
    assert "p-dev" in result["error"] and "p-prod" in result["error"]
    # Never queried — no silent cross-project default.
    client.query.assert_not_called()


def test_unknown_project_errors_naming_it_and_never_queries() -> None:
    cls, client = _mock_client_class([])
    with patch(_PATCH_TARGET, cls):
        result = gcp_logging_query.run(
            project="p-nope",
            _instances=[_instance("p-dev"), _instance("p-prod")],
            _flat_config={},
        )
    assert result["available"] is False
    assert "p-nope" in result["error"]
    client.query.assert_not_called()


def test_empty_flat_config_returns_unavailable_not_configured() -> None:
    # Empty/unconfigured project: get_instances synthesizes a default entry, so
    # resolution succeeds but cfg.is_configured is False → not-configured error.
    cls, client = _mock_client_class([])
    with patch(_PATCH_TARGET, cls):
        result = gcp_logging_query.run(project="", _instances=[], _flat_config={})
    assert result["available"] is False
    assert "not configured" in result["error"]
    client.query.assert_not_called()


# ---------------------------------------------------------------------------
# (c) read-only contract
# ---------------------------------------------------------------------------


def test_read_only_contract_only_query_called() -> None:
    cls, client = _mock_client_class([{"insertId": "x"}])
    with patch(_PATCH_TARGET, cls):
        gcp_logging_query.run(project="p-dev", _instances=[_instance("p-dev")], _flat_config={})
    # Exactly one Logging read call; no write/admin verb exists on the spec'd mock,
    # so any such access in the tool would have raised AttributeError above.
    client.query.assert_called_once()
    assert not client.method_calls or {c[0] for c in client.method_calls} <= {
        "__enter__",
        "__exit__",
        "query",
    }


def test_tool_source_has_no_write_or_admin_verb() -> None:
    import inspect
    import re

    from integrations.gcp_logging import tools as tools_mod

    source = inspect.getsource(tools_mod)
    assert not re.search(r"\.(create|update|patch|delete|write|insert)\(", source), (
        "a write/admin method call is present in the gcp_logging tool source"
    )
    # The only client method the tool invokes is query().
    called = set(re.findall(r"client\.(\w+)\(", source))
    assert called <= {"query"}, f"unexpected client methods called: {called}"


# ---------------------------------------------------------------------------
# (d) model-visibility — injected params pruned from the public schema
# ---------------------------------------------------------------------------


def _registered_public_schema_props() -> dict[str, Any]:
    rt = {t.name: t for t in get_registered_tools()}["gcp_logging_query"]
    return rt.public_input_schema.get("properties", {})


def test_model_visible_params_present_injected_pruned() -> None:
    props = _registered_public_schema_props()
    for visible in ("project", "resource_type", "severity"):
        assert visible in props, f"{visible} should be model-visible"
    # Injected params are pruned from the model-facing schema.
    assert "_instances" not in props
    assert "_flat_config" not in props


# ---------------------------------------------------------------------------
# (e) extract_params wiring
# ---------------------------------------------------------------------------


def test_extract_params_carries_instances_and_flat_config() -> None:
    instances = [_instance("p-dev"), _instance("p-prod")]
    flat = {"project_id": "p-dev"}
    sources = {"gcp_logging": flat, "_all_gcp_logging_instances": instances}
    params = gcp_logging_query.extract_params(sources)
    assert params["_instances"] == instances
    assert params["_flat_config"] == flat
    assert params["project"] == ""


# ---------------------------------------------------------------------------
# (f) single-instance flat fallback (no _all_ key)
# ---------------------------------------------------------------------------


def test_single_instance_flat_fallback_resolves_without_project() -> None:
    cls, client = _mock_client_class([{"insertId": "f1"}])
    # Only the flat source is set; no _all_gcp_logging_instances key at all.
    flat = {"project_id": "solo-project", "credentials_path": "", "default_filter": ""}
    with patch(_PATCH_TARGET, cls):
        result = gcp_logging_query.run(
            project="",
            resource_type="gce_instance",
            resource_name="1234567890",
            _instances=[],
            _flat_config=flat,
        )
    assert result["available"] is True
    assert result["project"] == "solo-project"
    args, _kwargs = client.query.call_args
    assert args[0] == ["projects/solo-project"]
    assert 'resource.labels.instance_id="1234567890"' in args[1]
