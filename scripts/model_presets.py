#!/usr/bin/env python3
"""The two model settings a teacher actually chooses between.

Nobody wants to reason about six providers x N models x six reasoning efforts x
thinking on/off. There are really only two questions — "is this a test run?" and
"is this the batch I hand out?" — so this collapses the matrix to two named answers,
and the per-stage knobs stay in 进阶模式 for when one of them is wrong.

**A preset names a role, not a model.** It used to say ``deepseek-v4-flash`` outright,
which meant the whole idea of a preset died the moment you pointed the app at another
API. Now it says 「快的那个」 / 「强的那个」 and ``providers.py`` says what that is for
whoever you are talking to. Same for strength: 标准 / 深度 resolve to whatever levels
that model really accepts — and to *nothing* on GLM and Qwen, which have no strength
dial at all (决策 26: a notch the server does not distinguish is a decoration, and we
do not draw those).

Segmentation is deliberately absent: it runs locally and costs nothing, and its model
is only the fallback for a paper whose structure no rule can read ("which paragraph
does 七选五 start at?"), which flash answers as well as pro.

Both the CLI and the GUI read this, so a model added to ``providers.py`` reaches both.
"""

from __future__ import annotations

import providers as pv
from providers import DEEP, FLASH, PRO, STANDARD, ModelSpec  # noqa: F401  (re-exported)

THINKING = ["disabled", "enabled"]

# Every level any supported vendor has. Which of them a given model shows is decided by
# providers.py, not here.
EFFORT_LABELS = {
    "": "不可调",
    "none": "不思考",
    "low": "低",
    "medium": "中",
    "high": "标准",
    "xhigh": "深度",
    "max": "最深（慢）",
}

SPEED = "speed"
QUALITY = "quality"
CUSTOM = "custom"

# The stages a preset drives. vocab is one of them now: it used to ride on
# --enrich-model with its effort hardcoded, which meant the词汇表 quietly ignored
# whichever preset the teacher had chosen.
PRESET_STAGES = ("score", "review", "enrich", "explain", "vocab")

# 速度优先 = 快的模型 + 最深的思考；质量优先 = 强的模型 + 最深的思考。
# Depth on both: the failure mode this project actually hits is a model that thought too
# little and emitted a wrong answer, not one that thought too long. The cost of that
# depth is real — see effective_max_tokens, which has to grow the output cap to match
# (决策 9: reasoning tokens are billed against the output quota, and a cap that does not
# account for them once left a call literally zero tokens in which to write its JSON).
PRESETS: dict[str, dict[str, dict[str, str]]] = {
    SPEED: {"*": {"role": FLASH, "effort": DEEP, "thinking": "enabled"}},
    QUALITY: {"*": {"role": PRO, "effort": DEEP, "thinking": "enabled"}},
}

PRESET_LABELS = {
    SPEED: "速度优先（更快，先试一遍看看效果）",
    QUALITY: "质量优先（深度思考，讲解更细致，出成品用）",
    CUSTOM: "自定义（进阶模式手动设置）",
}


def models_for(provider_id: str) -> tuple[ModelSpec, ...]:
    """The models this provider offers, for a dropdown."""
    return pv.get(provider_id).models


def model_ids_for(provider_id: str) -> list[str]:
    return [spec.id for spec in models_for(provider_id)]


def efforts_for(provider_id: str, model_id: str) -> tuple[str, ...]:
    """The strength levels this model really accepts. ``()`` means it has no dial."""
    return pv.model_spec(provider_id, model_id).efforts


def normalize_effort(effort: str, provider_id: str = pv.DEFAULT_PROVIDER, model_id: str = "") -> str:
    """Fold a stored or legacy level onto one this model really has.

    Settings outlive a provider switch, so a ``max`` saved against DeepSeek can arrive
    at a GLM model that has no levels at all. Returning ``""`` there is the point: the
    field must then not be sent, rather than being sent with a value the server will
    either reject or silently ignore.
    """
    spec = pv.model_spec(provider_id, model_id or pv.get(provider_id).role_model(PRO))
    return pv.normalize_effort(effort, spec)


def preset_values(preset: str, provider_id: str = pv.DEFAULT_PROVIDER) -> dict[str, str]:
    """Flat ``{score_model: …, score_effort: …, score_thinking: …, …}`` for a preset.

    Returns ``{}`` for ``custom``, which by definition has no values of its own.
    """
    spec = PRESETS.get(preset)
    if not spec:
        return {}
    provider = pv.get(provider_id)

    values: dict[str, str] = {}
    for stage in PRESET_STAGES:
        stage_spec = spec.get(stage, spec["*"])
        model_id = provider.role_model(stage_spec["role"])
        effort = provider.role_effort(stage_spec["effort"])
        values[f"{stage}_model"] = model_id
        # A model with no dial gets "", and request_fields then omits the field entirely.
        values[f"{stage}_effort"] = pv.normalize_effort(effort, provider.model(model_id))
        values[f"{stage}_thinking"] = stage_spec["thinking"]
    return values


def detect_preset(values: dict[str, str], provider_id: str = pv.DEFAULT_PROVIDER) -> str:
    """Which preset these settings are, or ``custom`` if they are neither.

    The GUI needs this so that hand-editing one dropdown in 进阶模式 flips the preset to
    自定义 instead of leaving a radio button lying about what will run.
    """
    for preset in (SPEED, QUALITY):
        wanted = preset_values(preset, provider_id)
        if wanted and all(values.get(key) == value for key, value in wanted.items()):
            return preset
    return CUSTOM


def default_segment_model(provider_id: str = pv.DEFAULT_PROVIDER) -> str:
    """Segmentation's fallback model: the fast one. It is not part of any preset."""
    return pv.get(provider_id).role_model(FLASH)
