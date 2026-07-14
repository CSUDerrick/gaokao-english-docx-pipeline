"""Persisted app settings, so a second run is one click.

Only preferences live here — API keys go to the macOS Keychain, never to disk.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# The providers, the models and the two presets are defined once, in scripts/, because
# the CLI needs them too — a model offered here but unknown to the pipeline is a crash
# at 开始整理, and tests/test_app_settings.py fails if the two ever drift apart.
import providers as pv  # noqa: E402
from model_presets import (  # noqa: E402
    CUSTOM,
    EFFORT_LABELS,
    PRESET_LABELS,
    PRESETS,
    QUALITY,
    SPEED,
    THINKING,
    default_segment_model,
    detect_preset,
    efforts_for,
    model_ids_for,
    models_for,
    normalize_effort,
    preset_values,
)

__all__ = [
    "CUSTOM", "EFFORT_LABELS", "PRESET_LABELS", "PRESETS", "QUALITY", "SPEED",
    "THINKING", "Settings", "SCHEMA_VERSION", "STAGES", "default_segment_model",
    "detect_preset", "efforts_for", "model_ids_for", "models_for", "normalize_effort",
    "preset_values", "pv",
]

# Every stage the teacher can point at a model. vocab is one of them now: it used to
# ride on the 备课笔记 stage's model with its effort hardcoded, so the词汇表 quietly
# ignored whichever preset had been chosen.
STAGES = ("score", "review", "enrich", "explain", "vocab")

# v5: a provider is now a setting, and the model names are no longer DeepSeek's alone.
# A file written by v4 carries deepseek-v4-* in every model field; if those survived a
# switch to another provider they would be sent to an API that has never heard of them.
# The migration below drops them, and they are re-derived from whatever provider is in
# effect.
SCHEMA_VERSION = 5
MODEL_KEYS = (
    "provider",
    "segment_model",
    *(f"{stage}_{suffix}" for stage in STAGES for suffix in ("model", "effort", "thinking")),
    "preset",
    "verbose",
)

_SPEED = preset_values(SPEED, pv.DEFAULT_PROVIDER)


@dataclass
class Settings:
    input_dir: str = ""
    output_dir: str = ""
    mode: str = "basic"  # basic | advanced | debug
    seen_intro: bool = False

    # Which API. Decides the protocol, the thinking field, and which strength levels
    # even exist — see scripts/providers.py.
    provider: str = pv.DEFAULT_PROVIDER
    # Only for 自定义 / 预留 providers, where there is no URL to default to.
    base_url: str = ""

    # advanced.
    # 速度优先 by default: the run you are willing to repeat after tweaking a prompt.
    # Switch to 质量优先 for the batch that actually gets printed.
    preset: str = SPEED

    # Segmentation runs on your own machine; this model is only the fallback for a
    # paper whose structure no rule can read ("which paragraph does 七选五 start at?"),
    # and the fast model answers that as well as the strong one. Not part of the preset.
    segment_model: str = _SPEED["score_model"]

    score_model: str = _SPEED["score_model"]
    enrich_model: str = _SPEED["enrich_model"]
    review_model: str = _SPEED["review_model"]
    explain_model: str = _SPEED["explain_model"]
    vocab_model: str = _SPEED["vocab_model"]
    score_effort: str = _SPEED["score_effort"]
    enrich_effort: str = _SPEED["enrich_effort"]
    review_effort: str = _SPEED["review_effort"]
    explain_effort: str = _SPEED["explain_effort"]
    vocab_effort: str = _SPEED["vocab_effort"]
    score_thinking: str = _SPEED["score_thinking"]
    enrich_thinking: str = _SPEED["enrich_thinking"]
    review_thinking: str = _SPEED["review_thinking"]
    explain_thinking: str = _SPEED["explain_thinking"]
    vocab_thinking: str = _SPEED["vocab_thinking"]
    workers: int = 16
    review_select: bool = False

    # How the student vocabulary handout is built. Not a right way and a wrong way — a
    # teaching choice, so 基础模式 puts it in front of the teacher:
    #   chunked (完整) — one call per selected question; the words match the paper she is
    #       handing out, question for question.
    #   whole   (困难) — read the paper end to end; the model can judge whether a word is
    #       genuinely hard for *this* paper, at the cost of naming words from questions
    #       the student's copy does not contain.
    # Deliberately not in MODEL_KEYS: that migration exists to drop stale *model defaults*,
    # and this is the teacher's own preference — it must survive a schema bump.
    vocab_mode: str = "whole"

    # PDF input: which OCR service turns a scan into a .docx.
    pdf_backend: str = "paddle"  # paddle | mineru

    # debug
    keep_intermediates: bool = True
    verbose: bool = True

    # Only for a network that intercepts TLS and whose root is not in the Keychain.
    insecure_ssl: bool = False

    # Bumped when a default changes in a way a saved file must not keep overriding.
    version: int = SCHEMA_VERSION

    _path: Path | None = field(default=None, repr=False, compare=False)

    def apply_preset(self, preset: str) -> None:
        """Push a preset's models/efforts/thinking onto every stage it drives."""
        for key, value in preset_values(preset, self.provider).items():
            setattr(self, key, value)
        self.preset = preset

    def apply_provider(self, provider: str) -> None:
        """Switch API, and re-derive every model name from the one now in effect.

        A model name is only meaningful to the provider it came from: keeping
        ``deepseek-v4-pro`` after switching to Claude would send a model Anthropic has
        never heard of. So the models follow the provider, and whichever preset was
        selected is re-resolved against it.
        """
        self.provider = provider
        self.base_url = pv.get(provider).base_url
        preset = self.preset if self.preset in PRESETS else SPEED
        self.apply_preset(preset)
        self.segment_model = default_segment_model(provider)

    def resolved_preset(self) -> str:
        """Which preset the current per-stage values actually add up to.

        Hand-editing one dropdown in 进阶模式 has to flip the badge to 自定义 —
        a radio button that still says 速度优先 while a pro model runs is a lie,
        and 基础模式 shows this string as its summary.
        """
        return detect_preset(asdict(self), self.provider)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}  # a corrupt settings file must not block startup

        # A settings file written before the current defaults would pin the models
        # forever — a saved preference should survive a change of default, but a
        # stale *default* should not masquerade as one. So the model choices are
        # re-derived once, and everything else (folders, workers, review,
        # verbosity) is kept.
        if int(data.get("version", 1)) < SCHEMA_VERSION:
            for key in MODEL_KEYS:
                data.pop(key, None)
            data["version"] = SCHEMA_VERSION

        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and not k.startswith("_")}
        obj = cls(**known)

        # A provider that no longer exists (a renamed entry, a hand-edited file) must not
        # take the app down: fall back to the default rather than raising at startup.
        if obj.provider not in pv.PROVIDERS:
            obj.apply_provider(pv.DEFAULT_PROVIDER)

        # A hand-edited file, or one that predates this provider, can carry a level the
        # model has never had — a `max` saved against DeepSeek landing on GLM, which has
        # no strength dial at all. Fold each onto something real rather than putting a
        # value in the dropdown that is not one of its choices.
        for stage in STAGES:
            key = f"{stage}_effort"
            model = getattr(obj, f"{stage}_model", "")
            setattr(obj, key, normalize_effort(getattr(obj, key), obj.provider, model))

        obj._path = path
        return obj

    def save(self) -> None:
        if self._path is None:
            return
        payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
