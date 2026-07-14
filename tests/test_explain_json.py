"""A model essay that quotes something must not kill the run.

Writing an English model essay, the model reaches for a quotation mark sooner or
later — `like "accept your feelings" helped me stay calm` — and one unescaped `"`
makes the whole JSON reply unparseable. That is what happened to 江苏's 应用文 on
the first real run.

Repairing the string in the parser would be a silent degradation (the essay would
come back subtly mangled), and hard-failing wastes the rest of the paper. So the
model gets exactly one corrective turn, and only then do we give up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gaokao_english_docx_pipeline as pipeline  # noqa: E402

BROKEN = '{"审题": {"体裁": "邮件"}, "思路": [{"范文": "your tips like "accept it" helped me."}]}'
FIXED = '{"审题": {"体裁": "邮件"}, "思路": [{"范文": "your tips like \'accept it\' helped me."}]}'


class FakeConversation:
    """Records what it was asked, replies with a scripted queue."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.asked: list[str] = []

    def ask(self, prompt: str, *, max_tokens: int | None = None, item_id: str = ""):
        self.asked.append(prompt)
        return pipeline.ChatResult(
            content=self.replies.pop(0),
            usage={"completion_tokens": 100, "prompt_tokens": 10},
            client_used="http",
        )


def _args(max_tokens: int = 16000) -> argparse.Namespace:
    return argparse.Namespace(explain_max_tokens=max_tokens)


def test_valid_json_is_returned_on_the_first_turn():
    conversation = FakeConversation([FIXED])
    parsed, _, usages = pipeline.ask_for_json(
        conversation, _args(), "讲这道题",
        model="deepseek-v4-flash", reasoning_effort="medium",
        thinking="enabled", max_tokens=16000,
    )
    assert isinstance(parsed, dict)
    assert len(conversation.asked) == 1, "no repair turn when nothing is broken"
    assert len(usages) == 1


def test_an_unescaped_quote_is_handed_back_to_the_model_once():
    conversation = FakeConversation([BROKEN, FIXED])
    parsed, _, usages = pipeline.ask_for_json(
        conversation, _args(), "讲这道题",
        model="deepseek-v4-flash", reasoning_effort="medium",
        thinking="enabled", max_tokens=16000,
    )
    assert isinstance(parsed, dict), "the corrective turn recovered the item"
    assert parsed["思路"][0]["范文"].endswith("helped me.")
    assert len(conversation.asked) == 2
    assert "合法 JSON" in conversation.asked[1]
    assert len(usages) == 2, "both turns are billed, so both must be counted"


def test_a_reply_that_is_still_broken_is_a_hard_error():
    conversation = FakeConversation([BROKEN, BROKEN])
    parsed, result, _ = pipeline.ask_for_json(
        conversation, _args(), "讲这道题",
        model="deepseek-v4-flash", reasoning_effort="medium",
        thinking="enabled", max_tokens=16000,
    )
    assert not isinstance(parsed, dict)
    try:
        pipeline.require_parsed(parsed, result, 16000, "explain", "item")
    except RuntimeError as exc:
        assert "不是合法 JSON" in str(exc)
    else:
        raise AssertionError("a twice-broken reply must stop the run, not degrade")


def test_the_essay_word_count_excludes_the_greeting_and_sign_off():
    # It is counted rather than read out of the model's own 词数 field, which it often
    # omits. And it must exclude the salutation/sign-off, because the 100–120 the
    # prompt asks for excludes them — a count that quietly includes "Dear Mr. Smith /
    # Yours, Li Hua" is four words of nonsense exactly when the teacher is deciding
    # whether the essay runs long.
    essay = (
        "Dear Mr. Smith,\n\n"
        "I am writing to update you on our project.\n\n"
        "Yours,\nLi Hua"
    )
    assert pipeline.essay_word_count(essay) == 9, pipeline.essay_word_count(essay)

    # A continuation has no letter framing at all; everything counts.
    assert pipeline.essay_word_count("Eric nodded and grabbed a bag.") == 6


SMART_QUOTED = '{“word”: “abandon”, “meaning”: “放弃”}'


def test_the_repair_turn_names_the_mistake_that_was_actually_made():
    # Telling the model the wrong thing makes it worse. "Don't use double quotes" is
    # right for the *contents* of a string, and flash duly applied it to the JSON's own
    # syntax — `{“word”: “x”}` — whereupon the generic repair message said it again and
    # it did it again. This is what killed the vocabulary stage on a real run.
    assert "语法符号被写成了中文引号" in pipeline._repair_instruction(SMART_QUOTED)
    assert "字符串内部" in pipeline._repair_instruction(BROKEN)


def test_smart_quoted_json_is_recovered_by_the_repair_turn():
    conversation = FakeConversation([SMART_QUOTED, FIXED])
    parsed, _, _ = pipeline.ask_for_json(
        conversation, _args(), "列词汇",
        model="deepseek-v4-flash", reasoning_effort="high",
        thinking="enabled", max_tokens=16000,
    )
    assert isinstance(parsed, dict)
    assert "花括号" in conversation.asked[1], "the retry has to explain the syntax, not scold"


def test_a_truncated_reply_is_not_retried():
    # Retrying a reply that hit the token cap just truncates again; require_parsed
    # already reports that case with the token counts, which is the useful message.
    conversation = FakeConversation([BROKEN, FIXED])
    parsed, _, _ = pipeline.ask_for_json(
        conversation, _args(max_tokens=100), "讲这道题",
        model="deepseek-v4-flash", reasoning_effort="medium",
        thinking="enabled", max_tokens=100,  # == the fake's completion_tokens
    )
    assert not isinstance(parsed, dict)
    assert len(conversation.asked) == 1, "truncation is not a punctuation slip"
