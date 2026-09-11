"""Pure Cloud Logging advanced-filter + ``resourceNames`` builders (no I/O).

Split out from the tool class so the tool stays thin (K-LOC): these are the only
places that shape a Cloud Logging ``entries.list`` query, and they touch nothing
but their arguments — no clients, no network, no clock beyond ``datetime.now``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# The monitored-resource label that names the target, per resource type. These
# are the real GCP monitored-resource label keys: ``cloud_run_revision`` carries
# ``resource.labels.service_name``; ``gce_instance`` carries
# ``resource.labels.instance_id``. An unknown resource type has no name label, so
# the resource-name clause is simply omitted (resource.type still constrains it).
_RESOURCE_NAME_LABEL = {
    "cloud_run_revision": "service_name",
    "gce_instance": "instance_id",
}

# Public: the resource types the tool's enum offers (derived from the private
# label map so the two can never drift). Imported by the tool module.
SUPPORTED_RESOURCE_TYPES: list[str] = sorted(_RESOURCE_NAME_LABEL)

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def resource_names(project_id: str) -> list[str]:
    """Cloud Logging ``resourceNames`` for a single project."""
    return [f"projects/{project_id}"]


def _quote(value: str) -> str:
    """Double-quote a filter string value (escape backslashes then quotes)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_filter(
    resource_type: str,
    resource_name: str,
    severity: str,
    since_minutes: int,
    extra_filter: str,
) -> str:
    """AND-join a valid Cloud Logging advanced filter from the given clauses.

    Empty clauses are skipped cleanly: an empty ``resource_type`` omits both the
    type and the resource-name clause; an empty ``resource_name`` omits only the
    name clause; an empty ``severity`` / ``extra_filter`` omits its own clause.
    The ``timestamp>=`` floor (``now - since_minutes``, UTC) is always present.
    String values are double-quoted with embedded quotes/backslashes escaped.
    """
    clauses: list[str] = []
    if resource_type:
        clauses.append(f"resource.type={_quote(resource_type)}")
        label = _RESOURCE_NAME_LABEL.get(resource_type)
        if label and resource_name:
            clauses.append(f"resource.labels.{label}={_quote(resource_name)}")
    if severity:
        clauses.append(f"severity>={_quote(severity)}")
    since = datetime.now(UTC) - timedelta(minutes=since_minutes)
    clauses.append(f"timestamp>={_quote(since.strftime(_TIMESTAMP_FORMAT))}")
    if extra_filter:
        # Parenthesize: Cloud Logging binds AND tighter than OR, so an
        # unwrapped top-level OR in the extra clause would escape the
        # timestamp/severity/resource floors above.
        clauses.append(f"({extra_filter})")
    return " AND ".join(clauses)
