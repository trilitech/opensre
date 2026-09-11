"""Tests for the GCP Cloud Logging pure query builders (filter + resourceNames)."""

from __future__ import annotations

import re

from integrations.gcp_logging.query_build import (
    SUPPORTED_RESOURCE_TYPES,
    build_filter,
    resource_names,
)


def _clauses(filter_: str) -> list[str]:
    return filter_.split(" AND ")


def test_resource_names_wraps_single_project() -> None:
    assert resource_names("p-dev") == ["projects/p-dev"]


def test_cloud_run_revision_filter_uses_service_name_label() -> None:
    clauses = _clauses(build_filter("cloud_run_revision", "checkout", "ERROR", 60, ""))
    assert 'resource.type="cloud_run_revision"' in clauses
    assert 'resource.labels.service_name="checkout"' in clauses


def test_gce_instance_filter_uses_instance_id_label() -> None:
    clauses = _clauses(build_filter("gce_instance", "1234567890", "WARNING", 60, ""))
    assert 'resource.type="gce_instance"' in clauses
    assert 'resource.labels.instance_id="1234567890"' in clauses


def test_severity_and_timestamp_floors_present() -> None:
    clauses = _clauses(build_filter("cloud_run_revision", "svc", "ERROR", 30, ""))
    assert 'severity>="ERROR"' in clauses
    # A single timestamp>= floor, quoted as an ISO-8601 Zulu instant.
    ts = [c for c in clauses if c.startswith("timestamp>=")]
    assert len(ts) == 1
    assert re.fullmatch(r'timestamp>="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"', ts[0])


def test_empty_resource_name_omits_label_clause_but_keeps_type() -> None:
    clauses = _clauses(build_filter("cloud_run_revision", "", "ERROR", 60, ""))
    assert 'resource.type="cloud_run_revision"' in clauses
    assert not any(c.startswith("resource.labels.") for c in clauses)


def test_empty_resource_type_omits_type_and_name_clauses() -> None:
    clauses = _clauses(build_filter("", "checkout", "ERROR", 60, ""))
    assert not any(c.startswith("resource.type") for c in clauses)
    assert not any(c.startswith("resource.labels.") for c in clauses)
    # Floors still present.
    assert 'severity>="ERROR"' in clauses
    assert any(c.startswith("timestamp>=") for c in clauses)


def test_empty_severity_omits_severity_clause() -> None:
    clauses = _clauses(build_filter("gce_instance", "i-1", "", 60, ""))
    assert not any(c.startswith("severity") for c in clauses)


def test_extra_filter_is_and_joined_parenthesized() -> None:
    extra = 'jsonPayload.message:"timeout"'
    clauses = _clauses(build_filter("cloud_run_revision", "svc", "ERROR", 60, extra))
    # Extra clause is appended last (after the timestamp floor), wrapped in parens
    # so a top-level OR cannot escape the AND-bound floors.
    assert clauses[-1] == f"({extra})"


def test_empty_extra_filter_omits_its_clause() -> None:
    filter_ = build_filter("cloud_run_revision", "svc", "ERROR", 60, "")
    assert not filter_.endswith(" AND ")
    assert "  " not in filter_


def test_string_values_are_quote_and_backslash_escaped() -> None:
    clauses = _clauses(build_filter("cloud_run_revision", 'sv"c\\x', "ERROR", 60, ""))
    # Embedded double-quote and backslash are both escaped inside the quoted value.
    assert 'resource.labels.service_name="sv\\"c\\\\x"' in clauses


def test_all_clauses_and_joined_in_order() -> None:
    filter_ = build_filter("cloud_run_revision", "svc", "ERROR", 60, "logName:stdout")
    clauses = _clauses(filter_)
    assert clauses[0] == 'resource.type="cloud_run_revision"'
    assert clauses[1] == 'resource.labels.service_name="svc"'
    assert clauses[2] == 'severity>="ERROR"'
    assert clauses[3].startswith("timestamp>=")
    assert clauses[4] == "(logName:stdout)"


def test_extra_filter_with_top_level_or_is_parenthesized() -> None:
    # A top-level OR in the extra clause must be wrapped so it cannot escape the
    # AND-bound floors (Cloud Logging binds AND tighter than OR).
    extra = 'a="1" OR severity>=DEFAULT'
    filter_ = build_filter("cloud_run_revision", "svc", "ERROR", 60, extra)
    assert f"({extra})" in filter_
    clauses = _clauses(filter_)
    # Floors remain top-level AND terms; the wrapped OR is its own final term.
    assert 'severity>="ERROR"' in clauses
    assert any(c.startswith("timestamp>=") for c in clauses)
    assert clauses[-1] == f"({extra})"


def test_supported_resource_types_is_public_and_matches_enum() -> None:
    assert SUPPORTED_RESOURCE_TYPES == ["cloud_run_revision", "gce_instance"]
