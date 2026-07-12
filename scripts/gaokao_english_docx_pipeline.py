#!/usr/bin/env python3
"""Batch pipeline for Gaokao English mock exam docx files.

DeepSeek's current official Python example uses the OpenAI SDK with:

    OpenAI(api_key=..., base_url="https://api.deepseek.com")
    client.chat.completions.create(
        model="deepseek-v4-pro",
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    )

This script follows that shape when the optional `openai` package is installed.
It also keeps a standard-library HTTP fallback so the prompt-generation workflow
can still run in lightweight environments. Search for "DEEPSEEK TUNING" below
to find the API parameters you are most likely to adjust.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import json
import os
import random
import re
import shutil
import sys
import time
import http.client
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from docx_blocks import DocxDoc, find_block_range, read_docx
from segment_quality import evaluate_document
from segment_repair import locate_missing_sections


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("reading_a", re.compile(r"(?im)^\s*(?:阅读理解|第一节|阅读)\s*(?:[\r\n ]+)?A\s*$|^\s*A\s*$")),
    ("reading_b", re.compile(r"(?im)^\s*B\s*$")),
    ("reading_c", re.compile(r"(?im)^\s*C\s*$")),
    ("reading_d", re.compile(r"(?im)^\s*D\s*$")),
    ("gap_filling", re.compile(r"(?im)七选五|选五|根据短文内容.*选项|选项中有两项(?:为)?多余选项")),
    ("cloze", re.compile(r"(?im)完形填空|完型填空|cloze")),
    ("grammar", re.compile(r"(?im)语法填空|短文填空|填入适当的单词|括号内单词的正确形式")),
    ("practical_writing", re.compile(r"(?im)应用文|书面表达|写一封|投稿|通知|邀请信|建议信")),
    ("continuation_writing", re.compile(r"(?im)读后续写|续写|Paragraph 1|Paragraph 2")),
]

SECTION_DISPLAY = {
    "reading_a": "阅读A",
    "reading_b": "阅读B",
    "reading_c": "阅读C",
    "reading_d": "阅读D",
    "gap_filling": "七选五",
    "cloze": "完形填空",
    "grammar": "语法填空",
    "practical_writing": "应用文",
    "continuation_writing": "读后续写",
    "unknown": "未识别",
}

SECTION_ALIASES = {
    "阅读A": "reading_a",
    "阅读A篇": "reading_a",
    "reading_a": "reading_a",
    "A": "reading_a",
    "阅读B": "reading_b",
    "阅读B篇": "reading_b",
    "reading_b": "reading_b",
    "B": "reading_b",
    "阅读C": "reading_c",
    "阅读C篇": "reading_c",
    "reading_c": "reading_c",
    "C": "reading_c",
    "阅读D": "reading_d",
    "阅读D篇": "reading_d",
    "reading_d": "reading_d",
    "D": "reading_d",
    "七选五": "gap_filling",
    "gap_filling": "gap_filling",
    "完形填空": "cloze",
    "完型填空": "cloze",
    "cloze": "cloze",
    "语法填空": "grammar",
    "grammar": "grammar",
    "应用文": "practical_writing",
    "practical_writing": "practical_writing",
    "读后续写": "continuation_writing",
    "作文续写": "continuation_writing",
    "continuation_writing": "continuation_writing",
}

SELECTION_TARGETS = {
    "reading_a": 2,
    "reading_b": 2,
    "reading_c": 2,
    "reading_d": 2,
    "gap_filling": 2,
    "cloze": 2,
    "grammar": 2,
    "practical_writing": 2,
    "continuation_writing": 2,
}

SEGMENT_PROMPT_VERSION = "segment_v1"
SCORE_PROMPT_VERSION = "score_v1"
REVIEW_SELECT_PROMPT_VERSION = "review_select_v1"
ENRICH_PROMPT_VERSION = "enrich_selected_v1"


@dataclass
class Item:
    item_id: str
    source_doc: str
    section: str
    item_label: str
    char_count: int
    wordish_count: int
    text: str


@dataclass
class ChatResult:
    content: str
    reasoning: str = ""
    usage: dict | None = None
    client_used: str = ""


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return time.strftime("%H:%M:%S")


def log(args: argparse.Namespace, message: str) -> None:
    if not getattr(args, "quiet", False):
        print(f"[{now_stamp()}] {message}", flush=True)


def file_size_label(path: Path) -> str:
    if not path.exists():
        return "missing"
    size = path.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def rough_token_estimate(text: str) -> int:
    """Cheap mixed Chinese/English token estimate for preflight cost checks."""

    return max(1, len(text) // 3)


def preview_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n... [truncated, {len(text) - max_chars} chars hidden]"


def show_terminal_text(args: argparse.Namespace, title: str, text: str, mode: str) -> None:
    if getattr(args, "quiet", False) or mode == "none":
        return
    if not text:
        log(args, f"{title}: not returned by API")
        return
    body = text if mode == "full" else preview_text(text, args.preview_chars)
    print(f"\n----- {title} -----\n{body}\n----- end {title} -----\n", flush=True)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def reset_output_dir(args: argparse.Namespace) -> None:
    """Remove generated outputs so an interrupted run can restart cleanly.

    This intentionally resets only the folder passed through --out. It does not
    touch input_docx, config, scripts, README, or the virtual environment.
    """

    out_dir = Path(args.out).resolve()
    project_dir = Path.cwd().resolve()
    input_path = Path(args.input).resolve()
    protected = {
        Path("/").resolve(),
        project_dir,
        project_dir / "input_docx",
        project_dir / "scripts",
        project_dir / "config",
        project_dir / ".venv",
        input_path,
    }
    if out_dir in protected:
        raise SystemExit(f"Refusing to initialize protected path: {out_dir}")
    if not is_relative_to(out_dir, project_dir):
        raise SystemExit(f"Refusing to initialize path outside this project: {out_dir}")

    log(args, f"Initializing output directory: {out_dir}")
    if out_dir.exists():
        shutil.rmtree(out_dir)
        log(args, "  removed old generated outputs and checkpoints.")
    ensure_dir(out_dir)
    log(args, "  created clean output directory.")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def node_text(node: ET.Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        tag = local_name(child.tag)
        if tag == "t" and child.text:
            parts.append(child.text)
        elif tag == "tab":
            parts.append("\t")
        elif tag in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def extract_docx_text(path: Path) -> str:
    """Flat text of a docx.

    Thin wrapper over ``docx_blocks.read_docx``, which produces byte-identical
    text but also keeps each paragraph's original OOXML node so the export can
    clone the source typesetting instead of rebuilding it.
    """
    return read_docx(path).text


def normalize_inline_text(text: str) -> str:
    text = unescape(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def find_section_starts(text: str) -> list[tuple[int, str, str]]:
    starts: list[tuple[int, str, str]] = []
    for key, pattern in SECTION_PATTERNS:
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end == -1:
                line_end = len(text)
            label = text[line_start:line_end].strip()[:80]
            starts.append((line_start, key, label))

    dedup: dict[int, tuple[int, str, str]] = {}
    for start, key, label in sorted(starts, key=lambda x: (x[0], section_order(x[1]))):
        dedup.setdefault(start, (start, key, label))
    return sorted(dedup.values(), key=lambda x: x[0])


def section_order(section: str) -> int:
    order = {
        "reading_a": 0,
        "reading_b": 1,
        "reading_c": 2,
        "reading_d": 3,
        "gap_filling": 4,
        "cloze": 5,
        "grammar": 6,
        "practical_writing": 7,
        "continuation_writing": 8,
    }
    return order.get(section, 99)


def split_doc_into_items(source_doc: str, text: str) -> list[Item]:
    starts = find_section_starts(text)
    chunks: list[tuple[str, str, str]] = []

    if not starts:
        for idx, chunk in enumerate(split_by_size(text, max_chars=4500), start=1):
            chunks.append(("unknown", f"自动分段{idx}", chunk))
    else:
        for idx, (start, section, label) in enumerate(starts):
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(text)
            chunk = text[start:end].strip()
            if len(chunk) >= 200:
                chunks.append((section, label or SECTION_DISPLAY.get(section, section), chunk))

    items: list[Item] = []
    counters: dict[str, int] = {}
    for section, label, chunk in chunks:
        for sub_idx, sub_chunk in enumerate(split_oversized_section(chunk, section), start=1):
            counters[section] = counters.get(section, 0) + 1
            label_suffix = "" if sub_idx == 1 else f"-{sub_idx}"
            item_label = f"{label}{label_suffix}"
            item_id = f"{safe_stem(source_doc)}_{section}_{counters[section]:02d}"
            items.append(
                Item(
                    item_id=item_id,
                    source_doc=source_doc,
                    section=SECTION_DISPLAY.get(section, section),
                    item_label=item_label,
                    char_count=len(sub_chunk),
                    wordish_count=count_wordish(sub_chunk),
                    text=sub_chunk,
                )
            )
    return items


def split_oversized_section(text: str, section: str) -> list[str]:
    max_chars = 6500 if section.startswith("reading") else 7500
    if len(text) <= max_chars:
        return [text]
    return split_by_size(text, max_chars=max_chars)


def split_by_size(text: str, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def count_wordish(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", text))


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "", stem)
    return stem[:80] or "doc"


def safe_filename(text: str, max_len: int = 120) -> str:
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "", text)
    return (text[:max_len] or "item").strip("_") or "item"


def normalize_section(section: str) -> str:
    section = (section or "").strip()
    if section in SECTION_ALIASES:
        return SECTION_ALIASES[section]
    lowered = section.lower()
    return SECTION_ALIASES.get(lowered, "unknown")


def section_display(section_key: str) -> str:
    return SECTION_DISPLAY.get(section_key, section_key or "未识别")


def write_json(path: Path, data: dict | list) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def score_number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


NO_ANSWER_MARKER = "原卷未提供答案"


def answer_key_text(answer_key: object) -> str:
    if not answer_key:
        return "未识别"
    if isinstance(answer_key, str):
        return answer_key
    if isinstance(answer_key, list):
        parts: list[str] = []
        for item in answer_key:
            if isinstance(item, dict):
                number = item.get("number") or item.get("question_number") or ""
                answer = item.get("answer") or item.get("key") or ""
                if number or answer:
                    parts.append(f"{number}: {answer}".strip(": "))
            else:
                parts.append(str(item))
        return "; ".join(parts) or "未识别"
    return json.dumps(answer_key, ensure_ascii=False)


def segment_body(segment: dict) -> str:
    for key in ["question_text", "text", "raw_text", "content"]:
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(segment, ensure_ascii=False)


def _find_answer_section_start(text: str) -> int | None:
    """Return the character position where the answer section begins, or None."""
    # --- Stage 1: answer header on its own line ---
    # Covers: "参考答案", "答案解析", "英语答案", "听力原文", "听力录音稿",
    # "参考答案及评分标准", "部分试题详解", etc.
    _ANSWER_HEADER_LINE = (
        r"(?im)^\s*"
        r"(?:"
        r"【?(?:英语|英语试题)?(?:参考)?答案(?:解析|及评分标准|与解析)?【?|"
        r"【?试题答案【?|"
        r"【?英语答案【?|"
        r"【?答案(?:解析|及解析|与解析)?【?|"
        r"【?(?:附[：:]?\s*)?听力(?:原文|录音稿|录音材料|录音原文)【?|"
        r"【?录音原文【?|"
        r"部分试题详解|"
        r"附[：:]?\s*听力(?:\s*原文)?|"
        r"参考答案及评分标准|"
        r"答案及评分标准"
        r")"
        r"\s*[:：]?\s*$"
    )
    line_candidates = [match.start() for match in re.finditer(_ANSWER_HEADER_LINE, text)]

    # --- Stage 2: answer header at end of a line (inline form) ---
    # e.g. "...故事结尾 ___ 安徽A10联盟...英语答案解析"
    # e.g. "...壮行考试参考答案及评分标准"
    _ANSWER_HEADER_INLINE = (
        r"(?im)"
        r"(?:"
        r"(?:英语|英语试题)?(?:参考)?答案(?:解析|及解析|与解析|及评分标准)?|"
        r"听力(?:原文|录音稿|录音材料)|"
        r"参考答案及评分标准|"
        r"答案及评分标准"
        r")"
        r"\s*[:：]?\s*$"
    )
    inline_candidates: list[int] = []
    for match in re.finditer(_ANSWER_HEADER_INLINE, text):
        candidate = match.start()
        after = text[candidate : candidate + 300]
        if re.search(r"\d+[-—~]\d+\s*[A-G]|\d+\.[A-G]\b|^\d+\.\s*\w+", after, re.M):
            inline_candidates.append(candidate)

    if line_candidates or inline_candidates:
        return min(line_candidates + inline_candidates)

    # --- Stage 2.5: model-essay / transcript boundary markers ---
    # These patterns indicate the answer section has started even without
    # a formal header.  Only match in the latter 60 % of the document to avoid
    # false positives when "One possible version" or "听力原文" appear inside
    # a reading comprehension passage.
    _TAIL_MARKERS = (
        r"(?im)^\s*"
        r"(?:"
        r"One possible version\s*[:：]?|"
        r"Possible version\s*[:：]?|"
        r"【?参考范文】?\s*[:：]?|"
        r"【?听力(?:原文|录音稿|录音材料|录音原文)】?|"
        r"【?录音原文】?|"
        r"【?(?:听力\s*)?答案(?:解析|详解)?】?|"
        r"附[：:]?\s*听力(?:\s*原文)?|"
        r"【?解题导语】?|"
        r"写作\s*(?:第一节|第二节).*参考范文|"
        r"评分标准|"
        r"评分原则"
        r")"
        r"\s*$"
    )
    tail60_start = max(0, len(text) * 2 // 5)  # last 60 %
    tail_matches = list(re.finditer(_TAIL_MARKERS, text[tail60_start:]))
    if tail_matches:
        for tm in tail_matches:
            candidate = tail60_start + tm.start()
            marker_text = text[candidate:candidate + 50]
            # Check whether this is a *strong* answer-section signal.
            strong_signal = re.search(
                r"(?im)^\s*(?:One possible version|Possible version|参考范文|范文|"
                r"听力原文|听力录音稿|听力录音材料|录音原文|"
                r"评分标准|评分原则|内容要点)",
                marker_text,
            )
            if strong_signal:
                # Model-essay markers ("One possible version", "参考范文")
                # can appear for *either* the application-writing or the
                # continuation-writing section.  We must NOT cut at the
                # application-writing model essay if the continuation-writing
                # question text follows it (some papers place both writing
                # model essays in sequence).
                after_candidate = text[candidate:]
                # Only "读后续写", "续写", or "Paragraph 1/2" are specific
                # to continuation-writing.  Bare "第二节" appears in answer
                # explanations (七选五/语法填空 analysis) and would cause
                # false positives.
                has_cw_after = bool(re.search(
                    r"(?im)^\s*(?:读后续写|续写|Paragraph\s*[12])",
                    after_candidate[200:],
                ))
                if not has_cw_after:
                    line_start = text.rfind("\n", 0, candidate) + 1
                    return line_start
                # If continuation-writing question IS after this marker,
                # skip it and keep looking for the next match (which should
                # be the continuation-writing model essay or transcript).
                continue
            # Listening-transcript / rubric markers: always safe to cut.
            if re.search(
                r"(?im)(?:听力原文|听力录音稿|听力录音材料|录音原文|"
                r"评分标准|评分原则|内容要点)",
                marker_text,
            ):
                line_start = text.rfind("\n", 0, candidate) + 1
                return line_start
            # For weaker markers, verify they are preceded by a writing
            # prompt or a blank line.
            before_match = text[max(0, candidate - 200):candidate]
            if re.search(r"(?im)(?:读后续写|第二节.*续写|写作.*第二节)\s*$", before_match):
                line_start = text.rfind("\n", 0, candidate) + 1
                return line_start
            if candidate >= 2 and text[candidate - 2:candidate] == "\n\n":
                line_start = text.rfind("\n", 0, candidate) + 1
                return line_start

    # --- Stage 3: answer-range lines in the last 25 % ---
    tail_start = max(0, len(text) - max(1, len(text) // 4))
    answer_line = re.search(r"(?m)^\s*\d+[-—~]\d+\s*[.．]?\s*[A-G]+", text[tail_start:])
    if answer_line:
        answer_line_pos = tail_start + answer_line.start()
        before = text[:answer_line_pos]
        header = re.search(r"(?im)(?:英语)?(?:参考答案|答案解析|试题答案|英语答案|听力原文|听力录音稿|录音原文|答案及评分标准)[^\n]{0,30}$", before)
        if header:
            return header.start()
        return answer_line_pos

    return None


def extract_answer_tail(text: str, max_chars: int = 8000) -> str:
    start = _find_answer_section_start(text)
    if start is None:
        return ""
    return text[start : start + max_chars].strip()


def trim_answer_tail_from_text(text: str) -> str:
    start = _find_answer_section_start(text)
    if start is None:
        return text
    return text[:start].strip()


def line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        start = pos
        end = pos + len(line)
        spans.append((start, end, line.strip()))
        pos = end
    return spans


def find_line_index(lines: list[tuple[int, int, str]], pattern: str, start: int = 0) -> int | None:
    regex = re.compile(pattern, re.I)
    for idx in range(start, len(lines)):
        if regex.search(lines[idx][2]):
            return idx
    return None


def find_standalone_letter(lines: list[tuple[int, int, str]], letter: str, start: int = 0) -> int | None:
    regex = re.compile(rf"^\s*{re.escape(letter)}\s*$", re.I)
    for idx in range(start, len(lines)):
        if regex.match(lines[idx][2]):
            return idx
    return None


def parse_answer_tokens(answer_tail: str) -> dict[int, str]:
    """Parse answers from a compact answer-key block.

    Handles range separators: ``-``, ``—`` (em-dash), ``~`` (tilde),
    and ``--`` (double-hyphen, common in some papers).
    Also handles single-number answers such as ``21. B``.
    """
    answers: dict[int, str] = {}
    # Range answers: "21-23 BDC", "21—23. BDB", "21~23DBC", "41--45. DADBA"
    range_patterns = [
        re.compile(r"(\d+)\s*[-—]\s*(\d+)\s*[.．]?\s*([A-G]+)"),
        re.compile(r"(\d+)\s*~\s*(\d+)\s*[.．]?\s*([A-G]+)"),
        re.compile(r"(\d+)\s*--\s*(\d+)\s*[.．]?\s*([A-G]+)"),
    ]
    for pattern in range_patterns:
        for match in pattern.finditer(answer_tail):
            start, end, letters = int(match.group(1)), int(match.group(2)), match.group(3).strip()
            for offset, number in enumerate(range(start, end + 1)):
                if offset < len(letters) and number not in answers:
                    answers[number] = letters[offset]
    # Single-number answers: "21. B" or "21．B"
    for match in re.finditer(r"(?<!\d)(\d+)\s*[.．]\s*([A-G])\b(?!\s*[.．])", answer_tail):
        number = int(match.group(1))
        if number not in answers:
            answers[number] = match.group(2)
    return answers


def parse_grammar_answers(answer_tail: str) -> dict[int, str]:
    """Parse grammar-fill answers (questions 56-65).

    Handles both well-spaced (``56. marking 57. shows``) and concatenated
    formats (``56. would spark57. playfully58.with``).
    """
    answers: dict[int, str] = {}
    # Pre-process: insert a space before each grammar question number so that
    # concatenated answers like "spark57." become "spark 57."
    normalized = re.sub(r"(?<!\d)(5[6-9]|6[0-5])\s*\.", r" \1. ", answer_tail)
    # Now parse: number, dot, then everything until the next number-dot or end
    for match in re.finditer(
        r"(5[6-9]|6[0-5])\s*[.．]\s*(.+?)(?=\s*(?:5[6-9]|6[0-5])\s*[.．]|$)",
        normalized,
    ):
        number = int(match.group(1))
        if number not in answers:
            answer = match.group(2).strip()
            # Trim trailing punctuation / stray characters
            answer = answer.rstrip(".,;，。；、")
            if answer:
                answers[number] = answer
    # Fallback: single-word grammar answers like "56. celebrated"
    for match in re.finditer(r"(5[6-9]|6[0-5])\s*[.．]\s*(\S+)", answer_tail):
        number = int(match.group(1))
        if number not in answers:
            answers[number] = match.group(2).rstrip(".,;，。；、")
    return answers


def extract_all_answers_from_full_text(text: str) -> dict[int, str]:
    """Scan the FULL paper text for answers using every known format.

    This is more aggressive than the tail-only approach and is designed for
    the ``repair-answers`` mode where we re-read the original extracted text.
    """
    answers: dict[int, str] = {}

    # --- multiple-choice answers (Q21-55) ---
    answers.update(parse_answer_tokens(text))

    # --- grammar-fill answers (Q56-65) ---
    answers.update(parse_grammar_answers(text))

    # --- table-format answers (成都七中 style) ---
    # Lines like "21~23 CDB 24~27 CBAC" or "41~45 CBDBA" or "21~23DBC" (no space)
    for match in re.finditer(r"(\d+)\s*~\s*(\d+)\s*[.．]?\s*([A-G]+)", text):
        start, end, letters = int(match.group(1)), int(match.group(2)), match.group(3).strip()
        for offset, number in enumerate(range(start, end + 1)):
            if offset < len(letters) and number not in answers:
                answers[number] = letters[offset]

    # --- explanation-embedded / double-hyphen format (江南十校 style) ---
    # "32—35 CACC" with double-hyphen variant "41--45. DADBA"
    for match in re.finditer(r"(\d+)\s*--\s*(\d+)\s*[.．]?\s*([A-G]+)", text):
        start, end, letters = int(match.group(1)), int(match.group(2)), match.group(3).strip()
        for offset, number in enumerate(range(start, end + 1)):
            if offset < len(letters) and number not in answers:
                answers[number] = letters[offset]

    return answers


def answer_range(answer_tail: str, start: int, end: int) -> list[dict]:
    parsed = parse_answer_tokens(answer_tail)
    return [{"number": str(number), "answer": parsed[number]} for number in range(start, end + 1) if number in parsed]


def grammar_answer_range(answer_tail: str) -> list[dict]:
    parsed = parse_grammar_answers(answer_tail)
    return [{"number": str(number), "answer": answer} for number, answer in sorted(parsed.items())]


def trim_writing_answer_extras(text: str) -> str:
    """Remove answer-book appendices accidentally captured after writing samples."""

    if not text:
        return ""
    stop_patterns = [
        r"(?im)^\s*听力录音稿\s*$",
        r"(?im)^\s*Text\s*1\b",
        r"(?im)^\s*(?:A|B|C|D|七选五|完形填空|语法填空)\s*\n\s*【解题导语】",
        r"(?im)^\s*【解题导语】",
        r"(?im)^\s*答案解析\s*$",
        r"(?im)^\s*试题解析\s*$",
    ]
    earliest: int | None = None
    for pattern in stop_patterns:
        match = re.search(pattern, text)
        if match and match.start() > 0:
            earliest = match.start() if earliest is None else min(earliest, match.start())
    if earliest is not None:
        text = text[:earliest]
    return text.strip()


def writing_answer_text(answer_tail: str, continuation: bool) -> str:
    if not answer_tail:
        return ""
    if continuation:
        idx = answer_tail.find("第二节")
        return trim_writing_answer_extras(answer_tail[idx:]) if idx >= 0 else ""
    idx = answer_tail.find("第一节")
    if idx < 0:
        return ""
    end = answer_tail.find("第二节", idx + 1)
    chunk = answer_tail[idx:end] if end >= 0 else answer_tail[idx:]
    return trim_writing_answer_extras(chunk)


def make_local_segment(
    source_doc: str,
    section: str,
    item_label: str,
    question_text: str,
    answer_key: list[dict] | str,
    answer_source: str,
    source_path: str = "",
    source_blocks: list[int] | None = None,
) -> dict:
    return {
        "source_doc": source_doc,
        "section": section,
        "display_section": section_display(section),
        "item_label": item_label,
        "title": "",
        "question_text": question_text.strip(),
        "questions": [],
        "answer_key": answer_key,
        "answer_source": answer_source,
        "confidence": 0.88 if question_text.strip() else 0.0,
        "prompt_version": "local_segment_v1",
        # Half-open range of w:body children this question occupies in the source
        # file. The export clones these nodes verbatim to keep the teacher's own
        # typesetting (fonts, indents, images). Empty for AI-segmented or OCR
        # input, where no original OOXML exists to point at.
        "source_path": source_path,
        "source_blocks": source_blocks or [],
    }


def trim_answer_tail_with_offset(text: str) -> tuple[str, int]:
    """``trim_answer_tail_from_text`` plus how far the result shifted.

    The trimmed body is what the segmenter measures offsets against, but the
    block map is indexed against the *untrimmed* text. ``.strip()`` can drop
    leading characters, so return the delta rather than assuming it is zero.
    """
    start = _find_answer_section_start(text)
    raw = text if start is None else text[:start]
    lead = len(raw) - len(raw.lstrip())
    return raw.strip(), lead


def local_segment_paper(
    source_doc: str,
    text: str,
    doc: DocxDoc | None = None,
    extra_starts: list[tuple[int, str, str]] | None = None,
) -> list[dict]:
    """Split a paper into questions.

    ``extra_starts`` are section boundaries the keyword rules missed, recovered by
    :mod:`segment_repair` asking the model for a paragraph number. They are fed
    back in here — rather than being patched on afterwards — so the recovered
    section gets its answer key and block range by exactly the same code as every
    other section.
    """
    body, body_offset = trim_answer_tail_with_offset(text)
    answer_tail = extract_answer_tail(text, max_chars=30000)
    # Use full-text scan for robust answer extraction (handles table format,
    # double-hyphen ranges, concatenated grammar answers, etc.)
    full_answers = extract_all_answers_from_full_text(text)
    lines = line_spans(body)
    starts: list[tuple[int, str, str]] = []

    read_start = find_line_index(lines, r"阅读理解|阅读下列短文|第二部分\s*阅读")
    search_from = read_start or 0
    a = find_standalone_letter(lines, "A", search_from)
    b = find_standalone_letter(lines, "B", (a or search_from) + 1)
    c = find_standalone_letter(lines, "C", (b or search_from) + 1)
    d = find_standalone_letter(lines, "D", (c or search_from) + 1)
    for idx, section, label in [(a, "reading_a", "阅读A"), (b, "reading_b", "阅读B"), (c, "reading_c", "阅读C"), (d, "reading_d", "阅读D")]:
        if idx is not None:
            starts.append((lines[idx][0], section, label))

    def add_start(section: str, label: str, idx: int | None) -> int | None:
        if idx is not None:
            starts.append((lines[idx][0], section, label))
        return idx

    gap_idx = add_start("gap_filling", "七选五", find_line_index(lines, r"七选五|选项中有两项(?:为)?多余选项"))
    cloze_idx = find_line_index(lines, r"完形填空|完型填空|语言运用", (gap_idx or 0) + 1)
    if cloze_idx is None:
        cloze_idx = find_line_index(lines, r"第一节.*共\s*15\s*小题.*[每各]小题\s*1\s*分", (gap_idx or 0) + 1)
    if cloze_idx is None:
        cloze_idx = find_line_index(lines, r"第一节.*共\s*15\s*小题.*[每各]题\s*1\s*分", (gap_idx or 0) + 1)
    cloze_idx = add_start("cloze", "完形填空", cloze_idx)
    grammar_idx = find_line_index(lines, r"语法填空|在空白处填入\s*1\s*个适当的单词|在空白处填入适当的内容", (cloze_idx or gap_idx or 0) + 1)
    if grammar_idx is None:
        grammar_idx = find_line_index(lines, r"第二节.*共\s*10\s*(?:小)?题.*[每各](?:小)?题\s*1\.?5\s*分", (cloze_idx or gap_idx or 0) + 1)
    if grammar_idx is None:
        grammar_idx = find_line_index(lines, r"第二节.*共\s*10\s*(?:小)?题.*满分\s*15\s*分", (cloze_idx or gap_idx or 0) + 1)
    grammar_idx = add_start("grammar", "语法填空", grammar_idx)
    writing_idx = find_line_index(lines, r"第四部分\s*写作|写作[（(]共两节")
    practical_idx = find_line_index(lines, r"应用文写作|第一节\s*应用文|第一节\s*写作", writing_idx or 0)
    if practical_idx is None and writing_idx is not None:
        practical_idx = find_line_index(lines, r"第一节.*满分\s*15\s*分", writing_idx)
    practical_idx = add_start("practical_writing", "应用文", practical_idx)
    continuation_idx = find_line_index(lines, r"读后续写|第二节\s*读后续写", (practical_idx or writing_idx or 0) + 1)
    if continuation_idx is None and writing_idx is not None:
        continuation_idx = find_line_index(lines, r"第二节.*满分\s*25\s*分", (practical_idx or writing_idx) + 1)
    add_start("continuation_writing", "读后续写", continuation_idx)

    for pos, section, label in extra_starts or []:
        # char offsets from the repair step index the full text; the segmenter
        # measures against the answer-trimmed body.
        adjusted = pos - body_offset
        if 0 <= adjusted < len(body):
            starts.append((adjusted, section, label))

    starts = sorted({(pos, section): (pos, section, label) for pos, section, label in starts}.values(), key=lambda x: x[0])
    segments: list[dict] = []
    answer_ranges = {
        "reading_a": (21, 23),
        "reading_b": (24, 27),
        "reading_c": (28, 31),
        "reading_d": (32, 35),
        "gap_filling": (36, 40),
        "cloze": (41, 55),
    }
    # Determine the global answer-source label for this paper.
    if not full_answers:
        global_answer_source = "原卷未提供答案"
    else:
        global_answer_source = "答案区"

    for i, (pos, section, label) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        qtext = body[pos:end].strip()
        if len(qtext) < 80:
            continue
        if section in answer_ranges:
            start_q, end_q = answer_ranges[section]
            ak_list = [{"number": str(n), "answer": full_answers[n]}
                       for n in range(start_q, end_q + 1) if n in full_answers]
            answer_source = global_answer_source if ak_list else ("未识别" if full_answers else NO_ANSWER_MARKER)
            ak = ak_list if ak_list else (NO_ANSWER_MARKER if not full_answers else [])
        elif section == "grammar":
            ak_list = [{"number": str(n), "answer": full_answers[n]}
                       for n in range(56, 66) if n in full_answers]
            answer_source = global_answer_source if ak_list else ("未识别" if full_answers else NO_ANSWER_MARKER)
            ak = ak_list if ak_list else (NO_ANSWER_MARKER if not full_answers else [])
        elif section == "practical_writing":
            ak = writing_answer_text(answer_tail, continuation=False)
            answer_source = "答案区/范文" if ak else (NO_ANSWER_MARKER if not full_answers else "未识别")
            ak = ak if ak else (NO_ANSWER_MARKER if not full_answers else "")
        elif section == "continuation_writing":
            ak = writing_answer_text(answer_tail, continuation=True)
            answer_source = "答案区/范文" if ak else (NO_ANSWER_MARKER if not full_answers else "未识别")
            ak = ak if ak else (NO_ANSWER_MARKER if not full_answers else "")
        else:
            ak = NO_ANSWER_MARKER if not full_answers else []
            answer_source = NO_ANSWER_MARKER if not full_answers else "未识别"

        # Map this question's char span back to the w:body children it came from,
        # so export can clone the original paragraphs. Offsets are measured
        # against the trimmed body, hence the shift back into full-text space.
        source_blocks: list[int] = []
        source_path = ""
        if doc is not None:
            lo, hi = doc.body_range(pos + body_offset, end + body_offset)
            source_blocks = [lo, hi]
            source_path = str(doc.path)

        segments.append(
            make_local_segment(
                source_doc, section, label, qtext, ak, answer_source,
                source_path=source_path, source_blocks=source_blocks,
            )
        )
    return segments


def read_prompt_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def render_analysis_prompt(template: str, item: Item) -> str:
    return (
        template.replace("{{SOURCE_DOC}}", item.source_doc)
        .replace("{{SECTION}}", item.section)
        .replace("{{ITEM_LABEL}}", item.item_label)
        .replace("{{TEXT}}", item.text)
    )


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def compact_final_record(row: dict) -> dict:
    """Keep only fields needed for final cross-item selection.

    Interrupted or verbose runs can leave large `reasoning`, `usage`, and raw
    response metadata in model_analyses.jsonl. Those are useful for debugging,
    but they should not be sent back to the model during final selection.
    """

    analysis = row.get("analysis", {})
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except json.JSONDecodeError:
            analysis = {"raw_analysis": analysis}
    if not isinstance(analysis, dict):
        analysis = {}

    keep_analysis_keys = [
        "topic",
        "topic_category",
        "novelty_score",
        "difficulty_score",
        "vocabulary_value_score",
        "grammar_value_score",
        "exam_value_score",
        "recommendation_score",
        "suitable_for_intensive_teaching",
        "core_high_frequency_words",
        "familiar_words_new_meanings",
        "difficult_or_low_frequency_words",
        "topic_words",
        "word_formation_and_grammar",
        "long_difficult_sentences",
        "exam_skills",
        "main_difficulty_sources",
        "best_fit_selection_bucket",
        "selection_reason",
        "classroom_suggestion",
    ]
    compact_analysis = {key: analysis.get(key) for key in keep_analysis_keys if key in analysis}
    return {
        "item_id": row.get("item_id", ""),
        "source_doc": row.get("source_doc", analysis.get("source_doc", "")),
        "section": row.get("section", analysis.get("section", "")),
        "item_label": row.get("item_label", analysis.get("item_label", "")),
        "analysis": compact_analysis,
    }


def build_final_material(analyses_path: Path, args: argparse.Namespace) -> str:
    if args.final_input == "full":
        log(args, "Final input mode: full model_analyses.jsonl")
        return analyses_path.read_text(encoding="utf-8")

    rows = read_jsonl(analyses_path)
    compact_rows = [compact_final_record(row) for row in rows]
    compact_path = analyses_path.with_name("model_analyses.final_compact.jsonl")
    write_jsonl(compact_path, compact_rows)
    log(args, f"Final input mode: compact ({len(compact_rows)} records).")
    log(args, f"  compact final material: {compact_path} ({file_size_label(compact_path)})")
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in compact_rows)


def flatten_analysis(row: dict) -> dict:
    analysis = row.get("analysis")
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except json.JSONDecodeError:
            analysis = {"raw_analysis": analysis}
    if not isinstance(analysis, dict):
        analysis = {}

    out = {
        "item_id": row.get("item_id", ""),
        "source_doc": row.get("source_doc", analysis.get("source_doc", "")),
        "section": row.get("section", analysis.get("section", "")),
        "item_label": row.get("item_label", analysis.get("item_label", "")),
        "topic": analysis.get("topic", ""),
        "topic_category": analysis.get("topic_category", ""),
        "novelty_score": analysis.get("novelty_score", ""),
        "difficulty_score": analysis.get("difficulty_score", ""),
        "vocabulary_value_score": analysis.get("vocabulary_value_score", ""),
        "grammar_value_score": analysis.get("grammar_value_score", ""),
        "exam_value_score": analysis.get("exam_value_score", ""),
        "recommendation_score": analysis.get("recommendation_score", ""),
        "best_fit_selection_bucket": analysis.get("best_fit_selection_bucket", ""),
        "selection_reason": analysis.get("selection_reason", ""),
        "classroom_suggestion": analysis.get("classroom_suggestion", ""),
    }
    for key in [
        "core_high_frequency_words",
        "familiar_words_new_meanings",
        "difficult_or_low_frequency_words",
        "topic_words",
        "word_formation_and_grammar",
        "long_difficult_sentences",
        "exam_skills",
        "main_difficulty_sources",
    ]:
        value = analysis.get(key, "")
        out[key] = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return out


def call_chat_completion(
    prompt: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    client_mode: str,
    reasoning_effort: str,
    thinking: str,
    timeout: int,
    max_tokens: int | None = None,
    max_retries: int = 3,
) -> ChatResult:
    if client_mode in {"auto", "sdk"}:
        try:
            return call_chat_completion_sdk(
                prompt,
                base_url=base_url,
                api_key=api_key,
                model=model,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
                timeout=timeout,
                max_tokens=max_tokens,
                max_retries=max_retries,
            )
        except ImportError:
            if client_mode == "sdk":
                raise RuntimeError(
                    "The OpenAI SDK is not installed. Install it with `pip3 install openai`, "
                    "or rerun with `--client http` to use the built-in HTTP fallback."
                )

    return call_chat_completion_http(
        prompt,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        timeout=timeout,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )


def chat_payload(
    prompt: str,
    *,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    max_tokens: int | None = None,
) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨的高三英语教研分析助手。请严格遵守用户要求输出。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "stream": False,
    }
    if reasoning_effort != "none":
        payload["reasoning_effort"] = reasoning_effort
    if thinking != "omit":
        payload["thinking"] = {"type": thinking}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return payload


def is_retryable_status(status_code: int | None) -> bool:
    if status_code is None:
        return True
    return status_code in {408, 409, 425, 429} or status_code >= 500


def retry_after_seconds(value: object) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def retry_delay_seconds(attempt: int, retry_after: object = None) -> float:
    explicit = retry_after_seconds(retry_after)
    if explicit is not None:
        return min(explicit, 90.0)
    base = min(2 ** max(0, attempt - 1), 60)
    return base + random.uniform(0.2, 1.2)


def exception_status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def exception_retry_after(exc: Exception) -> object:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None


def call_chat_completion_sdk(
    prompt: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    timeout: int,
    max_tokens: int | None,
    max_retries: int,
) -> ChatResult:
    # DEEPSEEK TUNING:
    # Official docs use the OpenAI SDK and set base_url to the API root, e.g.
    # https://api.deepseek.com, not https://api.deepseek.com/chat/completions.
    # You can change model/reasoning/thinking through CLI flags defined below.
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    kwargs = chat_payload(
        prompt,
        model=model,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        thinking="omit",
        max_tokens=max_tokens,
    )
    if thinking != "omit":
        # The OpenAI SDK forwards DeepSeek-specific fields through extra_body.
        kwargs["extra_body"] = {"thinking": {"type": thinking}}

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(**kwargs)
            message = response.choices[0].message
            usage = response.usage.model_dump() if getattr(response, "usage", None) else None
            return ChatResult(
                content=message.content or "",
                reasoning=getattr(message, "reasoning_content", "") or getattr(message, "reasoning", "") or "",
                usage=usage,
                client_used="sdk",
            )
        except Exception as exc:
            last_error = exc
            status_code = exception_status_code(exc)
            if not is_retryable_status(status_code):
                break
            if attempt < max_retries:
                time.sleep(retry_delay_seconds(attempt, exception_retry_after(exc)))
    raise RuntimeError(f"SDK API call failed after {max_retries} attempts: {last_error}")


def completion_endpoint(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def call_chat_completion_http(
    prompt: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    timeout: int,
    max_tokens: int | None,
    max_retries: int = 3,
) -> ChatResult:
    # HTTP fallback mirrors the official curl example. It accepts either the API
    # root URL or the full /chat/completions endpoint for convenience.
    payload = chat_payload(
        prompt,
        model=model,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        max_tokens=max_tokens,
    )
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    endpoint = completion_endpoint(base_url)
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                message = body["choices"][0]["message"]
                return ChatResult(
                    content=message.get("content") or "",
                    reasoning=message.get("reasoning_content") or message.get("reasoning") or "",
                    usage=body.get("usage"),
                    client_used="http",
                )
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            last_error = f"HTTP {exc.code} {exc.reason}: {error_body or '<empty response body>'}"
            if not is_retryable_status(exc.code):
                break
            if attempt < max_retries:
                time.sleep(retry_delay_seconds(attempt, exc.headers.get("Retry-After")))
        except (urllib.error.URLError, http.client.IncompleteRead, http.client.RemoteDisconnected, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(retry_delay_seconds(attempt))
    raise RuntimeError(f"API call failed after {max_retries} attempts: {last_error}")


def parse_model_json(content: str) -> dict | str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    salvaged = salvage_truncated_json(content)
    if salvaged is not None:
        return salvaged
    return content


def salvage_truncated_json(content: str) -> dict | None:
    """Recover the complete objects from a response that hit the token cap.

    A long paper can push the model past ``--segment-max-tokens``, cutting the
    reply mid-string. Everything before the cut is still perfectly good, so keep
    the segments that did finish rather than discarding the whole call.
    """
    start = content.find("{")
    if start == -1 or '"segments"' not in content:
        return None

    items = content[content.find("[", start) + 1 :]
    segments: list[dict] = []
    depth = 0
    begin = None
    in_string = False
    escaped = False

    for i, ch in enumerate(items):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin is not None:
                with contextlib.suppress(json.JSONDecodeError):
                    segments.append(json.loads(items[begin : i + 1]))
                begin = None

    if not segments:
        return None

    doc_match = re.search(r'"source_doc"\s*:\s*"([^"]*)"', content)
    return {"source_doc": doc_match.group(1) if doc_match else "", "segments": segments, "_truncated": True}


def build_final_selection_prompt(analyses_path: Path | None = None) -> str:
    suffix = ""
    if analyses_path:
        suffix = f"\n\n材料来源：请读取或粘贴 `{analyses_path}` 中的全部单篇分析结果。\n"
    return f"""你是一名熟悉高三英语备考、模拟题命题趋势和高考英语阅读难度分析的教研老师。

