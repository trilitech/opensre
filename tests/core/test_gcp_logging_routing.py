"""Routing/relevance tests for the gcp_logging integration.

PD-intake GCP alerts don't carry ``alert_source == "gcp_logging"``, so routing
is driven by relevance via ``SOURCE_ALIASES`` (keyword match against alert text),
not by an ``ALERT_SOURCE_ROUTING`` seed entry. These tests exercise the real
``relevant_sources_for_alert`` accessor (no mocking).
"""

from __future__ import annotations

import pytest

from core.domain.alerts.alert_source import (
    alert_source_routing,
    relevant_sources_for_alert,
    source_aliases,
)

CANDIDATE_SOURCES = ("gcp_logging", "datadog", "knowledge")


@pytest.fixture(autouse=True)
def _adapters() -> None:
    """Rebuild the adapters the way startup does, so aliases/routing register."""
    import integrations.harness_adapters as harness_adapters

    harness_adapters.register_harness_adapters()


@pytest.mark.parametrize(
    "message",
    [
        "Elevated 5xx on the checkout cloud run service",
        "resource.type=cloud_run_revision error spike in prod",
        "gce_instance web-prod-01 in gcp is throwing errors",
    ],
)
def test_gcp_signals_make_gcp_logging_relevant(message: str) -> None:
    matched = relevant_sources_for_alert({"message": message}, CANDIDATE_SOURCES)
    assert "gcp_logging" in matched


def test_pure_k8s_alert_does_not_surface_gcp_logging() -> None:
    # Aliases stay precise: a plain Kubernetes/GKE alert with no GCP-logging
    # signal must NOT make gcp_logging relevant (the tool can't serve it).
    candidates = (*CANDIDATE_SOURCES, "kubernetes")
    matched = relevant_sources_for_alert(
        {"message": "CrashLoopBackOff pod prod kubectl OOMKilled"}, candidates
    )
    assert "gcp_logging" not in matched


def test_no_alert_source_routing_seed_entry_for_gcp_logging() -> None:
    # ALERT_SOURCE_ROUTING keys off the payload alert_source, which PD-intake
    # GCP alerts won't carry as "gcp_logging" — a seed entry would never fire.
    # Relevance via SOURCE_ALIASES is the correct mechanism.
    assert "gcp_logging" not in alert_source_routing()
    assert "gcp_logging" in source_aliases()
