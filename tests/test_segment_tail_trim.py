"""Regression tests for _find_answer_section_start tail-trimming logic.

Verifies that answer-section boundaries are detected correctly and that
normal writing-prompts are NOT falsely cut.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from gaokao_english_docx_pipeline import _find_answer_section_start


def _build(*blocks: str) -> str:
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Positive cases: answer section should be detected
# ---------------------------------------------------------------------------

def test_cw_then_answer_key():
    """continuation_writing → '参考答案' — cut before the answer header."""
    text = _build(
        "第二节 读后续写\nParagraph 1: She took a deep breath.",
        "参考答案\n21-23 BDC 24-27 CCBA",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "参考答案" in text[pos:pos + 20]


def test_answer_key_before_later_transcript():
    """paper-name inline answer header should beat a later listening transcript."""
    text = _build(
        "第二节 读后续写\nParagraph 1: Tom had no choice but to ask for help.",
        "某联盟2026届高三英语参考答案\n1-5 CACBB 6-10 CABAA",
        "听力原文\nText 1\nW: Have you seen Rita lately?",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "英语参考答案" in text[pos:pos + 30]


def test_inline_answer_and_analysis_header():
    """inline '参考答案及解析' headers should be treated as answer sections."""
    text = _build(
        "第二节 读后续写\nParagraph 1: Though her tips were for comedy.",
        "湖北黄冈中学2026届高三英语试题参考答案及解析\n1—5 BBACB 6—10 ACACB",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "参考答案及解析" in text[pos:pos + 40]


def test_cw_then_listening_transcript():
    """continuation_writing → '听力原文' — cut before transcript."""
    text = _build(
        "读后续写\nParagraph 1: He looked at the sky.",
        "听力原文\nText 1\nM: Hello, how are you?",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "听力原文" in text[pos:pos + 20]


def test_cw_then_text1_transcript():
    """continuation_writing → 'Text 1' listening transcript."""
    text = _build(
        "第二节\n续写第一段开头：She opened the door.",
        "听力录音稿\nText 1\nW: Good morning.",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "听力录音稿" in text[pos:pos + 20]


def test_model_essay_boundary():
    """continuation_writing → 'One possible version' — cut before model essay."""
    text = _build(
        "读后续写\n阅读下面材料，根据其内容和所给段落开头语续写两段。",
        "One possible version\nParagraph 1: She ran towards the light.",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "One possible version" in text[pos:pos + 30]


def test_cankaofanwen_boundary():
    """continuation_writing → '参考范文' — cut before the Chinese model essay label."""
    text = _build(
        "第二节（满分25分）\n读后续写\n注意：续写词数应为150左右。",
        "参考范文\n他深吸一口气，推开了门。",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "参考范文" in text[pos:pos + 20]


def test_answer_analysis_boundary():
    """continuation_writing → '答案解析' — cut before answer explanations."""
    text = _build(
        "读后续写\n故事到此结束。",
        "答案解析\n21. B 解析：根据第一段可知...",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "答案解析" in text[pos:pos + 20]


def test_grading_rubric_boundary():
    """continuation_writing → '评分标准' — cut before rubric."""
    text = _build(
        "第二节 读后续写\nParagraph 2: And then she smiled.",
        "评分标准\n1. 本题总分为25分...",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    assert "评分标准" in text[pos:pos + 20]


# ---------------------------------------------------------------------------
# Negative cases: normal writing prompts should NOT be cut
# ---------------------------------------------------------------------------

def test_normal_text_in_story_does_not_trigger():
    """The word 'Text' in a normal reading passage should not cause a cut."""
    text = _build(
        "第一节\n阅读下列短文，从每题所给的A、B、C、D四个选项中选出最佳选项。",
        "A\nText messaging has become the most common form of communication...",
        "B\nAccording to the passage, what is the main advantage of...",
        "读后续写",
        "Paragraph 1: She received the text and smiled.",
    )
    pos = _find_answer_section_start(text)
    # Should either find nothing, or find a correct answer boundary
    # (not a false one in the middle of the reading passage).
    if pos is not None:
        # If it found something, it must be AFTER the reading passage and
        # the writing prompt — not in the middle.
        assert pos > text.find("读后续写"), (
            f"False positive: cut at {pos} but '读后续写' is at {text.find('读后续写')}"
        )


def test_app_writing_model_essay_not_confused_with_cw():
    """应用文 'One possible version' before 读后续写 should NOT cut off cw."""
    text = _build(
        "第一节 应用文写作\n请你写一封邀请信。",
        "One possible version\nDear Tom, I am writing to invite you...",
        "第二节 读后续写\n阅读下面材料...",
        "Paragraph 1: She hesitated at the door.",
        "One possible version\nParagraph 1: Taking a deep breath, she knocked.",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    # The cut should be at the SECOND "One possible version" (for cw),
    # not the first one (for application writing).
    cw_pos = text.find("第二节 读后续写")
    assert pos > cw_pos, (
        f"Cut at {pos} is BEFORE cw at {cw_pos} — wrong model essay chosen"
    )


def test_paragraph_in_story_does_not_cut():
    """'Paragraph 1' inside a story prompt is part of the question, not a boundary."""
    text = _build(
        "读后续写",
        "阅读下面材料，根据其内容和所给段落开头语续写两段。",
        "注意：\n1. 续写词数应为150左右；\n2. 请按如下格式在答题卡的相应位置作答。",
        "Paragraph 1:\nShe opened the door and saw...",
        "Paragraph 2:\nThe next morning, everything changed.",
        "参考答案\n21-23 BDC",
    )
    pos = _find_answer_section_start(text)
    assert pos is not None
    # The cut should be at "参考答案", not at "Paragraph 1" inside the prompt.
    assert "Paragraph 1" not in text[:pos].split("\n")[-3:], (
        "Paragraph 1 prompt text was incorrectly cut"
    )
    assert "参考答案" in text[pos:pos + 20]