现在我已经完成了多份英语模拟题的单篇标注评分。请你基于全部结果做横向筛选，形成教师备课用材料清单。

请输出：

1. 全部重难点阅读词汇汇总
- 按话题分类
- 标出高频词、熟词生义、难词、写作可迁移词
- 给出中文释义和简短例句

2. 语法与词汇变形汇总
- 词性转换
- 派生词
- 非谓语动词
- 从句
- 谓语动词
- 长难句
- 其他高频考点

3. 各题型最终推荐篇目
- 阅读A：2篇新题材
- 阅读B：2篇最高难度
- 阅读C：2篇难度高且题材新
- 阅读D：2篇难度高且题材新
- 七选五：2篇题材新颖
- 完形填空：2篇题材新颖
- 语法填空：2篇题型较新
- 应用文：2篇出题角度新颖
- 读后续写：2篇出题角度新颖

4. 每篇推荐内容请包括：
- 来源试卷
- 题型与篇目编号
- 推荐理由
- 适合课堂讲解的词汇
- 适合课堂讲解的语法/解题点
- 建议使用方式：精讲/限时训练/课后拓展/拔高训练

筛选原则：
- 阅读A优先题材新颖、适合拓展，不一定选最难。
- 阅读B优先难度最高、推理和长难句价值高。
- 阅读C/D同时看难度和题材新颖度。
- 七选五看篇章结构、衔接逻辑和空格区分度。
- 完形看语篇逻辑、词汇辨析、情感线和主题升华。
- 语法填空看考点分布、语境新颖性和二轮复习价值。
- 应用文和读后续写看真实情境、出题角度和写作训练价值。
{suffix}"""


def build_segment_prompt(source_doc: str, text: str) -> str:
    # Cache-friendly design: keep this long instruction stable and put the
    # variable document text only at the end.
    return f"""任务版本：{SEGMENT_PROMPT_VERSION}

