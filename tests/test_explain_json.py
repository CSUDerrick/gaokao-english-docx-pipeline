"""A model essay that quotes something must not kill the run.

Writing an English model essay, the model reaches for a quotation mark sooner or
later — `like "accept your feelings" helped me stay calm` — and one unescaped `"`
makes the whole JSON reply unparseable. That is what happened to 江苏's 应用文 on
the first real run.

Repairing the string in the parser would be a silent degradation (the essay would
come back subtly mangled), and hard-failing wastes the rest of the paper. So the
model gets a couple of corrective turns, and only then do we give up.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
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


def test_recovers_within_a_couple_of_corrective_turns():
    # One repair turn was not enough for the big nested 读后续写 JSON: the model rendered
    # its braces in Chinese quotes, the single retry said "don't" and it did it again, and
    # the whole paper failed. A second nudge — one that echoes the model's own opening back
    # at it — recovers the item.
    conversation = FakeConversation([BROKEN, BROKEN, FIXED])
    parsed, _, usages = pipeline.ask_for_json(
        conversation, _args(), "讲这道题",
        model="deepseek-v4-flash", reasoning_effort="medium",
        thinking="enabled", max_tokens=16000,
    )
    assert isinstance(parsed, dict), "a second corrective turn recovered the item"
    assert len(conversation.asked) == 3
    assert "上一条的开头是" in conversation.asked[2], "the later retry echoes the model's own mistake"
    assert len(usages) == 3, "every billed turn is counted"


def test_a_reply_that_is_still_broken_is_a_hard_error():
    replies = [BROKEN] * (pipeline.JSON_REPAIR_MAX_TURNS + 1)
    conversation = FakeConversation(replies)
    parsed, result, _ = pipeline.ask_for_json(
        conversation, _args(), "讲这道题",
        model="deepseek-v4-flash", reasoning_effort="medium",
        thinking="enabled", max_tokens=16000,
    )
    assert not isinstance(parsed, dict)
    assert len(conversation.asked) == pipeline.JSON_REPAIR_MAX_TURNS + 1, "all retries used"
    try:
        pipeline.require_parsed(parsed, result, 16000, "explain", "item")
    except RuntimeError as exc:
        assert "不是合法 JSON" in str(exc)
    else:
        raise AssertionError("a still-broken reply must stop the run, not degrade")


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


# --- the writing split: one giant nested JSON became a brief plus one small idea per turn.

BRIEF = '{"审题": {"原文情境": "母亲盯着成绩"}, "评分要点": "先看两段是否接住首句"}'
ONE_IDEA = (
    '{"角度": "母亲的认知转变", "提纲": ["第一段：电话", "第二段：拥抱"], '
    '"范文": "Not long after, the phone rang.\\n\\nThat afternoon, she came home.", '
    '"词数": 168, "亮点": {"高级词汇": ["moistened"], "黄金句型": ["Heart pounding, she paused.（独立主格）"]}}'
)


def _writing_args() -> argparse.Namespace:
    return argparse.Namespace(
        explain_model="deepseek-v4-flash",
        explain_reasoning_effort="high",
        explain_thinking="enabled",
        explain_max_tokens=16000,
        provider="deepseek",
        preset="quality",
        save_conversations=False,
    )


def test_writing_is_asked_in_small_pieces_and_merges_to_the_expected_shape():
    # The brief is one small turn; each idea is another. Never the 2–3-essay monolith that
    # broke its own JSON. The merged result must be exactly the shape render_explanation
    # already renders, so the exporter needs no change.
    replies = [BRIEF] + [ONE_IDEA] * pipeline.WRITING_IDEA_COUNT
    conversation = FakeConversation(replies)
    segment = {"question_text": "A story with two opening sentences.",
               "item_id": "p1__continuation_writing__01", "answer_key": ""}
    with tempfile.TemporaryDirectory() as tmp:
        merged, usages, clients = pipeline.explain_writing(
            conversation, _writing_args(), Path(tmp), segment,
            "continuation_writing", "官方参考范文……", "p1__continuation_writing__01",
        )
    assert len(conversation.asked) == 1 + pipeline.WRITING_IDEA_COUNT, "brief + one turn per idea"
    assert set(merged) >= {"审题", "思路", "评分要点"}
    assert len(merged["思路"]) == pipeline.WRITING_IDEA_COUNT
    # The exact contract with the exporter: this dict renders without a KeyError.
    rendered = pipeline.render_explanation(merged)
    assert "审题" in rendered and "思路 1" in rendered and "评分要点" in rendered
    assert len(usages) == 1 + pipeline.WRITING_IDEA_COUNT


def test_a_broken_writing_idea_leaves_the_whole_reply_on_disk():
    # 200 chars in the error message is not enough to debug a broken essay JSON. On a hard
    # failure the entire reply and the prompt are kept under <out>/failures/ for a post-mortem.
    long_broken = BROKEN * 50
    result = pipeline.ChatResult(
        content=long_broken, usage={"completion_tokens": 100}, client_used="http",
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        path = pipeline.dump_failure(
            out, "explain", "p1__continuation_writing__01", "THE-PROMPT-WE-SENT",
            result, RuntimeError("不是合法 JSON"),
            provider="deepseek", model="deepseek-v4-pro", preset="quality",
        )
        assert path.exists() and path.parent.name == "failures"
        saved = path.read_text(encoding="utf-8")
    assert "THE-PROMPT-WE-SENT" in saved
    assert "不是合法 JSON" in saved
    assert long_broken in saved, "the full reply is kept, not the 200-char preview"
