#!/usr/bin/env python3
"""Boundary repair for papers the local segmenter mis-split.

The local segmenter finds sections by keyword. Some papers label a section in a
way the regexes do not know — one real paper heads its 七选五 with nothing but
``第二节（共5小题；每小题2.5分，满分12.5分）``, no "七选五" and no "多余选项" — so
the section is missed and gets swallowed by the passage above it.

The old response was to throw the whole paper at the model and let it re-emit
every question. That was wrong twice over: the model paraphrases and abbreviates
(one run produced 44 items, several only 37 characters long), and re-emitted text
has no link back to the original paragraphs, so the export cannot clone the
teacher's formatting and the questions come out unstyled.

So the model is asked for one thing only: **which paragraph number does the
missing section start at?** It never produces content. The answer is a single
integer; the text is then cut from the original document exactly as the local
segmenter would have cut it, and cloning still works.
"""

from __future__ import annotations

import json
import re

from docx_blocks import DocxDoc

# What each section looks like, so the model knows what it is hunting for.
SECTION_HINTS: dict[str, tuple[str, str]] = {
    "reading_a": ("阅读A", "第一篇阅读理解短文，后面跟着若干四选一小题"),
    "reading_b": ("阅读B", "第二篇阅读理解短文"),
    "reading_c": ("阅读C", "第三篇阅读理解短文"),
    "reading_d": ("阅读D", "第四篇阅读理解短文"),
    "gap_filling": (
        "七选五",
        "一篇短文中有 5 个空（如 ____36____ 到 ____40____），后面给出 A-G 共 7 个选项，多余 2 个。"
        "标题可能只写「第二节」而不含「七选五」字样",
    ),
    "cloze": ("完形填空", "一篇短文中有约 15 个编号空，每空给出 A/B/C/D 四个选项"),
    "grammar": ("语法填空", "一篇短文中有约 10 个空，要求填入适当的词或所给词的正确形式"),
    "practical_writing": ("应用文", "要求写一篇书信/通知等应用文，通常 80 词左右"),
    "continuation_writing": ("读后续写", "给出一段故事，要求续写两段，通常 150 词左右"),
}

ORDER = [
    "reading_a", "reading_b", "reading_c", "reading_d",
    "gap_filling", "cloze", "grammar", "practical_writing", "continuation_writing",
]


def _host_range(segments: list[dict], missing: str) -> tuple[int, int] | None:
    """The block range the missing section must be hiding inside.

    Segments tile the paper in order, so a section that was never detected is
    still physically there — inside whichever detected segment runs across the
    place it should have started. That is the segment immediately before it in
    canonical order.
    """
    if missing not in ORDER:
        return None
    target = ORDER.index(missing)

    best: tuple[int, tuple[int, int]] | None = None
    for seg in segments:
        section = seg.get("section")
        blocks = seg.get("source_blocks") or []
        if section not in ORDER or len(blocks) != 2:
            continue
        rank = ORDER.index(section)
        if rank < target and (best is None or rank > best[0]):
            best = (rank, (blocks[0], blocks[1]))
    return best[1] if best else None


def build_prompt(doc: DocxDoc, lo: int, hi: int, missing: str) -> str:
    label, hint = SECTION_HINTS.get(missing, (missing, ""))
    lines = []
    for block in doc.blocks:
        if lo <= block.body_index < hi:
            text = block.text.strip()
            if text:
                lines.append(f"[{block.body_index}] {text[:110]}")

    listing = "\n".join(lines)
    return (
        "下面是一份高考英语试卷中一段连续的内容，每行开头方括号里是该段落的编号。\n\n"
        f"请找出「{label}」是从哪一个段落编号开始的。\n"
        f"「{label}」的特征：{hint}\n\n"
        "要求：\n"
        f"1. 只返回该题型**第一个段落**的编号（通常是它的小标题或指导语那一行）。\n"
        "2. 如果这段内容里根本没有这个题型，返回 null。\n"
        "3. 只返回 JSON，不要任何解释：{\"start_block\": 编号}\n\n"
        "内容：\n"
        f"{listing}\n"
    )


def parse_start_block(reply: str, lo: int, hi: int) -> int | None:
    """Read the block number back, refusing anything outside the region.

    A hallucinated or out-of-range index would cut the paper in the wrong place,
    so an unusable answer must fail closed rather than be applied.
    """
    text = (reply or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    value = None
    try:
        value = json.loads(text).get("start_block")
    except (json.JSONDecodeError, AttributeError):
        match = re.search(r'"start_block"\s*:\s*(null|\d+)', text)
        if match:
            value = None if match.group(1) == "null" else int(match.group(1))
        else:
            digits = re.search(r"\b(\d+)\b", text)
            value = int(digits.group(1)) if digits else None

    if value is None:
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None

    # Must land strictly inside the host range: equal to lo would mean the host
    # section itself starts there, which is not a split at all.
    if not (lo < index < hi):
        return None
    return index


def locate_missing_sections(
    doc: DocxDoc,
    segments: list[dict],
    missing: list[str],
    ask,
    log=print,
) -> list[tuple[int, str, str]]:
    """Return extra ``(char_pos, section, label)`` starts for the local segmenter.

    ``ask(prompt) -> str`` performs the model call. Returning char positions (not
    text) means the caller re-runs the ordinary local segmentation with one more
    boundary, so answer-key extraction and block ranges all follow as usual.
    """
    starts: list[tuple[int, str, str]] = []
    by_index = {b.body_index: b for b in doc.blocks}

    for section in missing:
        host = _host_range(segments, section)
        if host is None:
            log(f"  跳过 {section}：找不到它可能藏身的区间")
            continue

        lo, hi = host
        label = SECTION_HINTS.get(section, (section, ""))[0]
        log(f"  询问模型：「{label}」在段落 {lo}-{hi - 1} 之间从哪里开始？")

        index = parse_start_block(ask(build_prompt(doc, lo, hi, section)), lo, hi)
        if index is None:
            log(f"  模型未能定位「{label}」，保持本地切分结果")
            continue

        block = by_index.get(index)
        if block is None:
            log(f"  段落 {index} 不存在，忽略")
            continue

        log(f"  定位到「{label}」起始于段落 {index}：{block.text[:40]}")
        starts.append((block.char_start, section, label))

    return starts
