"""The teacher edition is 原题 + 官方答案与解析 + 详细解析和解答步骤, in that order.

The regression that matters most here is the *student* edition: this change rebuilt
the teacher path, and the student paper is the one that gets handed to a room full
of teenagers. So these tests also pin that the student file still carries no source
attribution, no answer key and no explanation.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docx_blocks as db  # noqa: E402
import export_docx_splice as ex  # noqa: E402
import gaokao_english_docx_pipeline as pipeline  # noqa: E402
from docx import Document  # noqa: E402
from docx.shared import Mm  # noqa: E402

QUESTION = [
    "B",
    "Cui Yameng runs a barrier-free guesthouse in her home city.",
    "24. What inspired Cui to design the guesthouse?",
    "A. A news report.  B. A hotel stay.  C. Her travels.  D. A friend's advice.",
    "25. What does the underlined word mean?",
    "A. Refused.  B. Encouraged.  C. Delayed.  D. Ignored.",
]
ANSWERS = [
    "参考答案",
    "24—25. CB",
    "【导语】本文主要讲述崔雅梦创办无障碍民宿帮助残障人士的故事。",
    "【24题详解】",
    "细节理解题。根据第二段可知，灵感来源于她与残障朋友的旅行。",
    "【25题详解】",
    "词义猜测题。根据上下文可知，该词意为“鼓舞”。",
]

EXPLANATION = {
    "questions": [
        {
            "number": "24",
            "answer": "C",
            "question_type": "细节理解题",
            "locate": "第二段：\"Her travels with physically-challenged friends…\"",
            "reasoning": "原文的 travels 被选项换成了 Her travels，属于同义替换。",
            "distractors": [{"option": "B", "why_wrong": "文中确实提到酒店，但那是她的目标，不是灵感来源。"}],
            "language_note": "",
        },
        {
            "number": "25",
            "answer": "B",
            "question_type": "词义猜测题",
            "locate": "第三段：\"The post inspired many followers…\"",
            "reasoning": "后一句举了一个受鼓舞出行的例子，方向是正面的。",
            "distractors": [{"option": "A", "why_wrong": "Refused 是反方向，和后文的 embarked on his first trip 冲突。"}],
        },
    ]
}


def _paper(tmp: Path) -> Path:
    doc = Document()
    # Real papers are A4, and validate() refuses anything else — python-docx would
    # otherwise hand us US Letter and fail the export on the fixture, not the code.
    section = doc.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    for line in QUESTION:
        doc.add_paragraph(line)
    for line in ANSWERS:
        doc.add_paragraph(line)
    path = tmp / "湖北某中学2026届高三英语试题.docx"
    doc.save(str(path))
    return path


def _run_dir(tmp: Path) -> Path:
    """A finished run: one selected item, with its segment and its explanation."""
    src = _paper(tmp)
    doc = db.read_docx(src)
    question_end = doc.text.index("参考答案")
    lo, hi = doc.body_range(0, question_end)

    import answer_explanation as ax

    official = ax.OfficialExplanations(doc, question_end)
    explanation_blocks = official.blocks_for([24, 25])
    assert explanation_blocks, "fixture must have an official explanation to clone"

    out = tmp / "run"
    (out / "segments").mkdir(parents=True)
    segment = {
        "item_id": "湖北__reading_b__01",
        "source_doc": src.name,
        "section": "reading_b",
        "display_section": "阅读B",
        "item_label": "阅读B",
        "question_text": "\n".join(QUESTION),
        "answer_key": [{"number": "24", "answer": "C"}, {"number": "25", "answer": "B"}],
        "answer_source": "答案区",
        "source_path": str(src),
        "source_blocks": [lo, hi],
        "official_explanation_blocks": explanation_blocks,
    }
    segment_path = out / "segments" / "item.json"
    segment_path.write_text(json.dumps(segment, ensure_ascii=False), encoding="utf-8")

    rows = [{
        "item_id": "湖北__reading_b__01",
        "source_doc": src.name,
        "section": "reading_b",
        "display_section": "阅读B",
        "item_label": "阅读B",
        "segment_path": str(segment_path),
        "explanation": EXPLANATION,
        "has_official_explanation": True,
    }]
    (out / "selected_items.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return out


def _text(path: Path) -> str:
    return db.read_docx(path).text


def test_teacher_edition_has_question_then_official_then_ai_in_that_order():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run_dir(Path(tmp))
        ex.export_selected(out, pipeline, log=lambda *_: None)
        text = _text(out / "docx_exports" / ex.TEACHER)

        question = text.index("What inspired Cui to design")
        official = text.index(ex.OFFICIAL_HEADING)
        detail = text.index("【24题详解】")
        ours = text.index(ex.AI_HEADING)
        assert question < official < detail < ours, "原题 → 官方答案与解析 → 详细解析和解答步骤"


def test_teacher_edition_carries_the_papers_own_explanation_verbatim():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run_dir(Path(tmp))
        ex.export_selected(out, pipeline, log=lambda *_: None)
        text = _text(out / "docx_exports" / ex.TEACHER)

        assert "【导语】本文主要讲述崔雅梦创办无障碍民宿" in text
        assert "细节理解题。根据第二段可知" in text
        assert "【25题详解】" in text


def test_teacher_edition_carries_our_explanation_including_the_wrong_options():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run_dir(Path(tmp))
        ex.export_selected(out, pipeline, log=lambda *_: None)
        text = _text(out / "docx_exports" / ex.TEACHER)

        assert "24. C　细节理解题" in text
        assert "为什么不选 B" in text, "the distractor analysis is the point of the AI pass"
        assert "同义替换" in text


def test_a_paper_that_never_explained_the_question_says_so_and_still_prints_the_answers():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run_dir(Path(tmp))
        segment_path = out / "segments" / "item.json"
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        segment["official_explanation_blocks"] = []  # 广东 skips four whole sections
        segment_path.write_text(json.dumps(segment, ensure_ascii=False), encoding="utf-8")

        ex.export_selected(out, pipeline, log=lambda *_: None)
        text = _text(out / "docx_exports" / ex.TEACHER)

        assert ex.NO_OFFICIAL in text
        assert "24: C" in text, "the answer key is known even when the explanation is not"
        assert ex.AI_HEADING in text, "our explanation is written either way"


def test_the_student_edition_is_untouched_by_any_of_this():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run_dir(Path(tmp))
        ex.export_selected(out, pipeline, log=lambda *_: None)
        text = _text(out / "docx_exports" / ex.STUDENT)

        assert "What inspired Cui to design" in text, "the question is still there"
        assert "第 1 篇" in text
        for forbidden in ("【24题详解】", "【导语】", ex.OFFICIAL_HEADING, ex.AI_HEADING, "来源：", "24—25. CB"):
            assert forbidden not in text, f"学生版混入了 {forbidden}"
        # And the gate that would have stopped the file shipping at all.
        ex.assert_student_edition_is_clean(out / "docx_exports" / ex.STUDENT)
