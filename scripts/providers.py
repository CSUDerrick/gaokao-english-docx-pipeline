#!/usr/bin/env python3
"""What each API looks like, in one table.

The pipeline used to be DeepSeek-shaped all the way down: ``chat_payload()`` wrote
``reasoning_effort`` and ``thinking:{type}`` unconditionally, and those two fields
are DeepSeek's alone. Sending them to OpenAI is rejected; GLM and Qwen ignore them
(their switch is ``enable_thinking``, and it is not an OpenAI-standard field);
Claude does not even speak the same protocol.

So the vendor differences live here, declared, and every caller asks this module
rather than branching on a model name.

Three things vary, and only three:

* **协议** — ``openai`` (everyone) or ``anthropic`` (Claude alone; see below).
* **思考怎么写** — ``thinking_style``, the one genuinely incompatible field.
* **强度有几档** — DeepSeek has 2, OpenAI has 6, GLM has none at all.

The teacher still sees exactly two choices, 标准 and 深度 (决策 26: giving her more
notches than the server distinguishes is a decoration). Each provider maps those two
*roles* onto levels it really has. A provider with no strength dial says so out loud
instead of drawing a switch that does nothing.

**Claude does not go through its OpenAI-compatibility layer.** Anthropic's own docs
say that layer supports neither extended thinking nor prompt caching and is "not
intended for production" — and those two are this project's lifeblood (决策 8: a
cache hit costs 1/120 of a miss). So Claude gets a native ``/v1/messages`` adapter.

**Unverified context windows are deliberately understated.** Model IDs churn (GLM 4.6
and 4.7 were delisted 2026-07-09), and the failure is asymmetric: guess the window too
small and vocab merely splits into more chunks — correct, slightly chattier. Guess it
too large and the conversation overflows and the run is garbage. So anything we cannot
confirm gets a conservative number and ``verified=False``.

Prices are per 1M tokens in USD. A model we cannot price carries ``price=None`` and is
reported as 费用未知 — never billed at some other model's rate. That silent fallback is
exactly what 决策 20 caught: a flash run quoted at pro's price, 3x too high.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# The two roles a preset picks between. Concrete model ids live in ProviderSpec.roles.
FLASH = "flash"
PRO = "pro"
ROLES = (FLASH, PRO)

# The two strength roles the teacher sees. Concrete levels live in ProviderSpec.effort_roles.
STANDARD = "standard"
DEEP = "deep"
EFFORT_ROLES = (STANDARD, DEEP)

# Protocols.
OPENAI = "openai"
ANTHROPIC = "anthropic"

# thinking_style — the only field where the vendors genuinely disagree.
STYLE_DEEPSEEK = "deepseek"    # reasoning_effort + thinking:{type}
STYLE_OPENAI = "openai"        # reasoning_effort only; no thinking field
STYLE_ZHIPU = "zhipu"          # enable_thinking (vendor extension); no strength dial
STYLE_QWEN = "qwen"            # enable_thinking + thinking_budget (vendor extension)
STYLE_ANTHROPIC = "anthropic"  # thinking:{type:adaptive} + output_config:{effort}
STYLE_NONE = "none"            # non-reasoning model: send none of it

# Reasoning tokens count against the output quota (决策 9), so a conversation must
# leave room for them. Mirrors MAX_EFFORT_TOKEN_CEILING in the pipeline.
OUTPUT_RESERVE = 64_000

# Your requirement: a vocab conversation stays under 40% of the window, everything
# else under 50%. Past that the model gets noticeably dumber long before it errors.
VOCAB_CONTEXT_FRACTION = 0.40
DEFAULT_CONTEXT_FRACTION = 0.50

# What an unknown model (custom endpoint, or one refreshed from /v1/models) is assumed
# to be. Small on purpose — see the module docstring.
UNKNOWN_CONTEXT = 128_000
UNKNOWN_MAX_OUTPUT = 8_192


@dataclass(frozen=True)
class PriceSpec:
    """USD per 1M tokens."""

    hit: float   # cached prompt tokens
    miss: float  # fresh prompt tokens
    out: float   # completion tokens (reasoning tokens are billed as output)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    context_window: int
    max_output: int
    efforts: tuple[str, ...] = ()      # the levels this model REALLY accepts; () = no dial
    price: PriceSpec | None = None     # None = unknown; report as 费用未知, never guess
    verified: bool = True              # False = id/limits not confirmed; refresh from /v1/models

    @property
    def has_effort_dial(self) -> bool:
        return len(self.efforts) > 1


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    protocol: str
    base_url: str
    api_key_env: str
    thinking_style: str
    supports_temperature: bool
    models: tuple[ModelSpec, ...]
    roles: dict[str, str]         # {"flash": <model id>, "pro": <model id>}
    effort_roles: dict[str, str]  # {"standard": <level>, "deep": <level>}
    note: str = ""                # shown in the UI; say the awkward truth here

    def model(self, model_id: str) -> ModelSpec:
        """The spec for a model id, synthesising a conservative one if we don't know it.

        A model the teacher typed herself, or one that came back from /v1/models, is
        still perfectly usable — we just don't know its window or its price, and we
        say so rather than assuming.
        """
        for spec in self.models:
            if spec.id == model_id:
                return spec
        return ModelSpec(
            id=model_id,
            label=model_id,
            context_window=UNKNOWN_CONTEXT,
            max_output=UNKNOWN_MAX_OUTPUT,
            efforts=self.default_efforts(),
            price=None,
            verified=False,
        )

    def default_efforts(self) -> tuple[str, ...]:
        """The levels to assume for a model we have no entry for.

        Whatever the provider's two roles resolve to. If they resolve to the same thing
        (or to nothing), this provider has no strength dial and the answer is ``()``.
        """
        levels: list[str] = []
        for role in EFFORT_ROLES:
            level = self.effort_roles.get(role)
            if level and level not in levels:
                levels.append(level)
        return tuple(levels) if len(levels) > 1 else ()

    def role_model(self, role: str) -> str:
        """The model id this role means. Empty for a provider with no models of its own."""
        return self.roles.get(role) or self.roles.get(PRO) or (self.models[0].id if self.models else "")

    def role_effort(self, role: str) -> str:
        return self.effort_roles.get(role, "")


# --------------------------------------------------------------------------- the table


_DEEPSEEK = ProviderSpec(
    id="deepseek",
    label="DeepSeek（深度求索）",
    protocol=OPENAI,
    base_url="https://api.deepseek.com",
    api_key_env="DEEPSEEK_API_KEY",
    thinking_style=STYLE_DEEPSEEK,
    supports_temperature=True,
    # reasoning_effort really only has high and max: DeepSeek's own docs say low and
    # medium are both mapped to high, and xhigh to max (决策 26).
    models=(
        ModelSpec(
            id="deepseek-v4-flash",
            label="V4 Flash（快）",
            context_window=1_000_000,
            max_output=64_000,
            efforts=("high", "max"),
            price=PriceSpec(hit=0.0028, miss=0.14, out=0.28),
        ),
        ModelSpec(
            id="deepseek-v4-pro",
            label="V4 Pro（强）",
            context_window=1_000_000,
            max_output=64_000,
            efforts=("high", "max"),
            price=PriceSpec(hit=0.003625, miss=0.435, out=0.87),
        ),
    ),
    roles={FLASH: "deepseek-v4-flash", PRO: "deepseek-v4-pro"},
    effort_roles={STANDARD: "high", DEEP: "max"},
)


_OPENAI = ProviderSpec(
    id="openai",
    label="OpenAI",
    protocol=OPENAI,
    base_url="https://api.openai.com/v1",
    api_key_env="OPENAI_API_KEY",
    thinking_style=STYLE_OPENAI,
    # GPT-5 reasoning models reject temperature. Cheaper than discovering that at
    # runtime on the teacher's machine.
    supports_temperature=False,
    # GPT-5.6 ships as three tiers (Sol / Terra / Luna). `deep` maps to xhigh, not max:
    # max is prone to overthinking, and every stage here ends in a JSON object, not an
    # open-ended proof.
    models=(
        ModelSpec(
            id="gpt-5.6-luna",
            label="GPT-5.6 Luna（快）",
            context_window=1_000_000,
            max_output=128_000,
            efforts=("none", "low", "medium", "high", "xhigh", "max"),
            price=PriceSpec(hit=0.10, miss=1.00, out=6.00),
            verified=False,
        ),
        ModelSpec(
            id="gpt-5.6-terra",
            label="GPT-5.6 Terra（均衡）",
            context_window=1_000_000,
            max_output=128_000,
            efforts=("none", "low", "medium", "high", "xhigh", "max"),
            price=PriceSpec(hit=0.25, miss=2.50, out=15.00),
            verified=False,
        ),
        ModelSpec(
            id="gpt-5.6-sol",
            label="GPT-5.6 Sol（强）",
            context_window=1_000_000,
            max_output=128_000,
            efforts=("none", "low", "medium", "high", "xhigh", "max"),
            price=PriceSpec(hit=0.50, miss=5.00, out=30.00),
            verified=False,
        ),
    ),
    roles={FLASH: "gpt-5.6-luna", PRO: "gpt-5.6-sol"},
    effort_roles={STANDARD: "high", DEEP: "xhigh"},
    note="模型编号会随版本更替；请用「刷新模型列表」核对。",
)


_ANTHROPIC = ProviderSpec(
    id="anthropic",
    label="Claude（Anthropic）",
    protocol=ANTHROPIC,
    base_url="https://api.anthropic.com",
    api_key_env="ANTHROPIC_API_KEY",
    thinking_style=STYLE_ANTHROPIC,
    # Opus 4.7+ and Sonnet 5 reject temperature/top_p/top_k outright (HTTP 400).
    supports_temperature=False,
    models=(
        ModelSpec(
            id="claude-haiku-4-5",
            label="Haiku 4.5（快）",
            context_window=200_000,
            max_output=64_000,
            efforts=(),  # no effort dial on Haiku 4.5
            price=PriceSpec(hit=0.10, miss=1.00, out=5.00),
        ),
        ModelSpec(
            id="claude-sonnet-5",
            label="Sonnet 5（均衡）",
            context_window=1_000_000,
            max_output=128_000,
            efforts=("low", "medium", "high", "xhigh", "max"),
            price=PriceSpec(hit=0.30, miss=3.00, out=15.00),
        ),
        ModelSpec(
            id="claude-opus-4-8",
            label="Opus 4.8（强）",
            context_window=1_000_000,
            max_output=128_000,
            efforts=("low", "medium", "high", "xhigh", "max"),
            price=PriceSpec(hit=0.50, miss=5.00, out=25.00),
        ),
    ),
    roles={FLASH: "claude-sonnet-5", PRO: "claude-opus-4-8"},
    effort_roles={STANDARD: "high", DEEP: "xhigh"},
)


_ZHIPU = ProviderSpec(
    id="zhipu",
    label="智谱 GLM",
    protocol=OPENAI,
    base_url="https://open.bigmodel.cn/api/paas/v4",
    api_key_env="ZHIPU_API_KEY",
    thinking_style=STYLE_ZHIPU,
    supports_temperature=True,
    # GLM has NO strength dial — only thinking on/off. `efforts=()` is what makes the
    # advanced tab print 「该模型不可调强度」 instead of drawing a switch that changes
    # nothing, which is the mistake 决策 26 was written about.
    #
    # Context understated on purpose: GLM-5.2 advertises 1M, but the id and its limits
    # churn (4.6/4.7 were delisted 2026-07-09) and guessing high is the dangerous
    # direction. Confirm with 刷新模型列表.
    models=(
        ModelSpec(
            id="glm-5.2",
            label="GLM-5.2",
            context_window=200_000,
            max_output=32_000,
            efforts=(),
            price=None,
            verified=False,
        ),
    ),
    roles={FLASH: "glm-5.2", PRO: "glm-5.2"},
    effort_roles={STANDARD: "", DEEP: ""},
    note="GLM 只有「思考开/关」，没有强度档：标准与深度效果相同。价格未配置，花费不计入。",
)


_QWEN = ProviderSpec(
    id="qwen",
    label="通义千问 Qwen（阿里百炼）",
    protocol=OPENAI,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key_env="DASHSCOPE_API_KEY",
    thinking_style=STYLE_QWEN,
    supports_temperature=True,
    # Qwen has no `reasoning_effort` field, but it does have a real depth control:
    # thinking_budget, a token count. So it keeps the two-notch dial — the level is just
    # translated into a budget by THINKING_BUDGETS instead of being sent verbatim. This
    # is the opposite of GLM, where there is genuinely nothing to turn.
    models=(
        ModelSpec(
            id="qwen3.7-max",
            label="Qwen3.7 Max",
            context_window=262_144,
            max_output=32_768,
            efforts=("high", "max"),
            price=None,
            verified=False,
        ),
    ),
    roles={FLASH: "qwen3.7-max", PRO: "qwen3.7-max"},
    effort_roles={STANDARD: "high", DEEP: "max"},
    note="Qwen 的「深度」是加长 thinking_budget（思考 token 预算）。价格未配置，花费不计入。",
)


_CUSTOM = ProviderSpec(
    id="custom",
    label="自定义（任意 OpenAI 兼容端点）",
    protocol=OPENAI,
    base_url="",
    api_key_env="CUSTOM_API_KEY",
    thinking_style=STYLE_NONE,
    supports_temperature=True,
    models=(),
    roles={},
    effort_roles={STANDARD: "", DEEP: ""},
    note="填入 base_url 与模型名；不确定就先用「刷新模型列表」。默认不发送任何思考字段。",
)


# Reserved: one row each, no code. All of these speak OpenAI's chat/completions, so
# they need nothing but a base_url. The teacher fills in the model with 刷新模型列表.
def _reserved(pid: str, label: str, base_url: str, key_env: str, style: str = STYLE_NONE) -> ProviderSpec:
    return replace(_CUSTOM, id=pid, label=label, base_url=base_url, api_key_env=key_env,
                   thinking_style=style, note="预留：请填入模型名（可用「刷新模型列表」）。价格未配置。")


_RESERVED = (
    _reserved("moonshot", "Kimi（月之暗面）", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    _reserved("doubao", "豆包（火山方舟）", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY"),
    _reserved("minimax", "MiniMax", "https://api.minimax.chat/v1", "MINIMAX_API_KEY"),
    _reserved("siliconflow", "硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY"),
    _reserved("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    _reserved("ollama", "本地 Ollama / vLLM", "http://127.0.0.1:11434/v1", "OLLAMA_API_KEY"),
)


PROVIDERS: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in (_DEEPSEEK, _OPENAI, _ANTHROPIC, _ZHIPU, _QWEN, *_RESERVED, _CUSTOM)
}

DEFAULT_PROVIDER = "deepseek"

# Order shown in the picker: the ones we actually tested first.
PROVIDER_ORDER = ("deepseek", "openai", "anthropic", "zhipu", "qwen",
                  *(spec.id for spec in _RESERVED), "custom")


def get(provider_id: str) -> ProviderSpec:
    return PROVIDERS.get(str(provider_id or "").strip().lower()) or PROVIDERS[DEFAULT_PROVIDER]


def model_spec(provider_id: str, model_id: str) -> ModelSpec:
    return get(provider_id).model(model_id)


# --------------------------------------------------------------------------- efforts


def normalize_effort(effort: str, spec: ModelSpec) -> str:
    """Fold an effort onto one this model really accepts.

    Settings survive a provider switch, so a stored ``max`` can arrive at a model that
    has never heard of it. Rather than let the API reject the call, land on the closest
    level the model does have.
    """
    effort = str(effort or "").strip().lower()
    if not spec.efforts:
        return ""  # no dial on this model; the field must not be sent at all
    if effort in spec.efforts:
        return effort
    # Legacy DeepSeek levels, and anything from another vendor: bias toward more
    # thinking, not less — the presets all ask for depth.
    if effort in {"xhigh", "max"}:
        return spec.efforts[-1]
    if effort in {"none", "low", "medium", "high"} and "high" in spec.efforts:
        return "high"
    return spec.efforts[-1]


def is_deepest(effort: str, provider_id: str, model_id: str = "") -> bool:
    """Is this the deepest setting this provider offers?

    Not ``effort == "max"``. The deep preset resolves to ``xhigh`` on OpenAI and Claude,
    and its chain of thought is every bit as long as DeepSeek's ``max`` — so a caller
    that string-matches ``"max"`` would decide those two were *not* thinking hard, and
    would size their output cap for a short answer. That is 决策 9's bug, and it is why
    this asks the provider instead of guessing.

    A model with no strength dial at all (GLM) answers ``True``: we cannot tell how long
    it means to think, and over-reserving output tokens is free while under-reserving
    truncates the JSON.
    """
    provider = get(provider_id)
    spec = provider.model(model_id or provider.role_model(PRO))
    if not spec.efforts:
        return True
    deep = normalize_effort(provider.role_effort(DEEP) or spec.efforts[-1], spec)
    return normalize_effort(effort, spec) == deep


# Qwen expresses depth as a thinking-token budget rather than a named level.
THINKING_BUDGETS = {STANDARD: 4_000, DEEP: 32_000}


def thinking_budget_for(effort: str, provider_id: str, model_id: str = "") -> int:
    """How many thinking tokens to allow, for vendors that take a number not a level."""
    return THINKING_BUDGETS[DEEP if is_deepest(effort, provider_id, model_id) else STANDARD]


# --------------------------------------------------------------------------- payloads


def request_fields(
    provider_id: str,
    model_id: str,
    *,
    effort: str,
    thinking: str,
    temperature: float,
    max_tokens: int | None,
) -> tuple[dict, dict]:
    """The vendor-specific half of a request.

    Returns ``(standard, extras)``:

    * ``standard`` — OpenAI-shaped fields that go straight into the JSON body and, for
      the SDK path, straight into ``client.chat.completions.create(**kwargs)``.
    * ``extras`` — vendor extensions that are *not* OpenAI-standard. Over raw HTTP they
      are merged into the same JSON body; through the OpenAI SDK they have to travel in
      ``extra_body`` or the SDK strips them.

    Keeping them apart is the whole reason GLM and Qwen work at all: ``enable_thinking``
    silently vanishes if it is passed as a normal keyword argument to the SDK.
    """
    provider = get(provider_id)
    spec = provider.model(model_id)
    style = provider.thinking_style
    standard: dict = {}
    extras: dict = {}

    if provider.supports_temperature:
        standard["temperature"] = temperature

    if max_tokens:
        # Reasoning models on OpenAI renamed the cap; sending max_tokens is a 400.
        key = "max_completion_tokens" if style == STYLE_OPENAI else "max_tokens"
        standard[key] = max_tokens

    level = normalize_effort(effort, spec)
    on = thinking == "enabled"

    if style == STYLE_DEEPSEEK:
        if level:
            standard["reasoning_effort"] = level
        if thinking != "omit":
            extras["thinking"] = {"type": thinking}

    elif style == STYLE_OPENAI:
        if level:
            standard["reasoning_effort"] = level if on else "none"

    elif style == STYLE_ZHIPU:
        if thinking != "omit":
            extras["enable_thinking"] = on

    elif style == STYLE_QWEN:
        if thinking != "omit":
            extras["enable_thinking"] = on
            if on:
                extras["thinking_budget"] = thinking_budget_for(effort, provider_id, model_id)

    elif style == STYLE_ANTHROPIC:
        # Adaptive is the only on-mode on Opus 4.7+/Sonnet 5; budget_tokens is a 400.
        if on:
            standard["thinking"] = {"type": "adaptive"}
        if level:
            standard["output_config"] = {"effort": level}

    # STYLE_NONE: send nothing. A non-reasoning or local model.
    return standard, extras


# --------------------------------------------------------------------------- budgets


def conversation_budget(spec: ModelSpec, stage: str = "") -> int:
    """How many tokens one conversation may grow to before it must be restarted.

    Two separate worries, and only the first is about the API accepting the request:

    * the reply needs somewhere to live, so reserve the output cap;
    * quality falls off long before the window is full, which is why this is a
      fraction and not the whole thing (决策 8 restarted at 20%; you asked for 40%
      on vocab and 50% elsewhere).
    """
    fraction = VOCAB_CONTEXT_FRACTION if stage == "vocab" else DEFAULT_CONTEXT_FRACTION
    reserve = min(spec.max_output, OUTPUT_RESERVE)
    return max(int(spec.context_window * fraction) - reserve, 8_000)


# --------------------------------------------------------------------------- usage


def normalize_usage(protocol: str, usage: object) -> dict:
    """Rewrite a vendor's usage block into the one shape the ledger records.

    Three dialects for the same three numbers. Translating here means ``record_usage``
    and ``usage_report`` never learn that more than one exists.
    """
    if not isinstance(usage, dict):
        return {}

    if protocol == ANTHROPIC:
        cached = int(usage.get("cache_read_input_tokens") or 0)
        # Cache *writes* are billed above the miss rate, but they are still fresh input;
        # folding them into miss keeps the ledger honest to within the write premium.
        fresh = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        return {
            "prompt_tokens": cached + fresh,
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": fresh,
            "completion_tokens": output,
            "total_tokens": cached + fresh + output,
            "completion_tokens_details": {"reasoning_tokens": 0},  # not reported separately
        }

    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("prompt_cache_hit_tokens") or 0)
    if not cached:
        # OpenAI reports the same number one level down.
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
    fresh = int(usage.get("prompt_cache_miss_tokens") or 0) or max(prompt - cached, 0)

    out = dict(usage)
    out["prompt_tokens"] = prompt
    out["prompt_cache_hit_tokens"] = cached
    out["prompt_cache_miss_tokens"] = fresh
    out["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    out.setdefault("total_tokens", prompt + out["completion_tokens"])
    return out
