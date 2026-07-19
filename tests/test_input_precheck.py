"""The local input smoke alarm: silent on real papers, loud on broken extraction.

It must never trip on a normal bilingual English paper (that would cry wolf before every
run), and it must catch the failures a bad PDF actually produces — mojibake, an almost
empty scan, a page that decoded to replacement characters.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import input_precheck  # noqa: E402


REAL_PAPER = (
    "阅读理解\n\nReading is one of the most rewarding habits a student can build. "
    "Over the years, researchers have found that people who read widely tend to write "
    "more clearly and think more critically. 第二节 语法填空\n\n"
    "Complete the passage with the correct forms of the words given in brackets. "
    "The little town, which lies by the river, has changed a great deal since then.\n"
    "书面表达\n\nSuppose you are Li Hua. Write an email to your foreign teacher Chris."
) * 3


def test_a_normal_english_paper_is_not_suspect():
    result = input_precheck.precheck_text(REAL_PAPER)
    assert result["suspect"] is False, result["reasons"]


def test_near_empty_extraction_is_suspect():
    result = input_precheck.precheck_text("图片\n\n   ")
    assert result["suspect"] is True
    assert any("过短" in r for r in result["reasons"])


def test_replacement_characters_are_suspect():
    garbage = ("Reading is good. " + "�" * 40) * 5
    result = input_precheck.precheck_text(garbage)
    assert result["suspect"] is True
    assert any("U+FFFD" in r or "替换字符" in r for r in result["reasons"])


def test_a_paper_with_almost_no_english_is_suspect():
    # An English exam that decoded to nearly pure CJK is a sign the passages did not
    # come through. Plenty of letters overall, but < 10% of them Latin.
    text = "试卷" * 400 + " a b"
    result = input_precheck.precheck_text(text)
    assert result["suspect"] is True
    assert any("英文字母占比" in r for r in result["reasons"])


def test_a_wall_of_text_with_no_whitespace_is_suspect():
    result = input_precheck.precheck_text("A" * 2500)
    assert result["suspect"] is True
    assert any("超长无空格串" in r for r in result["reasons"])