你是一名高考英语试卷结构化整理助手。请把一份高考英语模拟卷文本按题型切割成独立题目单元。

请严格输出 JSON，不要输出 Markdown，不要添加解释性前后缀。JSON 顶层结构：
{{
  "source_doc": "试卷文件名",
  "segments": [
    {{
      "section": "reading_a/reading_b/reading_c/reading_d/gap_filling/cloze/grammar/practical_writing/continuation_writing",
      "display_section": "阅读A/阅读B/阅读C/阅读D/七选五/完形填空/语法填空/应用文/读后续写",
      "item_label": "篇目或题号标签",
      "title": "题目标题或主题，无法判断则为空",
      "question_text": "完整题目文本，必须包含文章、题干、选项、写作要求等学生需要看到的内容",
      "questions": [
        {{"number": "题号", "stem": "题干", "options": {{"A": "选项", "B": "选项"}}}}
      ],
      "answer_key": [
        {{"number": "题号", "answer": "答案"}}
      ],
      "answer_source": "答案来自原文/答案区/未识别",
      "confidence": 0.0
    }}
  ],
  "warnings": ["无法确定或可能切错的地方"]
}}

切割要求：
- 只保留这些题型：阅读A、阅读B、阅读C、阅读D、七选五、完形填空、语法填空、应用文、读后续写。
- 每个 segment 必须尽量包含对应题目的题目正文和答案。
- 如果答案统一在试卷末尾，请尽量匹配到对应题号；无法匹配时 answer_key 为空，并在 warnings 说明。
- 不要输出听力、页眉页脚、学校声明、无关说明。
- 如果阅读A/B/C/D在原文中只写 A/B/C/D，请根据顺序归入 reading_a/b/c/d。
- 七选五选项也要放在 question_text 中。
- 应用文和读后续写要分别切开。
- confidence 取 0 到 1，表示你对切割准确性的信心。

试卷文件名：{source_doc}

试卷文本如下：
{text}
"""


def build_score_prompt(segment: dict) -> str:
    # Cache-friendly design: stable rubric first, variable segment JSON last.
    return f"""任务版本：{SCORE_PROMPT_VERSION}

你是一名熟悉高三英语备考、模拟题命题趋势和高考英语阅读难度分析的教研老师。
请对一个已经切割好的题目单元做“轻量质量评分”。这是第一轮大批量筛选，请保持输出简短。

请严格输出 JSON，不要输出 Markdown，不要添加解释性前后缀。不要在输出中复述完整题目原文，不要输出词汇表、长难句清单或详细语法讲解。

