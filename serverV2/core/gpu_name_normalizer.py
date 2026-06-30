"""GpuNameNormalizer -- collapse raw GPU name strings to a canonical form.

Different sources spell the same GPU very differently:

    "NVIDIA GeForce RTX 4090"    (Blender / nvidia-smi)
    "RTX 4090"                   (Vast 'gpu_name' field)
    "RTX_4090_24G"               (Vast 'gpu_label' field)
    "GeForce RTX 4090"           (some driver strings)
    "NVIDIA RTX 4090"

All five should normalize to ``RTX_4090`` so historical telemetry
groups cleanly by ``(fleet, gpu_model_normalized)`` for future
estimation work.  Pure lookup + regex; no side effects.

Normalization rules (applied in order):
    1. Strip vendor prefixes (``NVIDIA``, ``AMD``, ``Intel``).
    2. Strip family prefixes (``GeForce``, ``Quadro``, ``Tesla``,
       ``Radeon``, ``Instinct``).
    3. Strip VRAM suffixes (``24G``, ``48GB``, ``80G``) and version
       suffixes (``NVL``, ``SXM4``, ``PCIE``) -- they vary across
       hosts but the perf class is the same.
    4. Collapse whitespace/hyphens to a single underscore.
    5. Uppercase.

Anything that doesn't match a known prefix passes through unchanged
(uppercased + underscored).  Unknown models still group consistently
because we hash the same input the same way.
"""

from __future__ import annotations

import re


_VENDOR_PREFIXES = ("NVIDIA", "AMD", "INTEL")
_FAMILY_PREFIXES = (
    "GEFORCE",
    "QUADRO",
    "TESLA",
    "RADEON",
    "INSTINCT",
)
# VRAM / packaging suffixes that vary across hosts for the same chip.
# ``RTX_4090_24G`` and ``RTX_4090`` should hash identically because their
# render throughput is identical.
_NOISE_SUFFIXES = re.compile(
    r"(?:_(?:\d{1,3}(?:GB|G)|NVL|SXM\d?|PCIE|SUPER_TI|TI_SUPER|MAX_Q))$",
    flags=re.IGNORECASE,
)
# Strip trailing parenthetical annotations -- ``RTX 4090 (24 GB)``,
# ``L40S (PCIE)``, etc.  Applied BEFORE whitespace/hyphen collapse so the
# inner content (which may include spaces) is consumed cleanly.
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")
_WHITESPACE_OR_HYPHEN = re.compile(r"[\s\-]+")
_REPEATED_UNDERSCORES = re.compile(r"_+")


class GpuNameNormalizer:

    def normalize(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        s = raw.strip()
        if not s:
            return None

        # 0. Strip trailing parenthetical annotations.  Loop because a
        # value like ``RTX 4090 (24 GB) (PCIE)`` carries two.
        while True:
            stripped = _TRAILING_PARENS.sub("", s).strip()
            if stripped == s:
                break
            s = stripped

        # 1+2. Strip vendor + family prefixes (case-insensitive, repeated).
        upper = s.upper()
        for prefix in (*_VENDOR_PREFIXES, *_FAMILY_PREFIXES):
            if upper.startswith(prefix + " "):
                upper = upper[len(prefix) + 1:].strip()
            elif upper.startswith(prefix + "_"):
                upper = upper[len(prefix) + 1:].strip()

        # 4. Collapse whitespace and hyphens to single underscores.
        s2 = _WHITESPACE_OR_HYPHEN.sub("_", upper)
        s2 = _REPEATED_UNDERSCORES.sub("_", s2).strip("_")

        # 3. Strip VRAM / packaging suffixes.  Loop because some strings
        # carry both (e.g. ``RTX_4090_24G_PCIE`` -> ``RTX_4090_24G`` ->
        # ``RTX_4090``).
        while True:
            stripped = _NOISE_SUFFIXES.sub("", s2)
            if stripped == s2:
                break
            s2 = stripped

        return s2 or None
