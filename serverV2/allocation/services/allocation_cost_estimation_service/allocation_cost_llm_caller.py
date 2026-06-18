"""Asks the LLM for a per-scene render-time formula.

Owns the system prompt + tool-use schema + anchor few-shots.  Calls
``AllocationLLMClient.chat`` with a forced tool-use schema so the
response is structured JSON we can parse straight into
``AllocationCostFileFormula``.

Provider + model are read from the live Firestore config on EACH
call so admin-UI edits take effect without a redeploy.  Raises
``LLMException`` on any provider error or schema violation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from serverV2.allocation.allocation_llm_client import AllocationLLMClient
from serverV2.allocation.allocation_cost_estimation_config_repository import (
    AllocationCostEstimationConfigRepository,
)
from serverV2.allocation.services.allocation_cost_estimation_service.allocation_cost_file_formula import (
    ANCHOR_GPU,
    ANCHOR_RESOLUTION_PCT,
    ANCHOR_SAMPLES,
    AllocationCostFileFormula,
)
from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_exception import LLMException


_ANCHORS_FILE = (
    Path(__file__).parent / "allocation_cost_anchor_scenes.json"
)

_TOOL_NAME = "emit_render_cost_formula"

_FORMULA_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": (
        "Emit the rendering-time formula for the given Blender scene. "
        "The base value is seconds-per-frame measured at the anchor "
        "settings; the exponents describe how the time scales with "
        "samples and resolution percentage; denoise_overhead is the "
        "fixed cost added when denoising is enabled."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "base_seconds_per_frame_at_anchor": {
                "type": "number",
                "description": (
                    f"Seconds-per-frame at {ANCHOR_SAMPLES} samples, "
                    f"{ANCHOR_RESOLUTION_PCT}% resolution, on a "
                    f"{ANCHOR_GPU}.  Strictly positive."
                ),
            },
            "samples_exponent": {
                "type": "number",
                "description": (
                    "Exponent for samples scaling.  1.0 = linear "
                    "(typical for Cycles); 0.4-0.7 for EEVEE."
                ),
            },
            "resolution_exponent": {
                "type": "number",
                "description": (
                    "Exponent for resolution percentage scaling.  "
                    "2.0 = quadratic in linear dimension (typical)."
                ),
            },
            "denoise_overhead_seconds": {
                "type": "number",
                "description": "Fixed extra seconds per frame when denoising is on.",
            },
            "engine": {
                "type": "string",
                "enum": [
                    "CYCLES",
                    "BLENDER_EEVEE",
                    "BLENDER_EEVEE_NEXT",
                    "BLENDER_WORKBENCH",
                ],
            },
            "notes": {
                "type": "string",
                "description": (
                    "One-line rationale for what drives this scene's "
                    "cost (volumetrics, SSS, dense meshes, etc.).  "
                    "Audit only; not used in the math."
                ),
            },
        },
        "required": [
            "base_seconds_per_frame_at_anchor",
            "samples_exponent",
            "resolution_exponent",
            "denoise_overhead_seconds",
            "engine",
            "notes",
        ],
    },
}


class AllocationCostLLMCaller:

    def __init__(
        self,
        *,
        client: AllocationLLMClient,
        config_repo: AllocationCostEstimationConfigRepository,
    ) -> None:
        self._client = client
        self._config_repo = config_repo
        self._system_prompt = _build_system_prompt(_load_anchors())

    def call(self, heaviness: dict[str, Any]) -> AllocationCostFileFormula:
        cfg_llm = self._config_repo.get()
        # 4096 fits reasoning models' internal thinking budget (o1 /
        # o3-mini) before the tool call.  gpt-4o family only uses
        # ~200 tokens for the tool-use response so the higher ceiling
        # is harmless on those.
        request = LLMChatRequest(
            model=cfg_llm.model,
            max_tokens=4096,
            system=self._system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": _format_heaviness_for_user_turn(heaviness),
                },
            ],
            tools=[_FORMULA_TOOL],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
        )
        response = self._client.chat(cfg_llm.provider_name, request)
        tool_use = response.tool_use(_TOOL_NAME)
        if tool_use is None:
            raise LLMException(
                f"LLM did not invoke required tool {_TOOL_NAME!r} "
                f"(stop_reason={response.stop_reason!r})"
            )
        raw = tool_use["input"]
        return AllocationCostFileFormula(
            base_seconds_per_frame_at_anchor=float(
                raw["base_seconds_per_frame_at_anchor"],
            ),
            samples_exponent=float(raw["samples_exponent"]),
            resolution_exponent=float(raw["resolution_exponent"]),
            denoise_overhead_seconds=float(raw["denoise_overhead_seconds"]),
            engine=str(raw["engine"]),
            notes=str(raw.get("notes", "")),
            llm_provider=cfg_llm.provider_name,
            llm_model=cfg_llm.model,
        )


def _load_anchors() -> list[dict[str, Any]]:
    raw = json.loads(_ANCHORS_FILE.read_text(encoding="utf-8"))
    return list(raw.get("anchors", []))


def _build_system_prompt(anchors: list[dict[str, Any]]) -> str:
    parts: list[str] = [
        "You are a Blender render-time estimator.  Your job is to look "
        "at a scene's structural heaviness fields (polygon count, "
        "materials, lights, volumetrics presence, shader complexity, "
        "engine, etc.) and emit a parametric formula describing how "
        "long one frame takes to render on a reference GPU.",
        "",
        "You MUST respond by calling the "
        f"`{_TOOL_NAME}` tool with structured arguments.  Do not write "
        "free-form text.  Do not chain multiple tool calls.",
        "",
        "Calibration anchors:",
        f"  ANCHOR_SAMPLES        = {ANCHOR_SAMPLES}",
        f"  ANCHOR_RESOLUTION_PCT = {ANCHOR_RESOLUTION_PCT}",
        f"  ANCHOR_GPU            = {ANCHOR_GPU!r}",
        "",
        "`base_seconds_per_frame_at_anchor` is the number of seconds "
        "ONE frame of this scene would take to render on the anchor "
        "GPU at the anchor sample count and resolution percentage.",
        "",
        "The downstream system applies the formula as:",
        "  scaled = base",
        "        * (user_samples / ANCHOR_SAMPLES) ** samples_exponent",
        "        * (user_res_pct / ANCHOR_RESOLUTION_PCT) ** resolution_exponent",
        "  + denoise_overhead_seconds if denoising is on",
        "",
        "Typical exponents:",
        "  - Cycles samples_exponent: 0.9 - 1.0 (close to linear)",
        "  - EEVEE samples_exponent:  0.4 - 0.7",
        "  - resolution_exponent:     1.8 - 2.0 (quadratic in linear dim)",
    ]
    if anchors:
        parts.extend([
            "",
            "Reference anchor scenes (real telemetry, normalised to "
            "the anchor GPU).  Each entry has:",
            "  - ``heaviness``: the same dict shape you will receive.",
            "  - ``measurement.samples`` / ``resolution_percentage``: "
            "tunable settings the chunk actually rendered at.",
            "  - ``measurement.observed_on_gpu``: which GPU the chunk "
            "ran on.",
            "  - ``measurement.raw_seconds_per_frame_on_observed_gpu``: "
            "raw observed seconds-per-frame on that GPU.",
            "  - ``measurement.seconds_per_frame_at_anchor_gpu``: the "
            "SAME observation scaled to the anchor GPU "
            f"({ANCHOR_GPU!r}) by multiplying by that GPU's "
            "render-speed ratio.  USE THIS VALUE when calibrating "
            "your ``base_seconds_per_frame_at_anchor`` -- everything "
            "is already on the same scale.",
            "",
            json.dumps(anchors, indent=2),
        ])
    else:
        parts.extend([
            "",
            "(No reference anchor scenes provided yet.  Use your "
            "general Blender knowledge to estimate; future calibration "
            "anchors will be added here.)",
        ])
    return "\n".join(parts)


def _format_heaviness_for_user_turn(heaviness: dict[str, Any]) -> str:
    return (
        "Emit the render-cost formula for this scene.  Heaviness fields:\n\n"
        + json.dumps(heaviness, indent=2, sort_keys=True, default=str)
    )