JSON 顶层结构：
{{
  "item_id": "题目ID",
  "source_doc": "来源试卷",
  "section": "题型",
  "item_label": "篇目或题号",
  "topic": "主题",
  "topic_category": "科技/环保/教育/心理/文化/社会/健康/人物/应用文/写作/其他",
  "novelty_score": 1,
  "difficulty_score": 1,
  "vocabulary_value_score": 1,
  "grammar_value_score": 1,
  "exam_value_score": 1,
  "writing_angle_novelty_score": 1,
  "recommendation_score": 1,
  "suitable_for_intensive_teaching": "适合/一般/不太适合",
  "exam_skills": ["最多3个能力标签"],
  "main_difficulty_sources": ["最多3个难度来源"],
  "best_fit_selection_bucket": "新题材/高难度/题型新/写作角度新/不优先选择",
  "selection_reason": "不超过80字，说明后续筛选是否值得入选",
  "classroom_suggestion": "不超过50字，建议精讲/限时训练/课后拓展/拔高训练",
  "score_summary": "不超过50字，一句话概括本题价值"
}}

评分说明：
- 1 = 很低，2 = 略低，3 = 中等，4 = 较高，5 = 很高。
- 阅读A重点看题材新颖度和拓展价值。
- 阅读B重点看难度、推理、长难句和选项干扰。
- 阅读C/D同时看难度和题材新颖度。
- 七选五重点看篇章结构、衔接逻辑、指代和空格区分度。
- 完形填空重点看语篇逻辑、词汇辨析、情感线和主题升华。
- 语法填空重点看考点分布、语境新颖性和二轮复习价值。
- 应用文和读后续写重点看真实情境、出题角度和写作训练价值。

题目单元 JSON：
{json.dumps(segment, ensure_ascii=False)}
"""


def build_review_select_prompt(candidates: list[dict], target_count: int, section: str) -> str:
    return f"""任务版本：{REVIEW_SELECT_PROMPT_VERSION}

你是一名高三英语教研组长。请在程序按评分初筛出的候选题中，做最终人工式复核选择。
请严格输出 JSON，不要输出 Markdown，不要添加解释性前后缀。

JSON 顶层结构：
{{
  "section": "{section_display(section)}",
  "selected_item_ids": ["item_id"],
  "review_reason": "整体选择理由",
  "items": [
    {{"item_id": "item_id", "decision": "select/reject", "reason": "选择或不选择的理由"}}
  ]
}}

要求：
- 最终必须选择 {target_count} 个 item_id；如果候选不足，则全部选择。
- 阅读A优先题材新颖和拓展价值，不一定最难。
- 阅读B优先最高难度、推理价值、长难句和选项干扰。
- 阅读C/D同时看难度和题材新颖度。
- 七选五看篇章结构、衔接逻辑、指代和空格区分度。
- 完形看题材新颖、语篇逻辑、词汇辨析、情感线和主题升华。
- 语法填空看题型设置新、考点分布合理和复习价值。
- 应用文/读后续写看真实情境、出题角度和写作训练价值。
- 不要因为来源学校名气选择，必须看评分和理由。

候选材料只包含评分摘要，不包含完整题目，以节省 token：
{json.dumps(candidates, ensure_ascii=False)}
"""


def build_enrich_prompt(segment: dict, score: dict) -> str:
    return f"""任务版本：{ENRICH_PROMPT_VERSION}

你是一名高三英语教研老师。请只对“最终入选”的题目补充详细讲解材料。
请严格输出 JSON，不要输出 Markdown，不要添加解释性前后缀。

JSON 顶层结构：
{{
  "item_id": "题目ID",
  "core_high_frequency_words": [
    {{"word": "英文词汇", "meaning": "中文释义", "context_meaning": "文中语境含义", "teaching_reason": "适合讲解的原因"}}
  ],
  "familiar_words_new_meanings": [
    {{"word": "英文词汇", "meaning": "中文释义", "context_meaning": "文中语境含义", "teaching_reason": "适合讲解的原因"}}
  ],
  "difficult_or_low_frequency_words": [
    {{"word": "英文词汇", "meaning": "中文释义", "context_meaning": "文中语境含义", "teaching_reason": "适合讲解的原因"}}
  ],
  "topic_words": [
    {{"word": "英文词汇", "meaning": "中文释义", "context_meaning": "文中语境含义", "teaching_reason": "适合讲解的原因"}}
  ],
  "word_formation_and_grammar": [
    {{"type": "词性转换/派生词/非谓语/从句/长难句/其他", "evidence": "原句或关键词", "teaching_point": "考点说明"}}
  ],
  "long_difficult_sentences": [
    {{"sentence": "原句", "structure_analysis": "结构分析", "teaching_point": "讲解价值"}}
  ],
  "teaching_notes": "不超过150字的课堂讲解建议"
}}

数量要求：
- 每类词汇最多8项。
- 语法/词形变化最多8项。
- 长难句最多3句。
- 不要重复题目原文，只引用必要短句。

评分摘要：
{json.dumps(score, ensure_ascii=False)}

