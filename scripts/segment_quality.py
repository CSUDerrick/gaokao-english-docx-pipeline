"""Shared structural quality rules for segmented exam papers.

This module intentionally has no third-party dependencies so the CLI, GUI,
quality report, and test suite all grade segmentation with the same rules.
"""

from __future__ import annotations

import json
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
    "One possible version", "Possible version", "参考范文", "范文",
    "评分标准", "评分原则", "写作要点", "内容要点",
    "【导语】", "【详解】", "【点睛】", "词汇积累", "句式拓展",
]


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
                section = str(row.get("section") or "")
                hard = [token for token in HARD_LEAK_TOKENS if token in question_text]
                if hard and section in {"continuation_writing", "practical_writing"}:
                    seg_cosmetic.append(f"尾部粘连答案/录音区: {', '.join(hard[:3])}")
                elif hard:
                    seg_structural.append(f"疑似含答案/录音边界词: {', '.join(hard[:3])}")
                soft = [token for token in SOFT_LAYOUT_TOKENS if token in question_text]
                if soft:
                    seg_cosmetic.append(f"含试卷版面提示: {', '.join(soft[:3])}")
                if section in {"practical_writing", "continuation_writing"}:
                    writing = [token for token in WRITING_LEAK_TOKENS if token in question_text]
                    if writing:
                        seg_cosmetic.append(f"含答案范文/解析标记: {', '.join(writing[:3])}")
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
