"""GCP Cloud Logging investigation tool — read-only ``entries.list`` backed."""

from __future__ import annotations

from typing import Any

from core.tool import BaseTool
from core.tool_framework.utils.tool_availability import tool_unavailable
from integrations.config_models import GcpLoggingIntegrationConfig
from integrations.gcp_logging.client import GcpLoggingClient
from integrations.gcp_logging.query_build import (
    SUPPORTED_RESOURCE_TYPES,
    build_filter,
    resource_names,
)
from integrations.selectors import get_instance_by_name, get_instances

_INSTANCES_KEY = "_all_gcp_logging_instances"
_RESOURCE_TYPE_ENUM: list[str] = SUPPORTED_RESOURCE_TYPES


def _resolve_project(
    instances_view: dict[str, Any], project: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the target instance config by ``project`` (== project id, K1).

    Returns ``(config_dict, error)``. Never silently defaults across projects:
    with >1 configured instance and a missing or unknown ``project`` it returns
    an error naming the configured project ids — wrong-project logs poison a
    diagnosis. A single configured instance resolves without a ``project``.
    """
    # get_instances always synthesizes at least a default entry from the flat
    # gcp_logging config (present in the view even when empty), so an empty /
    # unconfigured project is not caught here — it falls through to the single-
    # instance branch and is rejected downstream by cfg.is_configured.
    entries = get_instances(instances_view, "gcp_logging")
    names = ", ".join(str(e.get("name", "?")) for e in entries)
    if project:
        conn = get_instance_by_name(instances_view, "gcp_logging", project)
        if conn is None:
            return None, f"unknown project '{project}' — configured projects: {names}"
        return conn, None
    if len(entries) == 1:
        return get_instance_by_name(instances_view, "gcp_logging", str(entries[0]["name"])), None
    return None, f"multiple GCP projects configured — pass 'project' as one of: {names}"


class GcpLoggingQueryTool(BaseTool):
    """Read Cloud Run / Compute VM logs from a GCP project (read-only)."""

    name = "gcp_logging_query"
    source = "gcp_logging"
    description = (
        "Read recent log entries from Google Cloud Logging for a Cloud Run service "
        "or a Compute Engine VM in a GCP project. Read-only (entries.list only). "
        "Use to inspect application errors, crashes, and request failures in GCP."
    )
    use_cases = [
        "Reading error/warning logs for a Cloud Run service named in a GCP alert",
        "Inspecting logs for a specific Compute Engine VM instance",
        "Narrowing to a time window and severity floor to find failure evidence",
    ]
    surfaces = ("action", "chat")
    requires = []
    injected_params = ["_instances", "_flat_config"]
    input_schema = {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "default": "",
                "description": (
                    "GCP project id to query (use the project named in the alert). "
                    "Required when multiple projects are configured — the error lists "
                    "valid ids. Omit only for a single-project setup."
                ),
            },
            "resource_type": {
                "type": "string",
                "enum": _RESOURCE_TYPE_ENUM,
                "default": "cloud_run_revision",
                "description": "GCP monitored-resource type to scope the query to.",
            },
            "resource_name": {
                "type": "string",
                "default": "",
                "description": (
                    "Name of the target: the Cloud Run service name for "
                    "cloud_run_revision, or the instance id for gce_instance. "
                    "Omit to query all resources of the given type."
                ),
            },
            "since_minutes": {
                "type": "integer",
                "default": 60,
                "description": "How far back to look, in minutes (timestamp floor).",
            },
            "severity": {
                "type": "string",
                "enum": ["DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"],
                "default": "WARNING",
                "description": "Minimum log severity to include (severity>= floor).",
            },
            "filter": {
                "type": "string",
                "default": "",
                "description": "Optional extra Cloud Logging advanced-filter clause, AND-joined.",
            },
            "limit": {
                "type": "integer",
                "default": 50,
                "description": "Maximum number of log entries to return.",
            },
            "_instances": {
                "type": "array",
                "default": [],
                "description": "Injected: configured gcp_logging instances (not model-visible)",
            },
            "_flat_config": {
                "type": "object",
                "default": {},
                "description": "Injected: default instance config (not model-visible)",
            },
        },
        "required": [],
    }
    outputs = {
        "entries": "List of Cloud Logging entries (most recent first)",
        "total": "Number of entries returned",
    }

    def is_available(self, sources: dict[str, Any]) -> bool:
        return bool(sources.get("gcp_logging") or sources.get(_INSTANCES_KEY))

    def extract_params(self, sources: dict[str, Any]) -> dict[str, Any]:
        return {
            "project": "",
            "resource_type": "cloud_run_revision",
            "resource_name": "",
            "since_minutes": 60,
            "severity": "WARNING",
            "filter": "",
            "limit": 50,
            "_instances": sources.get(_INSTANCES_KEY, []),
            "_flat_config": sources.get("gcp_logging", {}),
        }

    def run(
        self,
        project: str = "",
        resource_type: str = "cloud_run_revision",
        resource_name: str = "",
        since_minutes: int = 60,
        severity: str = "WARNING",
        filter: str = "",  # noqa: A002 — model-visible param name is part of the tool contract
        limit: int = 50,
        _instances: list[dict[str, Any]] | None = None,
        _flat_config: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        instances_view: dict[str, Any] = {"gcp_logging": _flat_config or {}}
        if _instances:
            instances_view[_INSTANCES_KEY] = _instances
        conn, err = _resolve_project(instances_view, str(project or "").strip())
        if conn is None:
            return tool_unavailable("gcp_logging", err or "unresolved project", entries=[], total=0)
        try:
            cfg = GcpLoggingIntegrationConfig.model_validate(
                {
                    "project_id": conn.get("project_id", ""),
                    "credentials_path": conn.get("credentials_path", ""),
                    "default_filter": conn.get("default_filter", ""),
                }
            )
        except Exception:
            return tool_unavailable(
                "gcp_logging", "GCP Logging instance config is invalid.", entries=[], total=0
            )
        if not cfg.is_configured:
            return tool_unavailable(
                "gcp_logging",
                "GCP Logging integration is not configured (no project).",
                entries=[],
                total=0,
            )
        # Parenthesize each fragment: AND binds tighter than OR in Cloud Logging,
        # so an un-wrapped top-level OR in either the operator default_filter or
        # the model filter would otherwise escape the other (and the floors).
        extra = " AND ".join(f"({c})" for c in (cfg.default_filter, filter) if c)
        query_filter = build_filter(resource_type, resource_name, severity, since_minutes, extra)
        try:
            with GcpLoggingClient(cfg) as client:
                results = client.query(
                    resource_names(cfg.project_id), query_filter, max_entries=limit
                )
        except Exception as exc:
            return tool_unavailable(
                "gcp_logging",
                f"Cloud Logging query failed ({type(exc).__name__}).",
                entries=[],
                total=0,
            )
        return {
            "source": "gcp_logging",
            "available": True,
            "project": cfg.project_id,
            "resource_type": resource_type,
            "filter": query_filter,
            "entries": results,
            "total": len(results),
        }


gcp_logging_query = GcpLoggingQueryTool()
