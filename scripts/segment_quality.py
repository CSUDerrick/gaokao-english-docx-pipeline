"""Shared structural quality rules for segmented exam papers.

This module intentionally has no third-party dependencies so the CLI, GUI,
quality report, and test suite all grade segmentation with the same rules.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


EXPECTED_SECTIONS = [
    "reading_a", "reading_b", "reading_c", "reading_d",
    "gap_filling", "cloze", "grammar",
    "practical_writing", "continuation_writing",
]

CRITICAL_SECTIONS = {
    "reading_a", "reading_b", "reading_c", "reading_d",
    "cloze", "grammar", "practical_writing", "continuation_writing",
}

SECTION_DISPLAY = {
    "reading_a": "阅读A", "reading_b": "阅读B", "reading_c": "阅读C",
    "reading_d": "阅读D", "gap_filling": "七选五", "cloze": "完形填空",
    "grammar": "语法填空", "practical_writing": "应用文",
    "continuation_writing": "读后续写", "unknown": "未识别",
}

HARD_LEAK_TOKENS = [
    "答案解析", "参考答案", "试题答案", "英语答案",
    "听力录音稿", "听力原文", "录音原文",
    "Text 1", "Text 2", "Text 3", "Text 4", "Text 5",
    "附：听力", "附听力", "第一节（共5小题）", "第二节（共15小题）",
]

SOFT_LAYOUT_TOKENS = ["答题卡", "考号", "准考证号"]

WRITING_LEAK_TOKENS = [
    "One possible version", "Possible version", "参考范文",
    "评分标准", "评分原则",
    "【导语】", "【详解】", "【点睛】",
]

# An answer run such as "21—23. CBC": a question range followed by exactly one
# letter per question. Nothing in a question body looks like this.
_ANSWER_RUN = re.compile(r"(?m)^\s*(\d{1,2})\s*[-—~－–]+\s*(\d{1,2})\s*[.．、:：]?\s*([A-G]{2,})\b")
_PER_QUESTION_EXPLANATION = re.compile(r"【\d+题详解】")


def _answer_runs(text: str):
    """The real answer runs in ``text``: a range whose letter count matches it exactly.

    "21—23" followed by three letters is a key. The same range followed by two, or by a
    word, is prose that happens to contain a dash.
    """
    for match in _ANSWER_RUN.finditer(text):
        first, last, letters = int(match.group(1)), int(match.group(2)), match.group(3)
        if last > first and len(letters) == last - first + 1:
            yield match


def answer_leaks(question_text: str) -> list[str]:
    """Name the answer-key evidence found inside a question body, if any."""
    found: list[str] = []
    for match in _answer_runs(question_text):
        found.append(f"答案键 {match.group(0).strip()}")
        break
    if _PER_QUESTION_EXPLANATION.search(question_text):
        found.append("逐题解析")
    return found


READING_SECTIONS = {"reading_a", "reading_b", "reading_c", "reading_d"}

# A reading question opens with a numbered stem: "24. What made ...". The fullwidth
# variants show up in OCR'd papers.
_STEM_TEMPLATE = r"(?m)^\s*%s\s*[.．、]"


def missing_question_numbers(segment: dict) -> list[str]:
    """Answer-key question numbers whose numbered stem is missing from the body.

    A reading passage prints its questions as "24. ..."; when the answer key names a
    question the body has no stem for, the *source paper* dropped it. One 华南师范 paper
    duplicated passage C's stems over passage B's, so 阅读B shipped as a passage with no
    questions at all and no warning anywhere — the code can't invent the missing stems,
    but it can refuse to pretend they are there.

    Restricted to the reading passages on purpose: 完形/语法填空 number their *blanks*
    inline ("____36____"), not as "N." stems, so counting stems there would cry wolf.
    """
    section = str(segment.get("section") or "")
    if section not in READING_SECTIONS:
        return []
    text = str(segment.get("question_text") or "")
    numbers: list[str] = []
    for entry in segment.get("answer_key") or []:
        number = str(entry.get("number") or "").strip()
        if number:
            numbers.append(number)
    return [n for n in numbers if not re.search(_STEM_TEMPLATE % re.escape(n), text)]


def first_answer_run(text: str) -> int | None:
    """Character offset of the first answer key in ``text``, or None.

    *Where* the key sits is what separates a 答案 document from a paper: a paper buries
    it in the tail, an answers document opens with it.
    """
    for match in _answer_runs(text):
        return match.start()
    return None


def load_segment(seg_path: str | Path) -> dict:
    path = Path(seg_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return json.loads(path.read_text(encoding="utf-8"))


def grade_doc(
    structural_issues: list[str],
    cosmetic_issues: list[str],
    seg_count: int,
    missing_critical: list[str],
) -> tuple[str, str]:
    if seg_count == 0 or len(missing_critical) >= 3:
        return "FAIL", "structural"
    if missing_critical or seg_count < 8 or seg_count > 12 or structural_issues:
        return "WARN", "structural"
    if cosmetic_issues:
        return "PASS", "tail-bleed"
    return "PASS", "clean"


def evaluate_document(doc: str, segs: list[dict]) -> dict:
    """Return the canonical quality result for one source document."""

    sections = [str(s.get("section") or "unknown") for s in segs]
    section_set = set(sections)
    missing = [s for s in EXPECTED_SECTIONS if s not in section_set]
    missing_critical = [s for s in CRITICAL_SECTIONS if s not in section_set]
    duplicates = [s for s, count in Counter(sections).items() if count > 1]
    structural_issues: list[str] = []
    cosmetic_issues: list[str] = []
    details: list[dict] = []

    for row in segs:
        try:
            char_count = int(row.get("char_count") or 0)
        except (TypeError, ValueError):
            char_count = 0
        seg_structural: list[str] = []
        seg_cosmetic: list[str] = []
        if char_count < 100:
            seg_structural.append(f"文本过短 ({char_count} 字符)")
        if char_count > 12000:
            seg_structural.append(f"文本过长 ({char_count} 字符) — 可能题型粘连")

        seg_path = row.get("segment_path", "")
        if seg_path:
            try:
                segment = load_segment(seg_path)
                question_text = str(segment.get("question_text") or "")
                # An answer inside a question body is never cosmetic: it is how
                # answers reached the *student* paper. The writing sections used
                # to be exempted here, which is exactly where the model essays
                # leaked through — so there is no exemption any more.
                leaks = answer_leaks(question_text)
                hard = [token for token in HARD_LEAK_TOKENS if token in question_text]
                if leaks or hard:
                    seg_structural.append(f"题干内含答案/解析: {', '.join((leaks + hard)[:3])}")
                missing_q = missing_question_numbers(segment)
                if missing_q:
                    seg_structural.append(f"阅读题目缺失: 缺第 {', '.join(missing_q)} 题（原卷未包含）")
                soft = [token for token in SOFT_LAYOUT_TOKENS if token in question_text]
                if soft:
                    seg_cosmetic.append(f"含试卷版面提示: {', '.join(soft[:3])}")
                writing = [token for token in WRITING_LEAK_TOKENS if token in question_text]
                if writing:
                    seg_structural.append(f"题干内含范文/解析标记: {', '.join(writing[:3])}")
            except Exception:
                seg_structural.append("segment JSON 读取失败")

        structural_issues.extend(seg_structural)
        cosmetic_issues.extend(seg_cosmetic)
        details.append({
            "section": row.get("section", "unknown"),
            "display": SECTION_DISPLAY.get(str(row.get("section") or "unknown"), str(row.get("section") or "unknown")),
            "label": row.get("item_label", ""),
            "chars": char_count,
            "path": seg_path,
            "structural": seg_structural,
            "cosmetic": seg_cosmetic,
        })

    if len(segs) < 8:
        structural_issues.append(f"segment 数量偏少 ({len(segs)} 个)")
    if len(segs) > 12:
        structural_issues.append(f"segment 数量偏多 ({len(segs)} 个)")
    if missing:
        structural_issues.append(f"缺少题型: {', '.join(missing)}")
    if duplicates:
        structural_issues.append(f"重复题型: {', '.join(duplicates)}")
    if not ({"practical_writing", "continuation_writing"} & section_set):
        structural_issues.append("缺少所有写作题型")

    grade, sub_grade = grade_doc(structural_issues, cosmetic_issues, len(segs), missing_critical)
    return {
        "doc": doc,
        "seg_count": len(segs),
        "sections": sections,
        "missing": missing,
        "missing_critical": missing_critical,
        "duplicates": duplicates,
        "structural_issues": structural_issues,
        "cosmetic_issues": cosmetic_issues,
        "grade": grade,
        "sub_grade": sub_grade,
        "needs_model_fallback": grade in {"WARN", "FAIL"},
        "details": details,
    }


def evaluate_rows(rows: list[dict]) -> list[dict]:
    by_doc: dict[str, list[dict]] = {}
    for row in rows:
        by_doc.setdefault(str(row.get("source_doc") or "unknown"), []).append(row)
    return [evaluate_document(doc, segs) for doc, segs in by_doc.items()]
