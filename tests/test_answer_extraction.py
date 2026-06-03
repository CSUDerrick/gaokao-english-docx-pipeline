"""Minimal regression tests for answer-extraction functions.

These tests do NOT call the AI — they verify that the local parsers correctly
handle every known answer format discovered across the five real exam papers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gaokao_english_docx_pipeline import (
    parse_answer_tokens,
    parse_grammar_answers,
    extract_all_answers_from_full_text,
    writing_answer_text,
)


# ---------------------------------------------------------------------------
# parse_answer_tokens — multiple-choice range formats
# ---------------------------------------------------------------------------

def test_standard_hyphen_range():
    """21-23 BDC  24-27 CCBA — the most common format."""
    text = "21-23 BDC 24-27 CCBA 28-31 CBCB 32-35 ADBD 36-40 BEGAC"
    answers = parse_answer_tokens(text)
    assert answers[21] == "B"
    assert answers[22] == "D"
    assert answers[23] == "C"
    assert answers[24] == "C"
    assert answers[27] == "A"
    assert answers[36] == "B"
    assert answers[37] == "E"
    assert answers[40] == "C"


def test_em_dash_range_with_period():
    """21—23. BDB 24—27. ACBA — 江南十校 style with Chinese em-dash + period."""
    text = "21—23. BDB 24—27. ACBA 28—31. CDAB 32—35 CACC 36—40. DBCAG"
    answers = parse_answer_tokens(text)
    assert answers[21] == "B"
    assert answers[23] == "B"
    assert answers[24] == "A"
    assert answers[27] == "A"
    assert answers[32] == "C"
    assert answers[35] == "C"
    assert answers[36] == "D"
    assert answers[40] == "G"


def test_tilde_range():
    """21~23 CDB 24~27 CBAC — 成都七中 style with tilde."""
    text = "21~23 CDB 24~27 CBAC 28~31 DDAC 32~35 CABD 36~40 DFBGA"
    answers = parse_answer_tokens(text)
    assert answers[21] == "C"
    assert answers[23] == "B"
    assert answers[32] == "C"
    assert answers[36] == "D"
    assert answers[40] == "A"


def test_tilde_range_no_space():
    """21~23DBC — 重庆巴蜀 style: tilde, no space before answer letters."""
    text = "21~23DBC 24—27 CADA"
    answers = parse_answer_tokens(text)
    assert answers[21] == "D"
    assert answers[22] == "B"
    assert answers[23] == "C"
    assert answers[24] == "C"
    assert answers[27] == "A"


def test_double_hyphen_range():
    """41--45. DADBA — 江南十校 style with double hyphen."""
    text = "41--45. DADBA 46—50. ABBCD 51—55. BCABD"
    answers = parse_answer_tokens(text)
    assert answers[41] == "D"
    assert answers[45] == "A"
    assert answers[46] == "A"
    assert answers[55] == "D"


def test_single_dot_answers():
    """21. B 22. D — single-question format sometimes found in explanations."""
    text = "21. B 22. D 23. C"
    answers = parse_answer_tokens(text)
    assert answers[21] == "B"
    assert answers[22] == "D"
    assert answers[23] == "C"


def test_cloze_range():
    """41-55 full cloze range."""
    text = "41-45 CDAAB 46-50 BDCCD 51-55 AACBA"
    answers = parse_answer_tokens(text)
    assert len([n for n in range(41, 56) if n in answers]) == 15
    assert answers[41] == "C"
    assert answers[55] == "A"


def test_no_duplicate_overwrite():
    """Earlier matches should not overwrite later matches for the same question."""
    text = "21-23 ABC\n21-23 DEF"  # second occurrence should be ignored
    answers = parse_answer_tokens(text)
    assert answers[21] == "A"  # first occurrence wins


# ---------------------------------------------------------------------------
# parse_grammar_answers — grammar fill-in (Q56-65)
# ---------------------------------------------------------------------------

def test_grammar_spaced():
    """56. marking 57. shows — standard well-spaced format."""
    text = "56. marking 57. shows 58. impressive 59. a 60. to enhance"
    answers = parse_grammar_answers(text)
    assert answers[56] == "marking"
    assert answers[57] == "shows"
    assert answers[58] == "impressive"
    assert answers[59] == "a"
    assert answers[60] == "to enhance"


def test_grammar_concatenated():
    """56. would spark57. playfully58.with — 成都七中 concatenated format."""
    text = "56. would spark57. playfully58.with59. and 60. Driven"
    answers = parse_grammar_answers(text)
    assert answers[56] == "would spark"
    assert answers[57] == "playfully"
    assert answers[58] == "with"
    assert answers[59] == "and"
    assert answers[60] == "Driven"


def test_grammar_full_range():
    """All 10 grammar answers 56-65."""
    text = (
        "56. celebrated 57. began 58. a 59. is 60. feelings "
        "61. with 62. energetic 63. standing 64. that/which 65. combination"
    )
    answers = parse_grammar_answers(text)
    assert len(answers) == 10
    assert answers[56] == "celebrated"
    assert answers[64] == "that/which"
    assert answers[65] == "combination"


def test_grammar_with_slashes():
    """Answers containing slashes like 'that/which' or 'was broadcast/was broadcasted'."""
    text = "64. that/which 65. was broadcast/was broadcasted"
    answers = parse_grammar_answers(text)
    assert answers[64] == "that/which"
    assert answers[65] == "was broadcast/was broadcasted"


# ---------------------------------------------------------------------------
# extract_all_answers_from_full_text — full-text scan
# ---------------------------------------------------------------------------

def test_full_text_all_formats():
    """Integration: a realistic answer block with mixed formats."""
    text = (
        "成都七中2025~2026 学年度高三（下）限时训练（四） 英语试题参考答案及评分标准\n"
        "1~5 AACBC 6~10CACBB 11~15 CCBAA16~20 BABCA\n"
        "21~23 CDB 24~27 CBAC 28~31 DDAC 32~35 CABD 36~40 DFBGA\n"
        "41~45 CBDBA 46~50 DBCCB 51~55 CDADA\n"
        "56. would spark57. playfully58.with59. and 60. Driven "
        "61.is filled62. safety63.a 64. introducing65. What\n"
    )
    answers = extract_all_answers_from_full_text(text)
    # Reading: Q21-35
    assert answers[21] == "C"
    assert answers[35] == "D"
    # Gap filling: Q36-40
    assert answers[36] == "D"
    assert answers[40] == "A"
    # Cloze: Q41-55
    assert answers[41] == "C"
    assert answers[55] == "A"
    # Grammar: Q56-65
    assert answers[56] == "would spark"
    assert answers[60] == "Driven"
    assert answers[65] == "What"
    # Count totals
    assert sum(1 for n in range(21, 36) if n in answers) == 15
    assert sum(1 for n in range(36, 41) if n in answers) == 5
    assert sum(1 for n in range(41, 56) if n in answers) == 15
    assert sum(1 for n in range(56, 66) if n in answers) == 10


def test_jiangan_shixiao_style():
    """江南十校: em-dash with periods + double-hyphen ranges."""
    text = (
        "安徽江南十校2026届高三下学期5月学业质量检测英语试题\n"
        "21—23. BDB 24—27. ACBA 28—31. CDAB 32—35 CACC 36—40. DBCAG\n"
        "41--45. DADBA 46—50. ABBCD 51—55. BCABD\n"
        "56. marking 57. shows 58. impressive 59. a 60. to enhance "
        "61. for 62. which 63. Internationally 64. was broadcast 65. but\n"
    )
    answers = extract_all_answers_from_full_text(text)
    assert sum(1 for n in range(21, 36) if n in answers) == 15
    assert sum(1 for n in range(36, 41) if n in answers) == 5
    assert sum(1 for n in range(41, 56) if n in answers) == 15
    assert sum(1 for n in range(56, 66) if n in answers) == 10
    assert answers[21] == "B"
    assert answers[41] == "D"
    assert answers[56] == "marking"
    assert answers[64] == "was broadcast"


def test_bashu_style():
    """重庆巴蜀: tilde without spaces + writing answers inline."""
    text = (
        "重庆市巴蜀中学校2025-2026学年高三下学期5月阶段检测英语试题（十）\n"
        "1~5 ACCBB 6~10 ABCBA 11~15 BAABC16~20 BCACA\n"
        "21~23DBC 24—27 CADA 28—31 BDAA 32—35 BCCB 36~40 FEBAG\n"
        "41~45 CABDA 46~50 BCDBA 51~55 BACCD\n"
        "56. to receive 57. highest 58. herself 59.that 60. visiting "
        "61. exposure 62. as 63. impressed64. people's65. and\n"
    )
    answers = extract_all_answers_from_full_text(text)
    assert answers[21] == "D"
    assert answers[23] == "C"
    assert answers[36] == "F"
    assert answers[56] == "to receive"
    assert answers[65] == "and"
    assert sum(1 for n in range(21, 36) if n in answers) == 15
    assert sum(1 for n in range(56, 66) if n in answers) == 10


def test_empty_text():
    """Empty text returns empty dict."""
    assert extract_all_answers_from_full_text("") == {}
    assert extract_all_answers_from_full_text("No answers here at all.") == {}


# ---------------------------------------------------------------------------
# writing_answer_text — writing sample boundaries
# ---------------------------------------------------------------------------

def test_continuation_answer_stops_before_listening_script():
    """Continuation samples should not absorb listening scripts appended later."""
    text = (
        "第二节 读后续写\n"
        "One possible version\n"
        "Paragraph 1:\nAlex learned to care about others.\n"
        "Paragraph 2:\nHis father smiled at the change.\n"
        "听力录音稿\n"
        "Text 1\n"
        "M: This should not be included.\n"
    )
    answer = writing_answer_text(text, continuation=True)
    assert "Alex learned" in answer
    assert "听力录音稿" not in answer
    assert "Text 1" not in answer


def test_continuation_answer_stops_before_reading_explanation():
    """Continuation samples should not absorb later reading explanations."""
    text = (
        "第二节 读后续写\n"
        "One possible version\n"
        "Paragraph 1:\nMax found the missing cat.\n"
        "Paragraph 2:\nThe neighbors apologized.\n"
        "A\n"
        "【解题导语】介绍英国本科课程申请方式。\n"
        "21.B 文中提到推荐信。\n"
    )
    answer = writing_answer_text(text, continuation=True)
    assert "Max found" in answer
    assert "【解题导语】" not in answer
    assert "21.B" not in answer


# ---------------------------------------------------------------------------
# Smoke test against actual extracted text files
# ---------------------------------------------------------------------------

def test_real_extracted_texts():
    """Verify that ALL five real papers achieve 35/35 choice + 10/10 grammar."""
    extracted_dir = (
        Path(__file__).resolve().parent.parent / "outputs" / "gaokao_english" / "extracted_text"
    )
    if not extracted_dir.exists():
        return  # Skip if outputs not available (e.g. in CI)

    for txt_path in sorted(extracted_dir.glob("*.txt")):
        text = txt_path.read_text(encoding="utf-8")
        answers = extract_all_answers_from_full_text(text)
        reading = sum(1 for n in range(21, 36) if n in answers)
        gap = sum(1 for n in range(36, 41) if n in answers)
        cloze = sum(1 for n in range(41, 56) if n in answers)
        grammar = sum(1 for n in range(56, 66) if n in answers)
        assert reading == 15, f"{txt_path.stem}: reading answers = {reading}/15"
        assert gap == 5, f"{txt_path.stem}: gap-fill answers = {gap}/5"
        assert cloze == 15, f"{txt_path.stem}: cloze answers = {cloze}/15"
        assert grammar == 10, f"{txt_path.stem}: grammar answers = {grammar}/10"
