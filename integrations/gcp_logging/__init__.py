"""GCP Cloud Logging integration classifier."""

from __future__ import annotations

import logging
from typing import Any

from integrations._validation_helpers import report_classify_failure
from integrations.config_models import GcpLoggingIntegrationConfig

logger = logging.getLogger(__name__)


def classify(
    credentials: dict[str, Any], record_id: str
) -> tuple[GcpLoggingIntegrationConfig | None, str | None]:
    """Build a :class:`GcpLoggingIntegrationConfig` from one instance's credentials.

    Named keys are picked explicitly (never the raw flattened credentials dict)
    so an unexpected extra key is ignored rather than tripping the strict
    ``extra="forbid"`` model and silently dropping the instance. The instance is
    keyed by its GCP ``project_id`` (its ``name`` is the project id); a record
    with no ``project_id`` is warned about and skipped, never vanished silently.
    """
    try:
        cfg = GcpLoggingIntegrationConfig.model_validate(
            {
                "project_id": credentials.get("project_id", ""),
                "credentials_path": credentials.get("credentials_path", ""),
                "default_filter": credentials.get("default_filter", ""),
                "integration_id": record_id,
            }
        )
    except Exception as exc:
        report_classify_failure(exc, logger=logger, integration="gcp_logging", record_id=record_id)
        return None, None
    if cfg.is_configured:
        return cfg, "gcp_logging"
    logger.warning("gcp_logging record %s has no project_id — skipped", record_id)
    return None, None
