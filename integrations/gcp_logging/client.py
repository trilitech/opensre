"""GCP Cloud Logging read-only client.

Reads Cloud Run / Compute VM logs across one or more GCP projects via the REST
Logging v2 ``entries.list`` method (``google-api-python-client`` — no gRPC
dependency). Strictly read-only: ``entries.list`` is the only method ever
called; no write/admin Logging method is reachable from here.

Auth is credential-mechanism-agnostic: Application Default Credentials
(``google.auth.default``) when ``credentials_path`` is empty, otherwise the
named service-account key file. Credentials always carry the ``logging.read``
scope — unscoped service-account credentials 403 on ``entries.list``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import google.auth
import google_auth_httplib2  # type: ignore[import-untyped]  # no py.typed marker/stubs
import httplib2  # type: ignore[import-untyped]  # stubs not installed in this env
from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError

from integrations.config_models import GcpLoggingIntegrationConfig
from integrations.probes import ProbeResult
from infrastructure.observability.errors.service import capture_service_error

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/logging.read"]
_DEFAULT_PAGE_SIZE = 50
_DEFAULT_MAX_ENTRIES = 50
# Hard bound on entries.list pages per query. Cloud Logging can return an empty
# ``entries`` array WITH a non-empty ``nextPageToken`` (server ran out of scan
# budget mid-window), so "stop on empty token or enough entries" is not enough —
# a sparse filter over a large window would page forever. This caps the loop.
_DEFAULT_MAX_PAGES = 10
# Per-request socket timeout (seconds) so a stalled endpoint can't block the
# worker indefinitely; combined with the page cap this bounds total query time.
_REQUEST_TIMEOUT_SECONDS = 30
_PROBE_WINDOW = timedelta(hours=1)


class GcpLoggingClient:
    """Read-only Cloud Logging client — ``entries.list`` only."""

    def __init__(self, config: GcpLoggingIntegrationConfig) -> None:
        self.config = config
        self._service: Any = None

    def _build_service(self) -> Any:
        if self.config.credentials_path:
            creds = service_account.Credentials.from_service_account_file(
                self.config.credentials_path, scopes=_SCOPES
            )
        else:
            creds, _ = google.auth.default(scopes=_SCOPES)
        # Wrap the credentials in an AuthorizedHttp backed by a timeout-configured
        # httplib2.Http so every request has a bounded socket timeout. (Passing
        # ``credentials=`` to build() would use a default Http with no timeout.)
        authed_http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(timeout=_REQUEST_TIMEOUT_SECONDS)
        )
        return discovery.build("logging", "v2", http=authed_http, cache_discovery=False)

    def _get_service(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def close(self) -> None:
        """Close the underlying discovery service connection pool."""
        if self._service is not None:
            self._service.close()
            self._service = None

    def __enter__(self) -> GcpLoggingClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def is_configured(self) -> bool:
        return self.config.is_configured

    def query(
        self,
        resource_names: list[str],
        filter_: str,
        page_size: int = _DEFAULT_PAGE_SIZE,
        order_by: str = "timestamp desc",
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_pages: int = _DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """Read log entries across one or more projects (``entries.list`` only).

        ``resource_names`` is a list of ``projects/{id}`` — native multi-project
        fan-out. Pages via ``nextPageToken`` until ``max_entries`` are collected,
        the server runs out of pages, or ``max_pages`` requests have been made
        (the hard bound that stops empty-but-tokened pages looping forever), then
        caps the total at ``max_entries``. Returns whatever was collected when the
        page cap is hit — never raises on the cap.
        """
        service = self._get_service()
        entries: list[dict[str, Any]] = []
        body: dict[str, Any] = {
            "resourceNames": resource_names,
            "orderBy": order_by,
            "pageSize": min(page_size, max_entries),
        }
        if filter_:
            body["filter"] = filter_
        for _ in range(max_pages):
            response = service.entries().list(body=body).execute()
            entries.extend(response.get("entries") or [])
            page_token = response.get("nextPageToken") or ""
            if len(entries) >= max_entries or not page_token:
                break
            body["pageToken"] = page_token
        return entries[:max_entries]

    def probe_access(self) -> ProbeResult:
        """Confirm read access with a tiny live ``entries.list`` (pageSize 1).

        Queries the config's own project over a recent time window so the probe
        stays cheap. Errors are captured server-side and returned as a redacted
        ``failed`` result — never raised.
        """
        if not self.is_configured:
            return ProbeResult.missing("Missing project_id.")
        since = (datetime.now(UTC) - _PROBE_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with self:
                self.query(
                    resource_names=[f"projects/{self.config.project_id}"],
                    filter_=f'timestamp>="{since}"',
                    page_size=1,
                    max_entries=1,
                )
        except Exception as exc:
            capture_service_error(
                exc, logger=logger, integration="gcp_logging", method="probe_access"
            )
            return ProbeResult.failed(_redact_error(exc))
        return ProbeResult.passed(
            f"Connected to Cloud Logging; project '{self.config.project_id}' readable.",
            project_id=self.config.project_id,
        )


def _redact_error(exc: Exception) -> str:
    """Generic, secret-free detail for an external verifier surface (CWE-209)."""
    if isinstance(exc, HttpError):
        status = getattr(exc, "status_code", None) or getattr(exc.resp, "status", "?")
        return f"Cloud Logging API error (HTTP {status})."
    return f"Cloud Logging access failed ({type(exc).__name__})."
