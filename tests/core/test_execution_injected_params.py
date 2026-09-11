"""Model-supplied values for injected params must be dropped, never merged.

Injected params are pruned from the model-facing schema; a value the model
supplies for one is hallucinated or adversarial (e.g. pointing kubernetes
tools at an attacker kubeconfig via ``_instances``/``kubeconfig_path``).
"""

from __future__ import annotations

from typing import Any

from core.tool.execution import _invoke_runtime_tool
from core.llm.types import ToolCall


class _SpyTool:
    injected_params = ("kubeconfig_path", "_instances")
    accepts_runtime_context = False

    def __init__(self) -> None:
        self.seen_kwargs: dict[str, Any] = {}

    def extract_params(self, _sources: dict[str, Any]) -> dict[str, Any]:
        return {"kubeconfig_path": "/etc/opensre/kube/real.kubeconfig", "_instances": []}

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.seen_kwargs = kwargs
        return {"available": True}


def _invoke(tool: _SpyTool, tc_input: dict[str, Any]) -> None:
    _invoke_runtime_tool(
        tool,  # type: ignore[arg-type]
        ToolCall(id="1", name="spy", input=tc_input),
        request=None,  # type: ignore[arg-type]
        tool_sources={},
        resolved_integrations={},
        runtime_resources={},
        hooks=None,  # type: ignore[arg-type]
    )


def test_model_cannot_override_injected_params() -> None:
    tool = _SpyTool()
    _invoke(
        tool,
        {
            "cluster": "gra",
            "kubeconfig_path": "/tmp/attacker.kubeconfig",
            "_instances": [{"name": "gra", "config": {"kubeconfig_path": "/tmp/attacker"}}],
        },
    )
    assert tool.seen_kwargs["kubeconfig_path"] == "/etc/opensre/kube/real.kubeconfig"
    assert tool.seen_kwargs["_instances"] == []  # injected empty list wins over model value
    assert tool.seen_kwargs["cluster"] == "gra"  # non-injected params still flow through


def test_non_injected_model_input_still_merges_over_defaults() -> None:
    tool = _SpyTool()
    _invoke(tool, {"namespace": "prod", "limit": 5})
    assert tool.seen_kwargs["namespace"] == "prod"
    assert tool.seen_kwargs["limit"] == 5
