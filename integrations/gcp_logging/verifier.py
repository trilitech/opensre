"""GCP Cloud Logging integration verifier.

Registered with the central plugin registry at import time. The loader
at ``integrations/_verifiers_loader.py`` is the single place that
imports this module to trigger the registration.
"""

from __future__ import annotations

from integrations.config_models import GcpLoggingIntegrationConfig
from integrations.gcp_logging.client import GcpLoggingClient
from integrations.verification import register_probe_verifier

verify_gcp_logging = register_probe_verifier(
    "gcp_logging",
    config=GcpLoggingIntegrationConfig.model_validate,
    client=GcpLoggingClient,
)
