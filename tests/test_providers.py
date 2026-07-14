"""Each vendor gets the request it actually accepts, and no other.

The pipeline used to write ``reasoning_effort`` and ``thinking`` into every payload,
which are DeepSeek's fields alone. The interesting failures are not "does it crash" but
"does the wrong field get sent quietly":

* ``reasoning_effort`` to GLM — an unknown field it does not act on;
* ``thinking`` to OpenAI — rejected outright;
* ``temperature`` to Claude Opus 4.7+ — a 400;
* ``enable_thinking`` passed to the OpenAI *SDK* as a normal keyword — silently dropped,
  so the model answers without thinking and nothing says so.

So these are payload snapshots. They are the cheapest place to catch a vendor mismatch,
because the alternative is finding it ten minutes into a teacher's run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gaokao_english_docx_pipeline as pipeline  # noqa: E402
import providers as pv  # noqa: E402


def _openai_body(provider: str, model: str, effort: str, thinking: str = "enabled") -> tuple[dict, dict]:
    return pipeline.chat_payload(
        "hi", provider=provider, model=model, temperature=0.2,
        reasoning_effort=effort, thinking=thinking, max_tokens=8000,
    )


def test_deepseek_sends_reasoning_effort_and_its_own_thinking_field():
    body, extras = _openai_body("deepseek", "deepseek-v4-pro", "max")
    assert body["reasoning_effort"] == "max"
    assert body["max_tokens"] == 8000
    assert body["temperature"] == 0.2
    # `thinking` is a vendor extension, not an OpenAI field: it has to leave via extras
    # or the OpenAI SDK drops it.
    assert extras == {"thinking": {"type": "enabled"}}
    assert "thinking" not in body


def test_openai_gets_no_thinking_field_and_no_temperature():
    body, extras = _openai_body("openai", "gpt-5.6-sol", "xhigh")
    assert body["reasoning_effort"] == "xhigh"
    # Reasoning models renamed the cap; sending max_tokens is a 400.
    assert body["max_completion_tokens"] == 8000
    assert "max_tokens" not in body
    assert "thinking" not in body and not extras
    assert "temperature" not in body, "GPT-5 reasoning models reject temperature"


def test_glm_gets_enable_thinking_and_never_an_effort_level():
    """GLM has no strength dial — only thinking on/off.

    Drawing one anyway would be the decoration 决策 26 was written about, and sending
    `reasoning_effort` would be a field GLM does not act on.
    """
    body, extras = _openai_body("zhipu", "glm-5.2", "")
    assert "reasoning_effort" not in body
    assert extras == {"enable_thinking": True}

    # Even if a stale `max` survives from a DeepSeek settings file, it must not be sent.
    body, extras = _openai_body("zhipu", "glm-5.2", "max")
    assert "reasoning_effort" not in body
    assert extras == {"enable_thinking": True}


def test_qwen_expresses_depth_as_a_token_budget():
    """Qwen has no `reasoning_effort` either, but it does have a real depth control."""
    _, deep = _openai_body("qwen", "qwen3.7-max", "max")
    _, standard = _openai_body("qwen", "qwen3.7-max", "high")
    assert deep["enable_thinking"] is True
    assert deep["thinking_budget"] > standard["thinking_budget"], "深度档必须真的想得更久"


def test_thinking_off_is_respected_everywhere():
    for provider, model in (("deepseek", "deepseek-v4-pro"), ("zhipu", "glm-5.2"), ("qwen", "qwen3.7-max")):
        _, extras = _openai_body(provider, model, "high", thinking="disabled")
        assert extras.get("thinking", {}).get("type", "disabled") == "disabled"
        assert extras.get("enable_thinking", False) is False


def test_claude_speaks_the_messages_api_not_chat_completions():
    args = pipeline.parse_args(["x", "--provider", "anthropic", "--preset", "quality"])
    body = pipeline.anthropic_payload(
        "hi", provider="anthropic", model=args.explain_model, temperature=0.2,
        reasoning_effort=args.explain_reasoning_effort, thinking="enabled",
        max_tokens=args.explain_max_tokens,
    )
    # system is a top-level field, not messages[0].
    assert isinstance(body["system"], list)
    assert body["messages"][0]["role"] == "user"
    # Adaptive is the only on-mode on Opus 4.7+; budget_tokens is a 400.
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "xhigh"}
    assert "temperature" not in body, "Opus 4.7+ rejects temperature"
    assert "reasoning_effort" not in body
    assert body["max_tokens"] > 0, "Anthropic requires max_tokens"

    # The frozen instruction prefix is cached — without it every turn of a growing
    # conversation would re-pay full price for the whole history (决策 8).
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_the_endpoint_and_auth_header_match_the_protocol():
    assert pipeline.completion_endpoint("https://api.deepseek.com", "deepseek").endswith("/chat/completions")
    assert pipeline.completion_endpoint("https://api.anthropic.com", "anthropic").endswith("/v1/messages")

    openai_headers = pipeline.auth_headers("deepseek", "sk-x")
    assert openai_headers["Authorization"] == "Bearer sk-x"

    claude_headers = pipeline.auth_headers("anthropic", "sk-x")
    assert claude_headers["x-api-key"] == "sk-x", "Anthropic does not use a Bearer token"
    assert claude_headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in claude_headers


def test_usage_is_normalised_to_one_shape():
    """Three dialects for the same three numbers. The ledger must only ever see one."""
    deepseek = pv.normalize_usage(pv.OPENAI, {
        "prompt_tokens": 1000, "prompt_cache_hit_tokens": 900,
        "prompt_cache_miss_tokens": 100, "completion_tokens": 50,
    })
    assert deepseek["prompt_cache_hit_tokens"] == 900

    # OpenAI reports the cached count one level down.
    openai = pv.normalize_usage(pv.OPENAI, {
        "prompt_tokens": 1000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 900},
    })
    assert openai["prompt_cache_hit_tokens"] == 900
    assert openai["prompt_cache_miss_tokens"] == 100

    # Anthropic calls them something else entirely.
    claude = pv.normalize_usage(pv.ANTHROPIC, {
        "input_tokens": 100, "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 0, "output_tokens": 50,
    })
    assert claude["prompt_cache_hit_tokens"] == 900
    assert claude["prompt_cache_miss_tokens"] == 100
    assert claude["prompt_tokens"] == 1000
    assert claude["completion_tokens"] == 50


def test_conversation_budget_is_a_fraction_of_the_real_window():
    """40% for vocab, 50% for everything else — of the model's own context window.

    The old ceiling was a flat 200_000, which is 20% of DeepSeek's window and simply
    wrong for a 200k-window model: it would have let a GLM conversation grow to the very
    edge of its context.
    """
    big = pv.model_spec("deepseek", "deepseek-v4-pro")     # 1M window
    small = pv.model_spec("zhipu", "glm-5.2")              # 200k window

    assert pv.conversation_budget(big, "vocab") < pv.conversation_budget(big, "explain")
    assert pv.conversation_budget(small, "vocab") < pv.conversation_budget(big, "vocab")

    # 40% of 1M, less the output reserve.
    assert pv.conversation_budget(big, "vocab") == int(1_000_000 * 0.40) - 64_000
    assert pv.conversation_budget(big, "explain") == int(1_000_000 * 0.50) - 64_000


def test_an_unknown_model_is_assumed_small_not_large():
    """A model the teacher typed in, or one that came back from /v1/models.

    Guessing the window too small only costs an extra chunk. Guessing it too large
    overflows the context and corrupts the run — so the default errs low.
    """
    spec = pv.model_spec("custom", "whatever-she-typed")
    assert spec.context_window <= 128_000
    assert spec.price is None, "an unpriced model must not be billed at somebody else's rate"
    assert not spec.verified


def test_every_provider_has_somewhere_to_send_a_request():
    for pid in pv.PROVIDER_ORDER:
        spec = pv.get(pid)
        assert spec.api_key_env, pid
        assert spec.protocol in (pv.OPENAI, pv.ANTHROPIC), pid
        if pid != "custom":
            assert spec.base_url, pid
        # A provider offering models must be able to resolve both preset roles to one.
        if spec.models:
            for role in (pv.FLASH, pv.PRO):
                assert spec.role_model(role) in [m.id for m in spec.models], f"{pid}/{role}"