题目单元 JSON：
{json.dumps(segment, ensure_ascii=False)}
"""


def save_api_conversation(
    out_dir: Path,
    kind: str,
    item_id: str,
    prompt: str,
    chat_result: ChatResult,
    args: argparse.Namespace,
) -> None:
    if not getattr(args, "save_conversations", True):
        return
    conversation_dir = out_dir / "api_conversations" / kind
    ensure_dir(conversation_dir)
    path = conversation_dir / f"{safe_filename(item_id)}.md"
    content = [
        f"# {kind}: {item_id}",
        "",
        "## API",
        "",
        f"- model: `{getattr(args, kind + '_model', getattr(args, 'model', ''))}`",
        f"- client: `{args.client}`",
        f"- base_url: `{args.base_url}`",
        "",
        "## Prompt",
        "",
        "```text",
        prompt,
        "```",
        "",
        "## Reasoning",
        "",
        "```text",
        chat_result.reasoning or "",
        "```",
        "",
        "## Output",
        "",
        "```text",
        chat_result.content or "",
        "```",
        "",
        "## Usage",
        "",
        "```json",
        json.dumps(chat_result.usage or {}, ensure_ascii=False, indent=2),
        "```",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def collect_docx(input_dir: Path) -> list[Path]:
    if input_dir.is_file() and input_dir.suffix.lower() == ".docx":
        return [input_dir]
    return sorted(p for p in input_dir.rglob("*.docx") if not p.name.startswith("~$"))


def collect_pdf(input_dir: Path) -> list[Path]:
    if input_dir.is_file() and input_dir.suffix.lower() == ".pdf":
        return [input_dir]
    if input_dir.is_file():
        return []
    return sorted(p for p in input_dir.rglob("*.pdf") if not p.name.startswith("~$"))


def convert_pdfs(args: argparse.Namespace, out_dir: Path) -> list[Path]:
    """OCR any PDFs into .docx so the rest of the pipeline sees only Word files.

    The converted files land under the output directory, never next to the
    teacher's originals — input is never written to.
    """
    pdfs = collect_pdf(Path(args.input))
    if not pdfs:
        return []

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pdf_ingest

    converted_dir = out_dir / "pdf_converted"
    ensure_dir(converted_dir)
    converted: list[Path] = []
    for pdf in pdfs:
        target = converted_dir / f"{pdf.stem}.docx"
        if target.exists():
            log(args, f"  reusing existing OCR result for {pdf.name}")
            converted.append(target)
            continue
        log(args, f"  OCR {pdf.name} via PaddleOCR-VL …")
        converted.append(
            pdf_ingest.ingest_pdf(
                pdf, converted_dir, base_url=args.paddle_base_url, token=args.paddle_token
            )
        )
    return converted


def call_stage_model(
    args: argparse.Namespace,
    prompt: str,
    *,
    model: str,
    reasoning_effort: str,
    thinking: str,
    max_tokens: int | None = None,
) -> ChatResult:
    return call_chat_completion(
        prompt,
        base_url=args.base_url,
        api_key=args.api_key or os.environ.get(args.api_key_env, ""),
        model=model,
        temperature=args.temperature,
        client_mode=args.client,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        timeout=args.timeout,
        max_tokens=max_tokens,
        max_retries=args.max_retries,
    )


def flatten_score(row: dict) -> dict:
    score = row.get("score", {})
    if isinstance(score, str):
        try:
            score = json.loads(score)
        except json.JSONDecodeError:
            score = {"raw_score": score}
    if not isinstance(score, dict):
        score = {}
    return {
        "item_id": row.get("item_id", score.get("item_id", "")),
        "source_doc": row.get("source_doc", score.get("source_doc", "")),
        "section": row.get("section", score.get("section", "")),
        "display_section": row.get("display_section", ""),
        "item_label": row.get("item_label", score.get("item_label", "")),
        "topic": score.get("topic", ""),
        "topic_category": score.get("topic_category", ""),
        "novelty_score": score.get("novelty_score", ""),
        "difficulty_score": score.get("difficulty_score", ""),
        "vocabulary_value_score": score.get("vocabulary_value_score", ""),
        "grammar_value_score": score.get("grammar_value_score", ""),
        "exam_value_score": score.get("exam_value_score", ""),
        "writing_angle_novelty_score": score.get("writing_angle_novelty_score", ""),
        "recommendation_score": score.get("recommendation_score", ""),
        "best_fit_selection_bucket": score.get("best_fit_selection_bucket", ""),
        "score_summary": score.get("score_summary", ""),
        "selection_reason": score.get("selection_reason", ""),
        "classroom_suggestion": score.get("classroom_suggestion", ""),
    }


def rough_segment_units(source_doc: str, text: str, args: argparse.Namespace) -> list[dict]:
    """Create local rough chunks before calling the model.

    This reduces tokens for large papers. We append a shared answer tail to each
    rough chunk so the model can still attach answers when answers are collected
    at the end of the paper.
    """

    if args.segment_input == "full":
        return [{"unit_id": safe_stem(source_doc), "label": "full_paper", "text": text}]

    answer_tail = extract_answer_tail(text, max_chars=args.answer_tail_chars)
    rough_items = split_doc_into_items(source_doc, text)
    if len(rough_items) <= 1:
        return [{"unit_id": safe_stem(source_doc), "label": "full_paper", "text": text}]

    units: list[dict] = []
    for idx, item in enumerate(rough_items, start=1):
        chunk = item.text
        if answer_tail and answer_tail not in chunk:
            chunk = f"{chunk}\n\n【统一答案区，供匹配题号使用】\n{answer_tail}"
        units.append(
            {
                "unit_id": f"{safe_stem(source_doc)}__rough_{idx:02d}",
                "label": f"{item.section}-{item.item_label}",
                "text": chunk,
            }
        )
    return units


def segment_docx_file(
    docx: Path,
    args: argparse.Namespace,
    out_dir: Path,
    extra_starts: list[tuple[int, str, str]] | None = None,
) -> list[dict]:
    extracted_dir = out_dir / "extracted_text"
    segments_dir = out_dir / "segments"
    rough_dir = out_dir / "rough_segments"
    ensure_dir(extracted_dir)
    ensure_dir(segments_dir)
    ensure_dir(rough_dir)

    source_doc = docx.name
    doc = read_docx(docx)
    text = doc.text
    text_path = extracted_dir / f"{safe_stem(source_doc)}.txt"
    text_path.write_text(text, encoding="utf-8")

    if args.segment_input == "local":
        segments = local_segment_paper(source_doc, text, doc, extra_starts)
        write_json(rough_dir / f"{safe_stem(source_doc)}__local_segments.json", segments)
        counters: dict[str, int] = {}
        rows: list[dict] = []
        for segment in segments:
            section_key = normalize_section(str(segment.get("section") or segment.get("display_section") or "unknown"))
            counters[section_key] = counters.get(section_key, 0) + 1
            item_id = f"{safe_stem(source_doc)}__{section_key}__{counters[section_key]:02d}"
            item_label = str(segment.get("item_label") or f"{section_display(section_key)}{counters[section_key]}")
            segment.update(
                {
                    "item_id": item_id,
                    "source_doc": source_doc,
                    "section": section_key,
                    "display_section": section_display(section_key),
                    "item_label": item_label,
                    "rough_unit": "local",
                    "rough_unit_index": 0,
                    "prompt_version": "local_segment_v1",
                }
            )
            segment_path = segments_dir / f"{safe_filename(item_id)}.json"
            write_json(segment_path, segment)
            rows.append(
                {
                    "item_id": item_id,
                    "source_doc": source_doc,
                    "section": section_key,
                    "display_section": section_display(section_key),
                    "item_label": item_label,
                    "title": segment.get("title", ""),
                    "char_count": len(segment_body(segment)),
                    "answer_count": len(segment.get("answer_key") or []),
                    "confidence": segment.get("confidence", ""),
                    "rough_unit": "local",
                    "segment_path": str(segment_path),
                }
            )
        return rows

    units = rough_segment_units(source_doc, text, args)
    write_json(rough_dir / f"{safe_stem(source_doc)}__rough_units.json", units)

    counters: dict[str, int] = {}
    rows: list[dict] = []

    for unit_idx, unit in enumerate(units, start=1):
        prompt = build_segment_prompt(f"{source_doc}｜{unit['label']}", unit["text"])
        chat_result = call_stage_model(
            args,
            prompt,
            model=args.segment_model,
            reasoning_effort=args.segment_reasoning_effort,
            thinking=args.segment_thinking,
            max_tokens=args.segment_max_tokens,
        )
        save_api_conversation(out_dir, "segment", unit["unit_id"], prompt, chat_result, args)

        parsed = parse_model_json(chat_result.content)
        raw_path = segments_dir / f"{safe_filename(unit['unit_id'])}__raw_segment_response.json"
        if isinstance(parsed, dict):
            write_json(raw_path, parsed)
        else:
            raw_path.write_text(str(parsed), encoding="utf-8")
            raise RuntimeError(f"Segment JSON parse failed for {source_doc} / {unit['label']}; raw response saved to {raw_path}")

        for segment in parsed.get("segments", []):
            if not isinstance(segment, dict):
                continue
            section_key = normalize_section(str(segment.get("section") or segment.get("display_section") or "unknown"))
            counters[section_key] = counters.get(section_key, 0) + 1
            item_id = f"{safe_stem(source_doc)}__{section_key}__{counters[section_key]:02d}"
            item_label = str(segment.get("item_label") or f"{section_display(section_key)}{counters[section_key]}")
            # AI segmentation re-emits the text instead of reporting offsets, so
            # anchor it back onto the source paragraphs — without a block range
            # the export cannot clone the original formatting and would drop the
            # question from the Word file entirely.
            source_blocks = find_block_range(doc, str(segment.get("question_text") or ""))
            if not source_blocks:
                log(args, f"  WARNING: could not locate {item_label} in {source_doc}; it will keep AI text but lose original formatting.")

            segment.update(
                {
                    "item_id": item_id,
                    "source_doc": source_doc,
                    "section": section_key,
                    "display_section": section_display(section_key),
                    "item_label": item_label,
                    "rough_unit": unit["label"],
                    "rough_unit_index": unit_idx,
                    "prompt_version": SEGMENT_PROMPT_VERSION,
                    "source_path": str(docx),
                    "source_blocks": source_blocks,
                }
            )
            segment_path = segments_dir / f"{safe_filename(item_id)}.json"
            write_json(segment_path, segment)
            rows.append(
                {
                    "item_id": item_id,
                    "source_doc": source_doc,
                    "section": section_key,
                    "display_section": section_display(section_key),
                    "item_label": item_label,
                    "title": segment.get("title", ""),
                    "char_count": len(segment_body(segment)),
                    "answer_count": len(segment.get("answer_key") or []),
                    "confidence": segment.get("confidence", ""),
                    "rough_unit": unit["label"],
                    "segment_path": str(segment_path),
                }
            )
    return rows


def run_segment(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    fallback_report_path = out_dir / "segment_fallback_report.json"
    if args.segment_input == "local" and fallback_report_path.exists():
        fallback_report_path.unlink()
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if args.segment_input != "local" and not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")

    docx_files = collect_docx(Path(args.input)) + convert_pdfs(args, out_dir)
    if not docx_files:
        raise SystemExit(f"No .docx or .pdf files found under {args.input}")

    if args.segment_input == "local":
        log(args, f"Segmenting {len(docx_files)} docx file(s) locally; no segment API calls will be made.")
    else:
        log(args, f"Segmenting {len(docx_files)} docx file(s) with {args.segment_model}; workers={args.segment_workers}.")
    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.segment_workers)) as executor:
        future_map = {executor.submit(segment_docx_file, docx, args, out_dir): docx for docx in docx_files}
        for future in concurrent.futures.as_completed(future_map):
            docx = future_map[future]
            try:
                doc_rows = future.result()
                rows.extend(doc_rows)
                log(args, f"  segmented {docx.name}: {len(doc_rows)} item(s).")
            except Exception as exc:
                log(args, f"  segment failed for {docx.name}: {exc}")
                raise

    fallback_records: list[dict] = []
    if args.segment_input == "local" and args.segment_warning_fallback:
        rows_by_doc: dict[str, list[dict]] = {}
        for row in rows:
            rows_by_doc.setdefault(row["source_doc"], []).append(row)
        quality_by_doc = {
            doc: evaluate_document(doc, doc_rows)
            for doc, doc_rows in rows_by_doc.items()
        }
        needs_fallback = {
            doc: result
            for doc, result in quality_by_doc.items()
            if result["needs_model_fallback"]
        }
        if needs_fallback:
            if not api_key:
                summary = "; ".join(
                    f"{doc}: {', '.join(result['structural_issues'][:2])}"
                    for doc, result in needs_fallback.items()
                )
                raise SystemExit(
                    "Local segmentation produced structural WARN/FAIL and model fallback requires an API key. "
                    f"Problems: {summary}. Set {args.api_key_env}, or use --no-segment-warning-fallback for local diagnostics only."
                )
            log(args, f"{len(needs_fallback)} 份试卷本地切分有结构疑点；只就疑点处向模型询问边界，正文仍从原卷截取。")
            docx_by_name = {path.name: path for path in docx_files}
            replacement_rows: dict[str, list[dict]] = {}

            def repair_paper(doc_name: str) -> list[dict]:
                """Ask the model only where the missing section starts, then re-cut locally."""
                path = docx_by_name[doc_name]
                parsed = read_docx(path)
                segments = [read_json(Path(r["segment_path"])) for r in rows_by_doc[doc_name]]
                missing = needs_fallback[doc_name].get("missing") or []
                if not missing:
                    return []

                def ask(prompt: str) -> str:
                    return call_stage_model(
                        args, prompt,
                        model=args.segment_model,
                        reasoning_effort=args.segment_reasoning_effort,
                        thinking=args.segment_thinking,
                        max_tokens=200,  # the reply is a single number
                    ).content

                extra = locate_missing_sections(
                    parsed, segments, missing, ask, log=lambda m: log(args, m)
                )
                if not extra:
                    return []
                return segment_docx_file(path, args, out_dir, extra_starts=extra)

            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.segment_workers)) as executor:
                future_map = {executor.submit(repair_paper, doc): doc for doc in needs_fallback}
                for future in concurrent.futures.as_completed(future_map):
                    doc = future_map[future]
                    try:
                        repaired = future.result()
                    except Exception as exc:  # noqa: BLE001
                        # Repair is an attempt to *improve* a paper the local pass
                        # already handled. Its failure must not take the run down.
                        log(args, f"  边界修复失败（{doc}）：{type(exc).__name__}: {exc}")
                        log(args, f"  保留 {doc} 的本地切分结果。")
                        repaired = []

                    if not repaired:
                        fallback_records.append({
                            "source_doc": doc,
                            "local_grade": needs_fallback[doc]["grade"],
                            "local_issues": needs_fallback[doc]["structural_issues"],
                            "fallback_mode": "boundary",
                            "model": args.segment_model,
                            "final_grade": needs_fallback[doc]["grade"],
                            "final_issues": needs_fallback[doc]["structural_issues"],
                            "final_segment_count": len(rows_by_doc.get(doc, [])),
                        })
                        continue

                    quality = evaluate_document(doc, repaired)
                    replacement_rows[doc] = repaired
                    fallback_records.append({
                        "source_doc": doc,
                        "local_grade": needs_fallback[doc]["grade"],
                        "local_issues": needs_fallback[doc]["structural_issues"],
                        "fallback_mode": "boundary",
                        "model": args.segment_model,
                        "final_grade": quality["grade"],
                        "final_issues": quality["structural_issues"],
                        "final_segment_count": len(repaired),
                    })
                    log(args, f"  边界修复后 {doc}：{len(repaired)} 道题，评级={quality['grade']}。")

            final_rows: list[dict] = []
            unresolved: list[dict] = []
            for doc, local_rows in rows_by_doc.items():
                if doc not in replacement_rows:
                    final_rows.extend(local_rows)
                    continue
                model_rows = replacement_rows[doc]
                model_paths = {str(row.get("segment_path") or "") for row in model_rows}
                for old_row in local_rows:
                    old_path = Path(str(old_row.get("segment_path") or ""))
                    if str(old_path) not in model_paths and old_path.exists():
                        old_path.unlink()
                final_rows.extend(model_rows)
                final_quality = evaluate_document(doc, model_rows)
                if final_quality["needs_model_fallback"]:
                    unresolved.append(final_quality)
            rows = final_rows
            write_json(fallback_report_path, fallback_records)

            # A remaining WARN is advisory: the questions were still cut from the
            # original paper and still carry their block ranges, so they score and
            # clone fine — the teacher just gets told which paper to eyeball. Only
            # a FAIL (nothing usable came out) is worth stopping for.
            failed = [item for item in unresolved if item["grade"] == "FAIL"]
            if failed:
                summary = "; ".join(
                    f"{item['doc']}: {', '.join(item['structural_issues'][:2])}" for item in failed
                )
                raise SystemExit(f"切分失败，已停止以免浪费 API 额度。{summary}")
            for item in unresolved:
                log(args, f"  注意：{item['doc']} 仍有结构疑点（{', '.join(item['structural_issues'][:2])}），已继续处理，建议人工抽查。")

    rows.sort(key=lambda r: (r["source_doc"], section_order(r["section"]), r["item_id"]))
    segment_index = out_dir / "segment_index.jsonl"
    segment_csv = out_dir / "segment_index.csv"
    write_jsonl(segment_index, rows)
    write_csv(
        segment_csv,
        rows,
        ["item_id", "source_doc", "section", "display_section", "item_label", "title", "char_count", "answer_count", "confidence", "rough_unit", "segment_path"],
    )
    log(args, f"Segment outputs written: {segment_index} ({file_size_label(segment_index)}), {segment_csv} ({file_size_label(segment_csv)})")
    return rows


def load_segment_rows(out_dir: Path) -> list[dict]:
    index_path = out_dir / "segment_index.jsonl"
    if not index_path.exists():
        raise SystemExit(f"Missing {index_path}. Run --mode segment first.")
    return read_jsonl(index_path)


def score_one_segment(row: dict, args: argparse.Namespace, out_dir: Path) -> dict:
    scores_dir = out_dir / "scores"
    ensure_dir(scores_dir)
    item_id = row["item_id"]
    score_path = scores_dir / f"{safe_filename(item_id)}.json"
    if score_path.exists() and not args.force:
        existing = read_json(score_path)
        if isinstance(existing, dict):
            return existing

    segment = read_json(Path(row["segment_path"]))
    if not isinstance(segment, dict):
        raise RuntimeError(f"Invalid segment file for {item_id}")

    prompt = build_score_prompt(segment)
    chat_result = call_stage_model(
        args,
        prompt,
        model=args.score_model,
        reasoning_effort=args.score_reasoning_effort,
        thinking=args.score_thinking,
        max_tokens=args.score_max_tokens,
    )
    save_api_conversation(out_dir, "score", item_id, prompt, chat_result, args)
    parsed = parse_model_json(chat_result.content)
    score = parsed if isinstance(parsed, dict) else {"raw_score": str(parsed)}
    score.setdefault("item_id", item_id)
    score.setdefault("source_doc", row.get("source_doc", ""))
    score.setdefault("section", row.get("display_section", row.get("section", "")))
    score.setdefault("item_label", row.get("item_label", ""))
    result = {
        "item_id": item_id,
        "source_doc": row.get("source_doc", ""),
        "section": row.get("section", ""),
        "display_section": row.get("display_section", ""),
        "item_label": row.get("item_label", ""),
        "segment_path": row.get("segment_path", ""),
        "score_path": str(score_path),
        "score": score,
        "usage": chat_result.usage,
        "client_used": chat_result.client_used,
        "prompt_version": SCORE_PROMPT_VERSION,
    }
    write_json(score_path, result)
    return result


def run_score(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out)
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")
    rows = load_segment_rows(out_dir)
    log(args, f"Scoring {len(rows)} segment(s) with {args.score_model}; workers={args.score_workers}.")
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.score_workers)) as executor:
        future_map = {executor.submit(score_one_segment, row, args, out_dir): row for row in rows}
        for future in concurrent.futures.as_completed(future_map):
            row = future_map[future]
            try:
                result = future.result()
                results.append(result)
                log(args, f"  scored {row['item_id']} ({len(results)}/{len(rows)})")
            except Exception as exc:
                log(args, f"  score failed for {row['item_id']}: {exc}")
                raise

    results.sort(key=lambda r: (r["source_doc"], section_order(r["section"]), r["item_id"]))
    score_index = out_dir / "score_index.jsonl"
    score_csv = out_dir / "score_index.csv"
    write_jsonl(score_index, results)
    write_csv(
        score_csv,
        [flatten_score(row) for row in results],
        [
            "item_id",
            "source_doc",
            "section",
            "display_section",
            "item_label",
            "topic",
            "topic_category",
            "novelty_score",
            "difficulty_score",
            "vocabulary_value_score",
            "grammar_value_score",
            "exam_value_score",
            "writing_angle_novelty_score",
            "recommendation_score",
            "best_fit_selection_bucket",
            "score_summary",
            "selection_reason",
            "classroom_suggestion",
        ],
    )
    log(args, f"Score outputs written: {score_index} ({file_size_label(score_index)}), {score_csv} ({file_size_label(score_csv)})")
    return results


def score_for_selection(section: str, score: dict) -> float:
    novelty = score_number(score.get("novelty_score"))
    difficulty = score_number(score.get("difficulty_score"))
    vocab = score_number(score.get("vocabulary_value_score"))
    grammar = score_number(score.get("grammar_value_score"))
    exam = score_number(score.get("exam_value_score"))
    writing = score_number(score.get("writing_angle_novelty_score"))
    rec = score_number(score.get("recommendation_score"))
    if section == "reading_a":
        return novelty * 3 + rec * 1.5 + exam + vocab * 0.5
    if section == "reading_b":
        return difficulty * 3 + exam * 1.5 + vocab + grammar * 0.5
    if section in {"reading_c", "reading_d"}:
        return difficulty * 2 + novelty * 2 + exam + grammar * 0.5
    if section in {"gap_filling", "cloze"}:
        return novelty * 2 + exam * 1.5 + difficulty + rec
    if section == "grammar":
        return grammar * 2 + novelty * 1.5 + exam + difficulty * 0.5
    if section in {"practical_writing", "continuation_writing"}:
        return writing * 2 + novelty * 1.5 + exam + rec
    return rec + novelty + difficulty + exam


def run_select(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out)
    score_path = out_dir / "score_index.jsonl"
    if not score_path.exists():
        raise SystemExit(f"Missing {score_path}. Run --mode score first.")
    score_rows = read_jsonl(score_path)
    selected: list[dict] = []
    for section, target_count in SELECTION_TARGETS.items():
        candidates = [row for row in score_rows if row.get("section") == section]
        for row in candidates:
            row_score = row.get("score", {})
            row["selection_score"] = score_for_selection(section, row_score if isinstance(row_score, dict) else {})
        ranked = sorted(candidates, key=lambda r: (r.get("selection_score", 0), score_number((r.get("score") or {}).get("recommendation_score"))), reverse=True)
        selected.extend(ranked[:target_count])
        log(args, f"Selected {min(target_count, len(ranked))}/{target_count} for {section_display(section)} from {len(candidates)} candidate(s).")

    selection_path = out_dir / "selected_items.json"
    selection_csv = out_dir / "selected_items.csv"
    write_json(selection_path, selected)
    write_csv(
        selection_csv,
        [flatten_score(row) | {"selection_score": row.get("selection_score", "")} for row in selected],
        [
            "item_id",
            "source_doc",
            "section",
            "display_section",
            "item_label",
            "selection_score",
            "topic",
            "topic_category",
            "novelty_score",
            "difficulty_score",
            "vocabulary_value_score",
            "grammar_value_score",
            "exam_value_score",
            "writing_angle_novelty_score",
            "recommendation_score",
            "best_fit_selection_bucket",
            "score_summary",
            "selection_reason",
            "classroom_suggestion",
        ],
    )
    log(args, f"Selection outputs written: {selection_path} ({file_size_label(selection_path)}), {selection_csv} ({file_size_label(selection_csv)})")
    return selected


def candidate_summary(row: dict) -> dict:
    flat = flatten_score(row)
    return {
        "item_id": flat.get("item_id", ""),
        "source_doc": flat.get("source_doc", ""),
        "section": flat.get("display_section") or flat.get("section", ""),
        "item_label": flat.get("item_label", ""),
        "topic": flat.get("topic", ""),
        "topic_category": flat.get("topic_category", ""),
        "novelty_score": flat.get("novelty_score", ""),
        "difficulty_score": flat.get("difficulty_score", ""),
        "vocabulary_value_score": flat.get("vocabulary_value_score", ""),
        "grammar_value_score": flat.get("grammar_value_score", ""),
        "exam_value_score": flat.get("exam_value_score", ""),
        "writing_angle_novelty_score": flat.get("writing_angle_novelty_score", ""),
        "recommendation_score": flat.get("recommendation_score", ""),
        "best_fit_selection_bucket": flat.get("best_fit_selection_bucket", ""),
        "score_summary": flat.get("score_summary", ""),
        "selection_reason": flat.get("selection_reason", ""),
        "classroom_suggestion": flat.get("classroom_suggestion", ""),
        "local_selection_score": row.get("selection_score", ""),
    }


def run_review_select(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out)
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")
    score_path = out_dir / "score_index.jsonl"
    if not score_path.exists():
        raise SystemExit(f"Missing {score_path}. Run --mode score first.")

    score_rows = read_jsonl(score_path)
    by_id = {row.get("item_id"): row for row in score_rows}
    final_selected: list[dict] = []
    review_records: list[dict] = []
    log(args, f"Review-select with {args.review_model}; candidates per section={args.review_candidates}.")

    for section, target_count in SELECTION_TARGETS.items():
        candidates = [row for row in score_rows if row.get("section") == section]
        for row in candidates:
            score = row.get("score", {})
            row["selection_score"] = score_for_selection(section, score if isinstance(score, dict) else {})
        ranked = sorted(candidates, key=lambda r: r.get("selection_score", 0), reverse=True)
        shortlist = ranked[: max(target_count, args.review_candidates)]
        if not shortlist:
            log(args, f"  no candidates for {section_display(section)}")
            continue
        if len(shortlist) <= target_count:
            chosen_ids = [row["item_id"] for row in shortlist]
            review = {"section": section_display(section), "selected_item_ids": chosen_ids, "review_reason": "候选数量不超过目标数量，全部入选。", "items": []}
        else:
            prompt = build_review_select_prompt([candidate_summary(row) for row in shortlist], target_count, section)
            chat_result = call_stage_model(
                args,
                prompt,
                model=args.review_model,
                reasoning_effort=args.review_reasoning_effort,
                thinking=args.review_thinking,
                max_tokens=args.review_max_tokens,
            )
            save_api_conversation(out_dir, "review_select", section, prompt, chat_result, args)
            parsed = parse_model_json(chat_result.content)
            if isinstance(parsed, dict) and isinstance(parsed.get("selected_item_ids"), list):
                review = parsed
                chosen_ids = [item_id for item_id in parsed["selected_item_ids"] if item_id in by_id][:target_count]
            else:
                review = {"section": section_display(section), "selected_item_ids": [], "review_reason": "模型输出解析失败，回退到本地评分。", "items": []}
                chosen_ids = [row["item_id"] for row in shortlist[:target_count]]
            if len(chosen_ids) < target_count:
                for row in shortlist:
                    if row["item_id"] not in chosen_ids:
                        chosen_ids.append(row["item_id"])
                    if len(chosen_ids) >= target_count:
                        break

        for item_id in chosen_ids[:target_count]:
            row = by_id[item_id]
            row["review_selected"] = True
            row["review_section"] = section
            final_selected.append(row)
        review_records.append(review)
        log(args, f"  reviewed {section_display(section)}: selected {len(chosen_ids[:target_count])}/{target_count}")

    reviewed_path = out_dir / "reviewed_selected_items.json"
    review_notes_path = out_dir / "review_select_notes.json"
    selection_path = out_dir / "selected_items.json"
    selection_csv = out_dir / "selected_items.csv"
    write_json(reviewed_path, final_selected)
    write_json(review_notes_path, review_records)
    write_json(selection_path, final_selected)
    write_csv(
        selection_csv,
        [flatten_score(row) | {"selection_score": row.get("selection_score", "")} for row in final_selected],
        [
            "item_id",
            "source_doc",
            "section",
            "display_section",
            "item_label",
            "selection_score",
            "topic",
            "topic_category",
            "novelty_score",
            "difficulty_score",
            "vocabulary_value_score",
            "grammar_value_score",
            "exam_value_score",
            "writing_angle_novelty_score",
            "recommendation_score",
            "best_fit_selection_bucket",
            "score_summary",
            "selection_reason",
            "classroom_suggestion",
        ],
    )
    log(args, f"Review selection outputs written: {reviewed_path} ({file_size_label(reviewed_path)}), {selection_csv} ({file_size_label(selection_csv)})")
    return final_selected


def enrich_one_selected(row: dict, args: argparse.Namespace, out_dir: Path) -> dict:
    enrich_dir = out_dir / "enrichments"
    ensure_dir(enrich_dir)
    item_id = row["item_id"]
    enrich_path = enrich_dir / f"{safe_filename(item_id)}.json"
    if enrich_path.exists() and not args.force:
        existing = read_json(enrich_path)
        if isinstance(existing, dict):
            row["enrichment"] = existing.get("enrichment", existing)
            row["enrichment_path"] = str(enrich_path)
            return row

    segment = read_json(Path(row["segment_path"]))
    if not isinstance(segment, dict):
        raise RuntimeError(f"Invalid segment file for {item_id}")
    score = row.get("score", {}) if isinstance(row.get("score"), dict) else {}
    prompt = build_enrich_prompt(segment, score)
    chat_result = call_stage_model(
        args,
        prompt,
        model=args.enrich_model,
        reasoning_effort=args.enrich_reasoning_effort,
        thinking=args.enrich_thinking,
        max_tokens=args.enrich_max_tokens,
    )
    save_api_conversation(out_dir, "enrich", item_id, prompt, chat_result, args)
    parsed = parse_model_json(chat_result.content)
    enrichment = parsed if isinstance(parsed, dict) else {"raw_enrichment": str(parsed)}
    enrichment.setdefault("item_id", item_id)
    result = {
        "item_id": item_id,
        "source_doc": row.get("source_doc", ""),
        "section": row.get("section", ""),
        "display_section": row.get("display_section", ""),
        "item_label": row.get("item_label", ""),
        "enrichment": enrichment,
        "usage": chat_result.usage,
        "client_used": chat_result.client_used,
        "prompt_version": ENRICH_PROMPT_VERSION,
    }
    write_json(enrich_path, result)
    row["enrichment"] = enrichment
    row["enrichment_path"] = str(enrich_path)
    return row


def run_enrich_selected(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out)
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")
    selection_path = out_dir / "selected_items.json"
    if not selection_path.exists():
        raise SystemExit(f"Missing {selection_path}. Run --mode select first.")
    selected = read_json(selection_path)
    if not isinstance(selected, list):
        raise SystemExit(f"Invalid {selection_path}")

    log(args, f"Enriching {len(selected)} selected item(s) with {args.enrich_model}; workers={args.enrich_workers}.")
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.enrich_workers)) as executor:
        future_map = {executor.submit(enrich_one_selected, row, args, out_dir): row for row in selected}
        for future in concurrent.futures.as_completed(future_map):
            row = future_map[future]
            try:
                result = future.result()
                results.append(result)
                log(args, f"  enriched {row['item_id']} ({len(results)}/{len(selected)})")
            except Exception as exc:
                log(args, f"  enrich failed for {row['item_id']}: {exc}")
                raise

    results.sort(key=lambda r: (section_order(r.get("section", "")), r.get("source_doc", ""), r.get("item_id", "")))
    enriched_path = out_dir / "selected_items.enriched.json"
    write_json(enriched_path, results)
    write_json(selection_path, results)
    log(args, f"Enrichment outputs written: {enriched_path} ({file_size_label(enriched_path)})")
    return results


def md_escape_heading(text: str) -> str:
    return str(text or "").replace("\n", " ").strip()


def render_words(words: object) -> str:
    if not isinstance(words, list) or not words:
        return "暂无"
    parts: list[str] = []
    for item in words[:12]:
        if isinstance(item, dict):
            word = item.get("word", "")
            meaning = item.get("meaning", "")
            reason = item.get("teaching_reason", "")
            parts.append(f"- `{word}`：{meaning}。{reason}".strip())
    return "\n".join(parts) or "暂无"


def render_grammar(points: object) -> str:
    if not isinstance(points, list) or not points:
        return "暂无"
    parts: list[str] = []
    for item in points[:10]:
        if isinstance(item, dict):
            typ = item.get("type", "考点")
            evidence = item.get("evidence", "")
            teaching = item.get("teaching_point", "")
            parts.append(f"- {typ}：{evidence}。{teaching}".strip())
    return "\n".join(parts) or "暂无"


def render_sentences(points: object) -> str:
    if not isinstance(points, list) or not points:
        return "暂无"
    parts: list[str] = []
    for index, item in enumerate(points[:5], start=1):
        if isinstance(item, dict):
            sentence = item.get("sentence", "")
            analysis = item.get("structure_analysis", "")
            teaching = item.get("teaching_point", "")
            parts.append(
                f"{index}. **原句：** {sentence}\n"
                f"   - **结构：** {analysis}\n"
                f"   - **讲解：** {teaching}"
            )
    return "\n".join(parts) or "暂无"


def merge_cached_enrichments(selected: list[dict], enriched_cache: object) -> int:
    if not isinstance(enriched_cache, list):
        return 0
    enrichment_by_id = {
        str(item.get("item_id") or ""): item.get("enrichment")
        for item in enriched_cache
        if isinstance(item, dict) and isinstance(item.get("enrichment"), dict)
    }
    merged = 0
    for item in selected:
        if not isinstance(item, dict) or isinstance(item.get("enrichment"), dict):
            continue
        cached = enrichment_by_id.get(str(item.get("item_id") or ""))
        if isinstance(cached, dict):
            item["enrichment"] = cached
            merged += 1
    return merged


def run_assemble(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    selection_path = out_dir / "selected_items.json"
    if not selection_path.exists():
        raise SystemExit(f"Missing {selection_path}. Run --mode select first.")
    selected = read_json(selection_path)
    if not isinstance(selected, list):
        raise SystemExit(f"Invalid {selection_path}")
    enriched_cache_path = out_dir / "selected_items.enriched.json"
    if enriched_cache_path.exists():
        enriched_cache = read_json(enriched_cache_path)
        merged = merge_cached_enrichments(selected, enriched_cache)
        if merged:
            log(args, f"Merged cached enrichment into {merged} selected item(s) before assembly.")
    assembled_dir = out_dir / "assembled"
    ensure_dir(assembled_dir)

    ordered = sorted(selected, key=lambda r: (section_order(r.get("section", "")), r.get("source_doc", ""), r.get("item_id", "")))
    question_lines = [
        "# 高三英语精选训练题",
        "",
        "> 本文档按题型整理，可直接用于打印、课堂训练或课后练习。",
    ]
    answer_lines = ["", "## 答案汇总"]
    answers_only_lines = [
        "# 高三英语答案汇总",
        "",
        "> 按题型和题目顺序汇总，供教师快速核对。",
    ]
    teacher_lines = [
        "# 教师讲解与评分说明",
        "",
        "> 包含选题依据、重点词汇、语法考点、长难句分析和课堂建议。",
    ]

    current_section = ""
    for row in ordered:
        segment = read_json(Path(row["segment_path"]))
        score = row.get("score", {}) if isinstance(row.get("score"), dict) else {}
        enrichment = row.get("enrichment", {}) if isinstance(row.get("enrichment"), dict) else {}
        score = score | enrichment
        section_name = row.get("display_section") or section_display(row.get("section", ""))
        if section_name != current_section:
            question_lines.extend(["", f"## {section_name}"])
            answer_lines.extend(["", f"### {section_name}"])
            answers_only_lines.extend(["", f"## {section_name}"])
            teacher_lines.extend(["", f"## {section_name}"])
            current_section = section_name
        title = md_escape_heading(segment.get("title") or score.get("topic") or row.get("item_label"))
        question_lines.extend(
            [
                "",
                f"### {row.get('item_label', '')}｜{title}",
                "",
                f"来源：{row.get('source_doc', '')}",
                "",
                segment_body(segment),
            ]
        )
        answer_lines.extend(
            [
                "",
                f"#### {row.get('item_label', '')}｜{title}",
                "",
                answer_key_text(segment.get("answer_key")),
            ]
        )
        answers_only_lines.extend(
            [
                "",
                f"### {row.get('item_label', '')}｜{title}",
                "",
                answer_key_text(segment.get("answer_key")),
            ]
        )
        teacher_lines.extend(
            [
                "",
                f"### {row.get('item_label', '')}｜{title}",
                "",
                f"- 来源：{row.get('source_doc', '')}",
                f"- 主题：{score.get('topic', '')}",
                f"- 评分：新颖度 {score.get('novelty_score', '')}；难度 {score.get('difficulty_score', '')}；词汇价值 {score.get('vocabulary_value_score', '')}；语法价值 {score.get('grammar_value_score', '')}；推荐度 {score.get('recommendation_score', '')}",
                f"- 入选理由：{score.get('selection_reason', '')}",
                f"- 课堂建议：{score.get('classroom_suggestion', '')}",
                "",
                "#### 重点词汇",
                render_words(score.get("core_high_frequency_words", []))
                + "\n"
                + render_words(score.get("familiar_words_new_meanings", []))
                + "\n"
                + render_words(score.get("difficult_or_low_frequency_words", [])),
                "",
                "#### 语法与词汇变形",
                render_grammar(score.get("word_formation_and_grammar", [])),
                "",
                "#### 长难句",
                render_sentences(score.get("long_difficult_sentences", [])),
                "",
                "#### 补充讲解建议",
                "",
                str(score.get("teaching_notes", "") or "暂无"),
            ]
        )

    final_set_path = assembled_dir / "final_selected_questions_with_answers.md"
    teacher_path = assembled_dir / "final_teacher_notes.md"
    answers_path = assembled_dir / "final_answers_only.md"
    final_set_path.write_text("\n".join(question_lines + answer_lines) + "\n", encoding="utf-8")
    teacher_path.write_text("\n".join(teacher_lines) + "\n", encoding="utf-8")
    answers_path.write_text("\n".join(answers_only_lines).strip() + "\n", encoding="utf-8")
    log(args, f"Assembled final set: {final_set_path} ({file_size_label(final_set_path)})")
    log(args, f"Assembled teacher notes: {teacher_path} ({file_size_label(teacher_path)})")
    log(args, f"Assembled answers only: {answers_path} ({file_size_label(answers_path)})")


def run_repair_answers(args: argparse.Namespace) -> None:
    """Repair answer_key fields in existing segment JSONs without calling AI.

    Reads the original extracted text for each paper, scans the full text for
    answers using all known formats (standard ranges, table-format tildes,
    double-hyphen ranges, concatenated grammar answers, explanation-embedded
    patterns), and updates the segment JSON files in-place.  Then regenerates
    ``segment_index.csv`` and runs ``assemble``.
    """
    out_dir = Path(args.out)
    extracted_dir = out_dir / "extracted_text"
    segments_dir = out_dir / "segments"
    index_path = out_dir / "segment_index.jsonl"

    if not index_path.exists():
        raise SystemExit(f"Missing {index_path}. Run --mode segment first.")
    segment_rows = read_jsonl(index_path)
    if not segment_rows:
        raise SystemExit("segment_index.jsonl is empty.")

    # Group segments by source doc
    by_doc: dict[str, list[dict]] = {}
    for row in segment_rows:
        by_doc.setdefault(row["source_doc"], []).append(row)

    log(args, f"Repairing answers for {len(by_doc)} paper(s) using full-text scan (no API calls).")
    updated_count = 0
    no_answer_docs: list[str] = []

    for source_doc, rows in sorted(by_doc.items()):
        stem = safe_stem(source_doc)
        text_path = extracted_dir / f"{stem}.txt"
        if not text_path.exists():
            log(args, f"  skip {source_doc}: extracted text not found at {text_path}")
            continue

        text = text_path.read_text(encoding="utf-8")
        full_answers = extract_all_answers_from_full_text(text)
        answer_tail = extract_answer_tail(text, max_chars=30000)

        if not full_answers:
            no_answer_docs.append(source_doc)

        answer_ranges = {
            "reading_a": (21, 23),
            "reading_b": (24, 27),
            "reading_c": (28, 31),
            "reading_d": (32, 35),
            "gap_filling": (36, 40),
            "cloze": (41, 55),
        }

        for row in rows:
            section = row.get("section", "")
            seg_path = Path(row["segment_path"])
            if not seg_path.exists():
                continue
            segment = read_json(seg_path)
            if not isinstance(segment, dict):
                continue

            if section in answer_ranges:
                start_q, end_q = answer_ranges[section]
                ak_list = [{"number": str(n), "answer": full_answers[n]}
                           for n in range(start_q, end_q + 1) if n in full_answers]
                answer_source = "答案区" if ak_list else ("原卷未提供答案" if not full_answers else "未识别")
                segment["answer_key"] = ak_list
            elif section == "grammar":
                ak_list = [{"number": str(n), "answer": full_answers[n]}
                           for n in range(56, 66) if n in full_answers]
                answer_source = "答案区" if ak_list else ("原卷未提供答案" if not full_answers else "未识别")
                segment["answer_key"] = ak_list
            elif section == "practical_writing":
                ak = writing_answer_text(answer_tail, continuation=False)
                answer_source = "答案区/范文" if ak else ("原卷未提供答案" if not full_answers else "未识别")
                segment["answer_key"] = ak
            elif section == "continuation_writing":
                ak = writing_answer_text(answer_tail, continuation=True)
                answer_source = "答案区/范文" if ak else ("原卷未提供答案" if not full_answers else "未识别")
                segment["answer_key"] = ak
            else:
                segment["answer_key"] = []
                answer_source = "原卷未提供答案" if not full_answers else "未识别"

            segment["answer_source"] = answer_source
            write_json(seg_path, segment)

            # Update the index row
            row["answer_count"] = len(segment["answer_key"]) if isinstance(segment["answer_key"], list) else (1 if isinstance(segment["answer_key"], str) and segment["answer_key"].strip() else 0)
            row["confidence"] = segment.get("confidence", row.get("confidence", ""))
            updated_count += 1

        extracted = sum(1 for n in range(21, 56) if n in full_answers)
        grammar = sum(1 for n in range(56, 66) if n in full_answers)
        log(args, f"  {source_doc}: extracted {extracted}/35 choice + {grammar}/10 grammar answers")

    # Write updated index
    segment_rows.sort(key=lambda r: (r["source_doc"], section_order(r["section"]), r["item_id"]))
    write_jsonl(index_path, segment_rows)
    segment_csv = out_dir / "segment_index.csv"
    write_csv(
        segment_csv,
        segment_rows,
        ["item_id", "source_doc", "section", "display_section", "item_label", "title", "char_count", "answer_count", "confidence", "rough_unit", "segment_path"],
    )
    log(args, f"Updated {updated_count} segment(s).  segment_index rewritten: {segment_csv}")

    if no_answer_docs:
        log(args, f"Papers marked '原卷未提供答案': {len(no_answer_docs)}")
        for doc in no_answer_docs:
            log(args, f"  - {doc}")

    # Auto-run assemble so the repaired answers appear in Markdown
    log(args, "Re-assembling final Markdown with repaired answers …")
    run_assemble(args)


def run_preflight(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    out_dir = Path(args.out)
    docx_files = collect_docx(input_path)
    if not docx_files:
        raise SystemExit(f"No .docx files found under {args.input}")

    total_chars = 0
    local_segment_count = 0
    local_segment_chars = 0
    low_confidence_docs: list[str] = []
    for docx in docx_files:
        text = extract_docx_text(docx)
        total_chars += len(text)
        if args.segment_input == "local":
            segments = local_segment_paper(docx.name, text)
            local_segment_count += len(segments)
            local_segment_chars += sum(len(segment_body(segment)) for segment in segments)
            if len(segments) < 7:
                low_confidence_docs.append(f"{docx.name}（本地只识别到 {len(segments)} 个单元）")

    api_segment_calls = 0 if args.segment_input == "local" else len(docx_files)
    score_calls = local_segment_count if args.segment_input == "local" else "取决于 segment 输出"
    approx_score_prompt_tokens = rough_token_estimate("x" * local_segment_chars) if args.segment_input == "local" else "取决于 segment 输出"
    existing_conversations = len(list((out_dir / "api_conversations").rglob("*.md"))) if (out_dir / "api_conversations").exists() else 0
    current_docs = {docx.name for docx in docx_files}
    stale_extracted = []
    extracted_dir = out_dir / "extracted_text"
    if extracted_dir.exists():
        for path in extracted_dir.glob("*.txt"):
            if not any(safe_stem(name) == path.stem for name in current_docs):
                stale_extracted.append(path.name)

    log(args, "Preflight summary:")
    log(args, f"  input docx: {len(docx_files)}")
    log(args, f"  extracted text chars: {total_chars:,} (roughly {rough_token_estimate('x' * total_chars):,} tokens if sent once)")
    log(args, f"  segment mode: {args.segment_input}; expected segment API calls: {api_segment_calls}")
    if args.segment_input == "local":
        log(args, f"  local segments: {local_segment_count}; score API calls after segment: {score_calls}")
        log(args, f"  rough score prompt text tokens before system overhead: {approx_score_prompt_tokens:,}")
    log(args, f"  existing saved API conversations under output: {existing_conversations}")
    if stale_extracted:
        log(args, f"  stale output warning: {len(stale_extracted)} extracted text file(s) do not match current input; use --init before a fresh run.")
    if low_confidence_docs:
        log(args, "  local segmentation warning:")
        for item in low_confidence_docs:
            log(args, f"    - {item}")


def run_quality_report(args: argparse.Namespace) -> None:
    """Generate a Markdown quality report summarising all pipeline outputs.

    This mode is read-only — it never calls the AI, never writes to segment
    or score files, and is safe to run at any time.
    """
    out_dir = Path(args.out)
    report_path = out_dir / "run_quality_report.md"

    # --- gather data ---
    docx_files = collect_docx(Path(args.input))
    segment_csv = out_dir / "segment_index.csv"
    score_csv = out_dir / "score_index.csv"
    selected_csv = out_dir / "selected_items.csv"
    review_notes_path = out_dir / "review_select_notes.json"
    fallback_report_path = out_dir / "segment_fallback_report.json"
    assembled_dir = out_dir / "assembled"

    segment_rows = list(csv.DictReader(segment_csv.open("r", encoding="utf-8-sig"))) if segment_csv.exists() else []
    score_rows = list(csv.DictReader(score_csv.open("r", encoding="utf-8-sig"))) if score_csv.exists() else []
    selected_rows = list(csv.DictReader(selected_csv.open("r", encoding="utf-8-sig"))) if selected_csv.exists() else []
    review_notes = json.loads(review_notes_path.read_text(encoding="utf-8")) if review_notes_path.exists() else []
    fallback_records = json.loads(fallback_report_path.read_text(encoding="utf-8")) if fallback_report_path.exists() else []
    if not isinstance(fallback_records, list):
        fallback_records = []
    api_count = len(list((out_dir / "api_conversations").rglob("*.md"))) if (out_dir / "api_conversations").exists() else 0

    # --- per-paper answer coverage ---
    by_doc: dict[str, dict] = {}
    for r in segment_rows:
        doc = r.get("source_doc", "?")
        if doc not in by_doc:
            by_doc[doc] = {"segs": 0, "with_answers": 0, "answer_total": 0, "sections": {}}
        by_doc[doc]["segs"] += 1
        ac = int(r.get("answer_count", "0"))
        by_doc[doc]["answer_total"] += ac
        if ac > 0:
            by_doc[doc]["with_answers"] += 1
        sec = r.get("section", "?")
        by_doc[doc]["sections"][sec] = ac

    # --- score summary per section ---
    score_by_section: dict[str, list[dict]] = {}
    for r in score_rows:
        sec = r.get("section", "?")
        score_by_section.setdefault(sec, []).append(r)

    # --- selected items ---
    review_enabled = bool(review_notes)

    # --- build report ---
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    w(f"# 高三英语模拟题整理质量报告")
    w()
    w(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    w()

    w("## 1. 输入与输出概览")
    w()
    w(f"| 指标 | 值 |")
    w(f"|---|---|")
    w(f"| 输入 docx | {len(docx_files)} |")
    w(f"| 切割 segments | {len(segment_rows)} |")
    w(f"| 评分记录 | {len(score_rows)} |")
    w(f"| 入选题目 | {len(selected_rows)} |")
    w(f"| Pro 复核 | {'✅ 已启用' if review_enabled else '❌ 未启用'} |")
    w(f"| API 调用次数 | {api_count} |")
    w()

    # --- output files ---
    w("### 输出文件")
    w()
    w("| 文件 | 大小 |")
    w("|---|---|")
    for p in sorted(out_dir.rglob("*")):
        if p.is_file() and p.suffix in {".csv", ".json", ".jsonl", ".md"}:
            w(f"| `{p.relative_to(out_dir)}` | {file_size_label(p)} |")
    w()

    # --- per-paper coverage ---
    w("## 2. 试卷切割与答案覆盖")
    w()
    expected_sections = ["reading_a", "reading_b", "reading_c", "reading_d",
                        "gap_filling", "cloze", "grammar",
                        "practical_writing", "continuation_writing"]
    header = "| 试卷 | segments | 有答案 | 总答案数 | " + " | ".join(SECTION_DISPLAY.get(s, s) for s in expected_sections) + " |"
    w(header)
    w("|" + "---|" * (4 + len(expected_sections)) + "")
    for doc in sorted(by_doc):
        d = by_doc[doc]
        parts = [doc[:30], str(d["segs"]), str(d["with_answers"]), str(d["answer_total"])]
        for s in expected_sections:
            parts.append(str(d["sections"].get(s, 0)))
        w("| " + " | ".join(parts) + " |")
    w()

    # --- score distribution ---
    w("## 3. 评分分布")
    w()
    w("| 题型 | 数量 | 均分·新颖 | 均分·难度 | 均分·词汇 | 均分·语法 | 均分·推荐 |")
    w("|---|---|---|---|---|---|---|")
    for sec in expected_sections:
        items = score_by_section.get(sec, [])
        if not items:
            continue
        n = len(items)
        avg = lambda key: f"{sum(float(r.get(key, 0) or 0) for r in items) / n:.1f}"
        w(f"| {SECTION_DISPLAY.get(sec, sec)} | {n} | {avg('novelty_score')} | {avg('difficulty_score')} | {avg('vocabulary_value_score')} | {avg('grammar_value_score')} | {avg('recommendation_score')} |")
    w()

    # --- selected items ---
    w("## 4. 入选题目")
    w()
    if selected_rows:
        w("| 题型 | 来源试卷 | 主题 | 评分 |")
        w("|---|---|---|---|")
        for r in selected_rows:
            sec = SECTION_DISPLAY.get(r.get("section", ""), r.get("section", ""))
            doc = (r.get("source_doc") or "")[:20]
            topic = (r.get("topic") or "")[:35]
            sel_score = r.get("selection_score", "")
            w(f"| {sec} | {doc} | {topic} | {sel_score} |")
    else:
        w("*(暂无入选题目 — 请先运行 select 或 review-select)*")
    w()

    # --- pro review summary ---
    w("## 5. Pro 复核摘要")
    w()
    if review_notes:
        for entry in review_notes:
            sec = entry.get("section", "?")
            selected = entry.get("selected_item_ids", [])
            reason = (entry.get("review_reason") or "")[:120]
            w(f"- **{sec}**：入选 {len(selected)} 篇 — {reason}")
    else:
        w("*(未运行 review-select，或 review_select_notes.json 不存在)*")
    w()

    # --- API usage ---
    w("## 6. API 用量估算")
    w()
    if score_rows:
        total_score_tokens = 0
        score_files = list((out_dir / "scores").glob("*.json"))
        for sf in score_files:
            try:
                data = json.loads(sf.read_text(encoding="utf-8"))
                usage = data.get("usage") or {}
                total_score_tokens += (usage.get("total_tokens") or usage.get("completion_tokens") or 0)
            except Exception:
                pass
        enrich_files = list((out_dir / "enrichments").glob("*.json"))
        total_enrich_tokens = 0
        for ef in enrich_files:
            try:
                data = json.loads(ef.read_text(encoding="utf-8"))
                usage = data.get("usage") or {}
                total_enrich_tokens += (usage.get("total_tokens") or usage.get("completion_tokens") or 0)
            except Exception:
                pass
        w(f"| 阶段 | 调用次数 | 估算 token |")
        w(f"|---|---|---|")
        w(f"| segment | {len(fallback_records)} paper fallback(s) | N/A (in conversations) |")
        w(f"| score | {len(score_rows)} | {total_score_tokens:,} |")
        w(f"| review-select | {len(review_notes)} | N/A (in conversations) |")
        w(f"| enrich-selected | {len(selected_rows)} | {total_enrich_tokens:,} |")
        w(f"| **合计** | **{len(score_rows) + len(review_notes) + len(selected_rows)}** | **{total_score_tokens + total_enrich_tokens:,}** |")
    else:
        w("*(暂无评分数据)*")
    w()

    # --- warnings ---
    w("## 7. 注意事项")
    w()
    if fallback_records:
        w("### 自动模型重切记录")
        w()
        w("| 试卷 | 本地等级 | 回退方式 | 模型 | 最终等级 |")
        w("|---|---|---|---|---|")
        for item in fallback_records:
            w(
                f"| {item.get('source_doc', '')} | {item.get('local_grade', '')} | "
                f"{item.get('fallback_mode', '')} | {item.get('model', '')} | {item.get('final_grade', '')} |"
            )
        w()
    warnings_found = 0
    for r in segment_rows:
        if float(r.get("confidence", "0") or 0) < 0.75:
            if warnings_found == 0:
                w("### 低置信度切割")
            warnings_found += 1
            w(f"- `{r.get('item_id', '?')}` — confidence={r.get('confidence', '?')}")
    if warnings_found == 0:
        w("✅ 无低置信度切割。")
    w()

    # --- write ---
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(args, f"Quality report written: {report_path} ({file_size_label(report_path)})")


def run_stage1(args: argparse.Namespace) -> None:
    run_segment(args)
    run_score(args)
    run_select(args)
    if args.review_select:
        run_review_select(args)
    run_enrich_selected(args)
    run_assemble(args)


def run_extract_and_prompt(args: argparse.Namespace) -> list[Item]:
    input_path = Path(args.input)
    out_dir = Path(args.out)
    extracted_dir = out_dir / "extracted_text"
    ensure_dir(out_dir)
    ensure_dir(extracted_dir)

    log(args, f"Step 1/3: scanning docx input: {input_path}")
    docx_files = collect_docx(input_path)
    if not docx_files:
        raise SystemExit(f"No .docx files found under {input_path}")
    log(args, f"Found {len(docx_files)} docx file(s).")

    template = read_prompt_template(Path(args.prompt_template))
    all_items: list[Item] = []
    for doc_idx, docx in enumerate(docx_files, start=1):
        log(args, f"Extracting {doc_idx}/{len(docx_files)}: {docx.name}")
        text = extract_docx_text(docx)
        source_doc = docx.name
        text_path = extracted_dir / f"{safe_stem(source_doc)}.txt"
        text_path.write_text(text, encoding="utf-8")
        items = split_doc_into_items(source_doc, text)
        all_items.extend(items)
        log(
            args,
            f"  wrote text: {text_path} ({file_size_label(text_path)}); "
            f"split into {len(items)} candidate item(s).",
        )

    log(args, f"Step 2/3: writing item index for {len(all_items)} candidate item(s).")
    items_path = out_dir / "items.jsonl"
    write_jsonl(items_path, [asdict(item) for item in all_items])

    prompt_rows = []
    for item in all_items:
        prompt_rows.append(
            {
                "item_id": item.item_id,
                "source_doc": item.source_doc,
                "section": item.section,
                "item_label": item.item_label,
                "char_count": item.char_count,
                "wordish_count": item.wordish_count,
                "prompt": render_analysis_prompt(template, item),
            }
        )
    prompts_path = out_dir / "analysis_prompts.jsonl"
    index_path = out_dir / "analysis_index.csv"
    final_prompt_path = out_dir / "final_selection_prompt.md"
    write_jsonl(prompts_path, prompt_rows)
    write_csv(
        index_path,
        [asdict(item) for item in all_items],
        ["item_id", "source_doc", "section", "item_label", "char_count", "wordish_count"],
    )
    final_prompt_path.write_text(
        build_final_selection_prompt(out_dir / "model_analyses.jsonl"),
        encoding="utf-8",
    )
    log(args, "Step 3/3: prompt-generation outputs written successfully.")
    log(args, f"  items: {items_path} ({file_size_label(items_path)})")
    log(args, f"  prompts: {prompts_path} ({file_size_label(prompts_path)})")
    log(args, f"  index: {index_path} ({file_size_label(index_path)})")
    log(args, f"  final selection prompt: {final_prompt_path} ({file_size_label(final_prompt_path)})")
    return all_items


def run_analyze(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    if not (out_dir / "analysis_prompts.jsonl").exists():
        log(args, "analysis_prompts.jsonl not found; generating prompts first.")
        run_extract_and_prompt(args)

    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")

    prompt_rows = read_jsonl(out_dir / "analysis_prompts.jsonl")
    result_path = out_dir / "model_analyses.jsonl"
    existing = {row.get("item_id"): row for row in read_jsonl(result_path)}
    results: list[dict] = list(existing.values())
    log(args, "Step 1/3: starting model analysis.")
    log(
        args,
        "API settings: "
        f"client={args.client}, base_url={args.base_url}, model={args.model}, "
        f"reasoning_effort={args.reasoning_effort}, thinking={args.thinking}, "
        f"temperature={args.temperature}",
    )
    log(args, f"Loaded {len(prompt_rows)} analysis prompt(s); {len(existing)} cached result(s).")

    for idx, row in enumerate(prompt_rows, start=1):
        item_id = row["item_id"]
        if item_id in existing and not args.force:
            log(
                args,
                f"[skip] {idx}/{len(prompt_rows)} {item_id} "
                f"({row.get('source_doc', '')} | {row.get('section', '')} | {row.get('item_label', '')})",
            )
            continue
        log(
            args,
            f"[analyze] {idx}/{len(prompt_rows)} {item_id} "
            f"({row.get('source_doc', '')} | {row.get('section', '')} | {row.get('item_label', '')}; "
            f"{row.get('char_count', '')} chars, {row.get('wordish_count', '')} words)",
        )
        started = time.time()
        chat_result = call_chat_completion(
            row["prompt"],
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            temperature=args.temperature,
            client_mode=args.client,
            reasoning_effort=args.reasoning_effort,
            thinking=args.thinking,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            max_retries=args.max_retries,
        )
        elapsed = time.time() - started
        log(
            args,
            f"  API response received in {elapsed:.1f}s via {chat_result.client_used}; "
            f"content={len(chat_result.content)} chars, reasoning={len(chat_result.reasoning)} chars.",
        )
        if chat_result.usage:
            log(args, f"  usage: {json.dumps(chat_result.usage, ensure_ascii=False)}")
        show_terminal_text(args, "AI reasoning/thinking", chat_result.reasoning, args.show_reasoning)
        show_terminal_text(args, "AI final output", chat_result.content, args.show_output)

        analysis = parse_model_json(chat_result.content)
        parsed_ok = isinstance(analysis, dict)
        log(args, f"  JSON parse: {'ok' if parsed_ok else 'failed, stored raw text'}")
        result = {
            "item_id": item_id,
            "source_doc": row.get("source_doc", ""),
            "section": row.get("section", ""),
            "item_label": row.get("item_label", ""),
            "analysis": analysis,
            "reasoning": chat_result.reasoning,
            "usage": chat_result.usage,
            "client_used": chat_result.client_used,
        }
        existing[item_id] = result
        results = list(existing.values())
        write_jsonl(result_path, results)
        log(args, f"  saved checkpoint: {result_path} ({file_size_label(result_path)})")

    log(args, "Step 2/3: writing flattened CSV analysis table.")
    flat = [flatten_analysis(row) for row in results]
    csv_path = out_dir / "model_analyses.csv"
    write_csv(
        csv_path,
        flat,
        [
            "item_id",
            "source_doc",
            "section",
            "item_label",
            "topic",
            "topic_category",
            "novelty_score",
            "difficulty_score",
            "vocabulary_value_score",
            "grammar_value_score",
            "exam_value_score",
            "recommendation_score",
            "best_fit_selection_bucket",
            "selection_reason",
            "classroom_suggestion",
            "core_high_frequency_words",
            "familiar_words_new_meanings",
            "difficult_or_low_frequency_words",
            "topic_words",
            "word_formation_and_grammar",
            "long_difficult_sentences",
            "exam_skills",
            "main_difficulty_sources",
        ],
    )
    log(args, "Step 3/3: model analysis outputs written successfully.")
    log(args, f"  jsonl: {result_path} ({file_size_label(result_path)})")
    log(args, f"  csv: {csv_path} ({file_size_label(csv_path)})")


def run_final(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    analyses_path = out_dir / "model_analyses.jsonl"
    if not analyses_path.exists():
        raise SystemExit(f"Missing {analyses_path}. Run --mode analyze first.")
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")

    log(args, "Step 1/2: building final horizontal-selection prompt.")
    log(args, f"Reading analyses: {analyses_path} ({file_size_label(analyses_path)})")
    analyses = build_final_material(analyses_path, args)
    log(args, f"Final material size: {len(analyses)} chars.")
    prompt = build_final_selection_prompt(analyses_path) + "\n\n单篇分析结果如下：\n" + analyses
    log(
        args,
        "API settings: "
        f"client={args.client}, base_url={args.base_url}, model={args.model}, "
        f"reasoning_effort={args.reasoning_effort}, thinking={args.thinking}, "
        f"temperature={args.temperature}",
    )
    started = time.time()
    chat_result = call_chat_completion(
        prompt,
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        temperature=args.temperature,
        client_mode=args.client,
        reasoning_effort=args.reasoning_effort,
        thinking=args.thinking,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        max_retries=args.max_retries,
    )
    elapsed = time.time() - started
    log(
        args,
        f"API response received in {elapsed:.1f}s via {chat_result.client_used}; "
        f"content={len(chat_result.content)} chars, reasoning={len(chat_result.reasoning)} chars.",
    )
    if chat_result.usage:
        log(args, f"usage: {json.dumps(chat_result.usage, ensure_ascii=False)}")
    show_terminal_text(args, "AI reasoning/thinking", chat_result.reasoning, args.show_reasoning)
    show_terminal_text(args, "AI final output", chat_result.content, args.show_output)

    final_path = out_dir / "final_selection.md"
    final_path.write_text(chat_result.content, encoding="utf-8")
    log(args, "Step 2/2: final selection document written successfully.")
    log(args, f"  final document: {final_path} ({file_size_label(final_path)})")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch analyze Gaokao English mock exam docx files.")
    parser.add_argument("input", help="A .docx file or a folder containing .docx files.")
    parser.add_argument("--out", default="outputs/gaokao_english", help="Output folder.")
    parser.add_argument(
        "--mode",
        choices=["prompts", "analyze", "final", "preflight", "segment", "score", "select", "review-select", "enrich-selected", "assemble", "repair-answers", "quality-report", "export-docx", "stage1"],
        default="prompts",
    )
    parser.add_argument("--prompt-template", default="config/analysis_prompt_template.md")
    parser.add_argument("--provider", default="deepseek", help="Label only, for your own notes.")
    # DEEPSEEK TUNING: official OpenAI-SDK style uses the API root here.
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    # DEEPSEEK TUNING: deepseek-chat / deepseek-reasoner are legacy aliases.
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--segment-model", default="deepseek-v4-flash", help="Model used to split full papers into section JSON files.")
    parser.add_argument("--score-model", default="deepseek-v4-flash", help="Model used to score each segmented item.")
    parser.add_argument("--review-model", default="deepseek-v4-pro", help="Model used to review local shortlist selections.")
    parser.add_argument("--enrich-model", default="deepseek-v4-flash", help="Model used to enrich final selected items with words/grammar/sentences.")
    parser.add_argument("--segment-workers", type=int, default=16, help="Concurrent docx segmentation requests.")
    parser.add_argument("--score-workers", type=int, default=16, help="Concurrent scoring requests.")
    parser.add_argument("--enrich-workers", type=int, default=16, help="Concurrent enrichment requests for selected items.")
    parser.add_argument("--segment-input", choices=["local", "rough", "full"], default="local", help="local parses common exam structure without API; rough/full use model segmentation.")
    parser.add_argument(
        "--segment-warning-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When local segmentation has structural WARN/FAIL, re-segment only affected papers with rough model chunks.",
    )
    parser.add_argument("--answer-tail-chars", type=int, default=8000, help="Characters of the final answer area appended to rough segment chunks.")
    parser.add_argument("--review-candidates", type=int, default=6, help="Local shortlist size per section before pro review.")
    parser.add_argument("--segment-reasoning-effort", choices=["none", "low", "medium", "high"], default="none")
    parser.add_argument("--score-reasoning-effort", choices=["none", "low", "medium", "high"], default="none")
    parser.add_argument("--review-reasoning-effort", choices=["none", "low", "medium", "high"], default="medium")
    parser.add_argument("--enrich-reasoning-effort", choices=["none", "low", "medium", "high"], default="none")
    parser.add_argument("--segment-thinking", choices=["enabled", "disabled", "omit"], default="disabled")
    parser.add_argument("--score-thinking", choices=["enabled", "disabled", "omit"], default="disabled")
    parser.add_argument("--review-thinking", choices=["enabled", "disabled", "omit"], default="enabled")
    parser.add_argument("--enrich-thinking", choices=["enabled", "disabled", "omit"], default="disabled")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    # PDF input. The layout-parsing service is deployed per AI Studio account, so
    # the base URL has to come from the user's own console — there is no global one.
    parser.add_argument("--paddle-base-url", default="", help="PaddleOCR layout-parsing URL (or set PADDLEOCR_BASE_URL).")
    parser.add_argument("--paddle-token", default="", help="AI Studio access token (or set PADDLEOCR_ACCESS_TOKEN).")
    parser.add_argument("--api-key", default="")
    # DEEPSEEK TUNING: `auto` tries the OpenAI SDK first, then falls back to raw HTTP.
    parser.add_argument("--client", choices=["auto", "sdk", "http"], default="auto")
    # DEEPSEEK TUNING: set to none if a non-DeepSeek-compatible endpoint rejects it.
    parser.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high"], default="high")
    # DEEPSEEK TUNING: official DeepSeek thinking mode uses enabled. Use omit for old endpoints.
    parser.add_argument("--thinking", choices=["enabled", "disabled", "omit"], default="enabled")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=8, help="Max retries for retryable API errors such as 429 rate limits.")
    parser.add_argument("--max-tokens", type=int, default=6000, help="Output token cap for legacy analyze/final modes.")
    parser.add_argument("--segment-max-tokens", type=int, default=16000, help="Output token cap for AI segmentation. Ignored by local segmentation.")
    parser.add_argument("--score-max-tokens", type=int, default=1200, help="Output token cap for each lightweight score call.")
    parser.add_argument("--review-max-tokens", type=int, default=2500, help="Output token cap for each review-select call.")
    parser.add_argument("--enrich-max-tokens", type=int, default=3500, help="Output token cap for each selected-item enrichment call.")
    parser.add_argument(
        "--show-output",
        choices=["none", "preview", "full"],
        default="preview",
        help="How much AI final output to print in the terminal.",
    )
    parser.add_argument(
        "--show-reasoning",
        choices=["none", "preview", "full"],
        default="preview",
        help="How much API-returned reasoning/thinking content to print, if the model returns it.",
    )
    parser.add_argument(
        "--final-input",
        choices=["compact", "full"],
        default="compact",
        help="Input material sent in --mode final. compact strips debug reasoning/usage metadata.",
    )
    parser.add_argument("--preview-chars", type=int, default=1200, help="Characters shown for preview output.")
    parser.add_argument("--quiet", action="store_true", help="Only print hard errors.")
    parser.add_argument("--save-conversations", action=argparse.BooleanOptionalAction, default=True, help="Save prompt/response markdown files for API calls.")
    parser.add_argument("--review-select", action="store_true", help="In --mode stage1, run pro review-select after local select.")
    parser.add_argument(
        "--init",
        action="store_true",
        help="Clear the --out generated outputs/checkpoints before running the selected mode.",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Clear the --out generated outputs/checkpoints and exit without running prompts/analyze/final.",
    )
    parser.add_argument("--force", action="store_true", help="Re-analyze items even if cached results exist.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    log(args, f"Pipeline started: mode={args.mode}, input={args.input}, out={args.out}")
    if args.init or args.init_only:
        reset_output_dir(args)
        if args.init_only:
            log(args, "Initialization finished; exiting because --init-only was used.")
            return 0
    if args.mode == "prompts":
        items = run_extract_and_prompt(args)
        log(args, f"Pipeline finished successfully: found {len(items)} candidate item(s).")
    elif args.mode == "analyze":
        run_analyze(args)
        log(args, f"Pipeline finished successfully: wrote analyses under {Path(args.out)}")
    elif args.mode == "final":
        run_final(args)
        log(args, f"Pipeline finished successfully: wrote final document under {Path(args.out)}")
    elif args.mode == "preflight":
        run_preflight(args)
        log(args, "Pipeline finished successfully: preflight completed.")
    elif args.mode == "segment":
        run_segment(args)
        log(args, f"Pipeline finished successfully: wrote segments under {Path(args.out)}")
    elif args.mode == "score":
        run_score(args)
        log(args, f"Pipeline finished successfully: wrote scores under {Path(args.out)}")
    elif args.mode == "select":
        run_select(args)
        log(args, f"Pipeline finished successfully: wrote selections under {Path(args.out)}")
    elif args.mode == "review-select":
        run_review_select(args)
        log(args, f"Pipeline finished successfully: wrote reviewed selections under {Path(args.out)}")
    elif args.mode == "enrich-selected":
        run_enrich_selected(args)
        log(args, f"Pipeline finished successfully: enriched selected items under {Path(args.out)}")
    elif args.mode == "assemble":
        run_assemble(args)
        log(args, f"Pipeline finished successfully: wrote assembled markdown under {Path(args.out)}")
    elif args.mode == "export-docx":
        # The export script lives alongside this file; add it to sys.path
        _scripts_dir = Path(__file__).resolve().parent
        if str(_scripts_dir) not in sys.path:
            sys.path.insert(0, str(_scripts_dir))
        import export_docx_splice
        docx_out = Path(args.out) / "docx_exports"
        created = export_docx_splice.export_selected(
            Path(args.out), sys.modules[__name__], log=lambda m: log(args, m)
        )
        log(args, f"Pipeline finished successfully: exported {len(created)} docx file(s) under {docx_out}")
    elif args.mode == "repair-answers":
        run_repair_answers(args)
        log(args, f"Pipeline finished successfully: repaired answers and re-assembled under {Path(args.out)}")
    elif args.mode == "quality-report":
        run_quality_report(args)
        log(args, f"Pipeline finished successfully: quality report written under {Path(args.out)}")
    elif args.mode == "stage1":
        run_stage1(args)
        log(args, f"Pipeline finished successfully: completed stage1 under {Path(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
