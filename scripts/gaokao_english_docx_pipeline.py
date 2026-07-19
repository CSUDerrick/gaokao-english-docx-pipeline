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
import functools
import contextlib
import csv
import json
import os
import random
import re
import shutil
import socket
import sys
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import answer_pairing
import deepseek_tokens
import input_precheck
import model_presets as mp
import net_tls
import notify as notify_mod
import providers as pv
from answer_explanation import (
    SECTION_QUESTION_RANGES,
    OfficialExplanations,
    question_numbers,
)
from bundle_paths import prompt_dir
from docx_blocks import DocxDoc, find_block_range, read_docx
from segment_quality import (
    READING_SECTIONS,
    evaluate_document,
    first_answer_run,
    missing_question_numbers,
)
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
EXPLAIN_PROMPT_VERSION = "explain_v1"
VOCAB_PROMPT_VERSION = "vocab_handout_v1"


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


def count_tokens(text: str) -> int:
    """Tokens as DeepSeek counts them (see ``deepseek_tokens``).

    Replaces a ``len(text) // 3`` estimate that was wrong in both directions —
    ~40% low on Chinese, ~40% high on English — which is no basis for quoting a
    teacher what a run will cost.
    """
    return deepseek_tokens.count(text)


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

# A compact answer run: "21—23. CBC", "1—5 BBCBA", "41-45 BDBAC".
# Groups: first question, last question, the letters.
_ANSWER_RUN = re.compile(r"(?m)^\s*(\d{1,2})\s*[-—~－–]+\s*(\d{1,2})\s*[.．、:：]?\s*([A-G]{2,})\b")
# Per-question explanations that every 答案 section of these papers carries.
_ANSWER_EXPL = re.compile(r"【导语】|【\d+题详解】|【详解】")

# Lines that separate the question body from the answer key but belong to
# neither: a "…答案 转载公众号…" credit line, or a bare repeat of the paper
# title. They sit right above the cut and would otherwise be printed at the end
# of the last question. Rules of underscores are deliberately NOT junk — those
# are the lines students write the continuation essay on.
_BODY_TRAILING_JUNK = (
    re.compile(r"答案.*(?:转载|公众号|感谢)"),
    re.compile(r"^(?![^\n]*[A-Za-z])[^\n]{4,60}(?:试题|试卷|密卷|联考|模拟考试|适应性考试)[^\n]{0,10}$"),
)


def _trim_trailing_junk(text: str, cut: int) -> int:
    """Move the answer-section boundary up over the junk lines just above it."""
    while cut > 0:
        prev_start = text.rfind("\n", 0, cut - 1) + 1
        line = text[prev_start:cut].strip()
        if line and not any(p.search(line) for p in _BODY_TRAILING_JUNK):
            break
        cut = prev_start
    return cut


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
    """Return the character position where the answer section begins, or None.

    Every detector below produces *candidates* and the earliest one wins.
    Returning as soon as one detector matched — the old behaviour — cut the
    paper too late on all three sample papers, because the two earliest signals
    (a header-less answer key like "21—23. CBC", and the 【21题详解】 blocks)
    were only looked for by the last stage. The answer section then stayed
    inside the body, and every later section start was free to land in it: the
    writing questions ended up swallowed by 语法填空, while the items labelled
    应用文/读后续写 held the model essays from the answer key.
    """
    candidates: list[int] = []
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

    candidates.extend(line_candidates)
    candidates.extend(inline_candidates)

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

    def _tail_marker_candidate() -> int | None:
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
        return None

    tail_marker = _tail_marker_candidate()
    if tail_marker is not None:
        candidates.append(tail_marker)

    # --- Stage 3: the answer key itself, with or without a header ---
    # The single most reliable signal: a compact answer run such as
    # "21—23. CBC" or "1—5 BBCBA". A question never looks like this, and every
    # paper has one — including the ones whose answer header is unmatchable
    # ("广东…密卷答案 转载公众号…") or absent entirely (江苏 just repeats the
    # paper title). The letter count must equal the question count, which makes
    # false positives essentially impossible.
    body_start = len(text) * 35 // 100
    for match in _ANSWER_RUN.finditer(text, body_start):
        first, last, letters = int(match.group(1)), int(match.group(2)), match.group(3)
        if last > first and len(letters) == last - first + 1:
            candidates.append(text.rfind("\n", 0, match.start()) + 1)
            break

    # --- Stage 4: per-question answer explanations ---
    explanation = _ANSWER_EXPL.search(text, body_start)
    if explanation:
        candidates.append(text.rfind("\n", 0, explanation.start()) + 1)

    if not candidates:
        return None
    return _trim_trailing_junk(text, min(candidates))


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
    official_explanation_blocks: list[int] | None = None,
    official_explanation_path: str = "",
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
        # The same, for the paper's own 【N题详解】 blocks. Only the indices are
        # stored: the text itself would ride along inside json.dumps(segment) into
        # the scoring prompt, inflating it and moving the cached prefix, and it is
        # cheap to re-read from the source when the teacher edition is built.
        # Empty when the paper never explained these questions.
        "official_explanation_blocks": official_explanation_blocks or [],
        # Which file those indices point into. Normally the paper itself, but a paper
        # can arrive as a student edition plus a *separate* answers document, and then
        # the explanation blocks live in that other file while the questions stay here.
        # Empty means "the same file as the questions", which is what every segment
        # written before this field existed means too.
        "official_explanation_path": official_explanation_path or "",
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


# A part/section heading: it introduces the section *below* it.
_SECTION_HEADING_LINE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十]+部分"
    r"|第[一二三四五六]节"
    r"|[（(]?\s*共\s*\d+\s*(?:小)?题"
    r"|[（(]?\s*满分\s*\d+\s*分"
    r")"
)


def _enforce_canonical_order(
    starts: list[tuple[int, str, str]], source_doc: str
) -> list[tuple[int, str, str]]:
    """Drop section starts that break the paper's canonical section order.

    A paper always runs 阅读A→D → 七选五 → 完形填空 → 语法填空 → 应用文 → 读后续写.
    A keyword that matches out of that order matched something it shouldn't have
    (historically: 应用文/读后续写 matching the *model essays* inside the answer
    key, which then stole the real writing questions from 语法填空). Keep the
    longest run that is consistent with the canonical order and discard the rest,
    rather than trusting a start whose position contradicts its own label.
    """
    if len(starts) < 2:
        return starts

    # Longest strictly-increasing subsequence over section_order, by position.
    best: list[int] = []
    prev: list[int] = [-1] * len(starts)
    length = [1] * len(starts)
    for i in range(len(starts)):
        for j in range(i):
            if section_order(starts[j][1]) < section_order(starts[i][1]) and length[j] + 1 > length[i]:
                length[i] = length[j] + 1
                prev[i] = j
    end = max(range(len(starts)), key=lambda i: length[i])
    while end != -1:
        best.append(end)
        end = prev[end]
    keep = set(best)

    dropped = [starts[i][2] for i in range(len(starts)) if i not in keep]
    if dropped:
        print(f"[segment] {source_doc}: 丢弃顺序异常的切分起点 {', '.join(dropped)}")
    return [starts[i] for i in sorted(keep)]


def local_segment_paper(
    source_doc: str,
    text: str,
    doc: DocxDoc | None = None,
    extra_starts: list[tuple[int, str, str]] | None = None,
    answer_doc: DocxDoc | None = None,
    answer_text: str = "",
    answer_path: str = "",
) -> list[dict]:
    """Split a paper into questions.

    ``extra_starts`` are section boundaries the keyword rules missed, recovered by
    :mod:`segment_repair` asking the model for a paragraph number. They are fed
    back in here — rather than being patched on afterwards — so the recovered
    section gets its answer key and block range by exactly the same code as every
    other section.

    ``answer_doc``/``answer_text`` are a *separate* answers document paired to this
    paper (a student edition whose answers came as their own file). When given, the
    answers and 详解 are read out of that document instead of this paper's tail — the
    questions still come from this one. Everything after that point is identical, so
    a paired paper and a self-contained one segment by the same code.
    """
    body, body_offset = trim_answer_tail_with_offset(text)
    if answer_doc is not None:
        # A dedicated answers document *is* the answer region, start to finish — so it is
        # taken whole. Running the tail detector over it instead cuts it at the first
        # header that matches, and on a real one that is 听力录音稿, which sits *below* the
        # 参考范文: the essays above it were thrown away and both writing sections came
        # back with no answer at all.
        answer_tail = answer_text[:30000]
        full_answers = extract_all_answers_from_full_text(answer_text)
        official = OfficialExplanations(answer_doc, 0)
    else:
        answer_tail = extract_answer_tail(text, max_chars=30000)
        # Use full-text scan for robust answer extraction (handles table format,
        # double-hyphen ranges, concatenated grammar answers, etc.)
        full_answers = extract_all_answers_from_full_text(text)
        # The answer section is cut out of the body above, but the teacher edition
        # clones the paper's own 【N题详解】 blocks back in, so index them before the
        # boundary is forgotten.
        official = OfficialExplanations(doc, _find_answer_section_start(text)) if doc is not None else None
    lines = line_spans(body)
    starts: list[tuple[int, str, str]] = []

    def add_start(section: str, label: str, idx: int | None) -> int | None:
        """Register a section start, moved up over its own heading lines.

        The keyword rules match the *content* line ("阅读下面材料，在空白处填入…"),
        but the heading above it ("第二节（共10小题；每小题1.5分，满分15分）")
        introduces this section, not the previous one. Anchoring on the content
        line left those headings dangling at the tail of the previous question.
        """
        if idx is None:
            return None
        floor = max((pos for pos, _, _ in starts), default=-1)
        at = idx
        while at > 0:
            prev = at - 1
            while prev > 0 and not lines[prev][2]:
                prev -= 1
            heading = lines[prev][2]
            if lines[prev][0] <= floor or not heading or len(heading) > 60:
                break
            if not _SECTION_HEADING_LINE.match(heading):
                break
            at = prev
        starts.append((lines[at][0], section, label))
        # The *original* index is returned: later sections search downwards from
        # it, and rewinding that cursor could re-match the heading we just took.
        return idx

    read_start = find_line_index(lines, r"阅读理解|阅读下列短文|第二部分\s*阅读")
    search_from = read_start or 0
    a = find_standalone_letter(lines, "A", search_from)
    b = find_standalone_letter(lines, "B", (a or search_from) + 1)
    c = find_standalone_letter(lines, "C", (b or search_from) + 1)
    d = find_standalone_letter(lines, "D", (c or search_from) + 1)
    for idx, section, label in [(a, "reading_a", "阅读A"), (b, "reading_b", "阅读B"), (c, "reading_c", "阅读C"), (d, "reading_d", "阅读D")]:
        add_start(section, label, idx)

    gap_idx = find_line_index(lines, r"七选五|选项中有两项(?:为)?多余选项")
    if gap_idx is None:
        # Some papers (江苏) never write "七选五" anywhere — the section is only
        # identifiable by its mark scheme, which is unique in the paper: 5
        # questions worth 12.5 marks. Searching below 阅读D keeps it away from
        # the listening part's own 第二节. Without this the section went missing
        # and had to be recovered by a paid model round-trip.
        gap_idx = find_line_index(
            lines, r"第二节.*共\s*5\s*(?:小)?题.*满分\s*12\.?5\s*分", (d or c or b or a or search_from) + 1
        )
    gap_idx = add_start("gap_filling", "七选五", gap_idx)
    # 语言知识运用 (江苏) vs 语言运用 (广东): missing the 知识 dropped the whole
    # "第三部分…" heading onto the tail of 七选五.
    cloze_idx = find_line_index(lines, r"完形填空|完型填空|语言(?:知识)?运用", (gap_idx or 0) + 1)
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
    starts = _enforce_canonical_order(starts, source_doc)
    segments: list[dict] = []
    # grammar is handled on its own branch below (its answers are words, not letters).
    answer_ranges = {k: v for k, v in SECTION_QUESTION_RANGES.items() if k != "grammar"}
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

        explanation_blocks: list[int] = []
        if official is not None:
            explanation_blocks = official.blocks_for(question_numbers(section, ak))

        segments.append(
            make_local_segment(
                source_doc, section, label, qtext, ak, answer_source,
                source_path=source_path, source_blocks=source_blocks,
                official_explanation_blocks=explanation_blocks,
                # Only set when the answers came from their own document; empty keeps
                # the old meaning, "the same file the questions came from".
                official_explanation_path=answer_path if answer_doc is not None else "",
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
    provider: str = pv.DEFAULT_PROVIDER,
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
    history: list[dict] | None = None,
    insecure_ssl: bool = False,
) -> ChatResult:
    # Claude does not speak chat/completions, and its OpenAI-compatibility shim drops
    # thinking and prompt caching. The SDK path is OpenAI's SDK, so it cannot serve
    # Claude either — the HTTP adapter is the only route.
    if pv.get(provider).protocol == pv.ANTHROPIC:
        client_mode = "http"

    if client_mode in {"auto", "sdk"}:
        try:
            return call_chat_completion_sdk(
                prompt,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
                timeout=timeout,
                max_tokens=max_tokens,
                max_retries=max_retries,
                history=history,
            )
        except ImportError:
            if client_mode == "sdk":
                raise RuntimeError(
                    "The OpenAI SDK is not installed. Install it with `pip3 install openai`, "
                    "or rerun with `--client http` to use the built-in HTTP fallback."
                )

    return call_chat_completion_http(
        prompt,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        timeout=timeout,
        max_tokens=max_tokens,
        max_retries=max_retries,
        history=history,
        insecure_ssl=insecure_ssl,
    )


SYSTEM_PROMPT = "你是严谨的高三英语教研分析助手。请严格遵守用户要求输出。"


def chat_payload(
    prompt: str,
    *,
    provider: str,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    max_tokens: int | None = None,
    history: list[dict] | None = None,
) -> tuple[dict, dict]:
    """Build one OpenAI-shaped request. Returns ``(payload, extras)``.

    ``extras`` are the vendor extensions that are *not* OpenAI-standard — DeepSeek's
    ``thinking``, GLM's and Qwen's ``enable_thinking``. Over raw HTTP they belong in the
    same JSON body; through the OpenAI SDK they have to travel in ``extra_body`` or the
    SDK drops them on the floor. Keeping them separate here is what makes both paths work.

    Everything vendor-specific is decided by ``providers.request_fields`` — this used to
    write ``reasoning_effort`` and ``thinking`` unconditionally, which is why the app
    could only ever talk to DeepSeek.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    standard, extras = pv.request_fields(
        provider,
        model,
        effort=reasoning_effort,
        thinking=thinking,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    payload.update(standard)
    return payload, extras


def anthropic_payload(
    prompt: str,
    *,
    provider: str,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    max_tokens: int | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Build one Claude Messages-API request.

    Not the OpenAI-compatibility layer: Anthropic's own docs say that layer supports
    neither extended thinking nor prompt caching, and both are load-bearing here
    (决策 8 — a cache hit costs 1/120 of a miss, which is the only reason a whole paper
    in one growing conversation is affordable).

    Shape differences from chat/completions: ``system`` is a top-level field rather than
    messages[0], and ``max_tokens`` is required rather than optional.
    """
    spec = pv.model_spec(provider, model)
    standard, _ = pv.request_fields(
        provider,
        model,
        effort=reasoning_effort,
        thinking=thinking,
        temperature=temperature,
        max_tokens=max_tokens or min(spec.max_output, 8192),
    )
    payload = {
        "model": model,
        # Cache the frozen instruction prefix. Anthropic's cache is explicit, unlike
        # DeepSeek's automatic one, so without this every turn of a conversation would
        # re-pay full price for the whole history.
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [*(history or []), {"role": "user", "content": prompt}],
        "stream": False,
    }
    payload.update(standard)
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
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    timeout: int,
    max_tokens: int | None,
    max_retries: int,
    history: list[dict] | None = None,
) -> ChatResult:
    # base_url is the API root (https://api.deepseek.com), not the /chat/completions
    # path — that is what every OpenAI-compatible vendor's own docs pass.
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    kwargs, extras = chat_payload(
        prompt,
        provider=provider,
        model=model,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        max_tokens=max_tokens,
        history=history,
    )
    if extras:
        # Vendor extensions (DeepSeek's `thinking`, GLM's/Qwen's `enable_thinking`) must
        # go through extra_body — passed as ordinary kwargs the SDK silently discards
        # them, and the model then answers without thinking at all.
        kwargs["extra_body"] = extras

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(**kwargs)
            message = response.choices[0].message
            usage = response.usage.model_dump() if getattr(response, "usage", None) else None
            return ChatResult(
                content=message.content or "",
                reasoning=getattr(message, "reasoning_content", "") or getattr(message, "reasoning", "") or "",
                usage=pv.normalize_usage(pv.OPENAI, usage),
                client_used="sdk",
            )
        except Exception as exc:
            last_error = exc
            status_code = exception_status_code(exc)
            if not is_retryable_status(status_code):
                break
            if attempt < max_retries:
                _sleep_or_cancel(retry_delay_seconds(attempt, exception_retry_after(exc)))
    raise RuntimeError(f"SDK API call failed after {max_retries} attempts: {last_error}")


def completion_endpoint(base_url: str, provider: str = pv.DEFAULT_PROVIDER) -> str:
    stripped = base_url.rstrip("/")
    if pv.get(provider).protocol == pv.ANTHROPIC:
        return stripped if stripped.endswith("/messages") else f"{stripped}/v1/messages"
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def auth_headers(provider: str, api_key: str) -> dict:
    """How this vendor wants to be told who you are.

    Anthropic does not use a Bearer token: the key goes in ``x-api-key`` and the API
    version is a required header. Sending it the OpenAI way is a 401.
    """
    common = {"Content-Type": "application/json", "Accept": "application/json"}
    if pv.get(provider).protocol == pv.ANTHROPIC:
        return {**common, "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {**common, "Authorization": f"Bearer {api_key}"}


# `max` produces a much longer chain of thought — and reasoning tokens are billed and
# *counted* as output. This is the trap in 决策 9: with thinking on, a 1200-token cap
# once left a scoring call literally zero tokens in which to write its JSON, the reply
# was truncated, and the item was silently ranked as if it had scored zero. Raising the
# cap along with the effort is not a nicety; without it, `max` would fail every call it
# was meant to improve.
MAX_EFFORT_TOKEN_MULTIPLIER = 3
MAX_EFFORT_TOKEN_CEILING = 64_000


def effective_max_tokens(
    base: int | None,
    effort: str,
    provider: str = pv.DEFAULT_PROVIDER,
    model: str = "",
) -> int | None:
    """Grow the output cap when this model is being asked to think its hardest.

    "Hardest" is per-model, not the literal string ``max``: on OpenAI and Claude the deep
    preset asks for ``xhigh``, whose chain of thought is every bit as long. Comparing
    against a hardcoded ``"max"`` would leave those two running the deep preset under the
    shallow preset's cap — 决策 9's bug wearing a new hat.

    A model with no strength dial at all (GLM, Qwen) gets the bigger cap too. We cannot
    tell from the effort string how long it intends to think, and the asymmetry is stark:
    a cap that is too high costs nothing (you are billed for tokens produced, not tokens
    allowed), while one that is too low truncates the JSON and kills the stage.

    Clamped to what the model will actually accept — asking glm-5.2 for 48000 output
    tokens when it caps at 32000 is just a 400.
    """
    if not base:
        return base
    spec = pv.model_spec(provider, model)
    if not pv.is_deepest(effort, provider, model):
        return min(base, spec.max_output)
    grown = min(base * MAX_EFFORT_TOKEN_MULTIPLIER, MAX_EFFORT_TOKEN_CEILING)
    return min(grown, spec.max_output)


# --------------------------------------------------------------------------- cancel


class Cancelled(Exception):
    """The teacher pressed 取消."""


CANCEL = threading.Event()

# Every HTTPS connection currently waiting on DeepSeek. Cancelling closes them, which
# is the only way to interrupt a blocking read: the worker threads are parked inside
# getresponse()/read() and cannot be signalled. Without this, 取消 did nothing until the
# whole stage finished — minutes, on the explanation stage.
_LIVE_CONNECTIONS: set = set()
_CONNECTIONS_LOCK = threading.Lock()


def set_cancel_event(event: threading.Event) -> None:
    """Let the GUI share its own event, so one flag drives the whole run."""
    global CANCEL
    CANCEL = event


def request_cancel() -> None:
    CANCEL.set()
    with _CONNECTIONS_LOCK:
        connections = list(_LIVE_CONNECTIONS)
    for connection in connections:
        # DeepSeek has no "stop generating" endpoint for a non-streaming request, so
        # hanging up is the abort. Tokens already produced server-side may still be
        # billed — the UI says so rather than pretending otherwise.
        #
        # shutdown() first, and it is not optional: close() only drops this thread's
        # handle on the socket, and a worker already parked inside recv() on the same fd
        # stays parked. shutdown(SHUT_RDWR) tears the connection down underneath it and
        # forces that read to return at once, which is the whole point of 取消.
        sock = getattr(connection, "sock", None)
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            connection.close()


def reset_cancel() -> None:
    CANCEL.clear()
    with _CONNECTIONS_LOCK:
        _LIVE_CONNECTIONS.clear()


def raise_if_cancelled() -> None:
    if CANCEL.is_set():
        raise Cancelled("已取消")


def post_json(
    endpoint: str,
    payload: dict,
    headers: dict,
    *,
    timeout: int,
    max_retries: int = 3,
    insecure_ssl: bool = False,
) -> dict:
    """POST one JSON request, over a connection we can hang up on, and retry it.

    Built on ``http.client`` rather than ``urllib.request.urlopen`` for two reasons,
    both of which the teacher hits in practice:

    * **Cancel.** A worker parked inside ``urlopen`` cannot be interrupted — 取消 did
      nothing until the whole stage finished. Holding the connection object lets
      ``request_cancel()`` close the socket from the UI thread, which makes the
      blocked read raise at once.
    * **TLS.** This is where the SSL context goes, so a Mac whose network re-signs
      HTTPS (school proxy, antivirus) verifies against the Keychain instead of
      failing with "self-signed certificate in certificate chain". See ``net_tls``.

    Every provider goes through here, which is the point: a new vendor gets cancellation
    and Keychain verification because it cannot avoid them, not because someone
    remembered to wire them up again.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    parts = urllib.parse.urlsplit(endpoint)
    path = parts.path + (f"?{parts.query}" if parts.query else "")

    last_error: object = None
    for attempt in range(1, max_retries + 1):
        raise_if_cancelled()
        connection = _open_connection(parts, timeout=timeout, insecure_ssl=insecure_ssl)
        try:
            connection.request("POST", path, body=data, headers=headers)
            response = connection.getresponse()
            body_bytes = response.read()

            if response.status >= 400:
                detail = body_bytes.decode("utf-8", errors="replace")
                last_error = f"HTTP {response.status} {response.reason}: {detail or '<empty response body>'}"
                if not is_retryable_status(response.status):
                    break
                if attempt < max_retries:
                    _sleep_or_cancel(retry_delay_seconds(attempt, response.getheader("Retry-After")))
                continue

            return json.loads(body_bytes.decode("utf-8"))
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            # Hanging up on a cancel surfaces here as a broken socket. It is not a
            # network failure to retry — it is what the teacher asked for.
            raise_if_cancelled()
            last_error = exc
            if net_tls.is_certificate_error(exc):
                raise RuntimeError(f"{net_tls.CERTIFICATE_HELP}\n（原始错误：{exc}）") from exc
            if attempt < max_retries:
                _sleep_or_cancel(retry_delay_seconds(attempt))
        finally:
            _close_connection(connection)

    raise RuntimeError(f"API call failed after {max_retries} attempts: {last_error}")


def _openai_result(body: dict) -> ChatResult:
    message = body["choices"][0]["message"]
    return ChatResult(
        content=message.get("content") or "",
        reasoning=message.get("reasoning_content") or message.get("reasoning") or "",
        usage=pv.normalize_usage(pv.OPENAI, body.get("usage")),
        client_used="http",
    )


def _anthropic_result(body: dict) -> ChatResult:
    """Flatten a Messages-API reply into the same ChatResult the rest of the code reads.

    The response is a list of content blocks, not a single string. Thinking arrives as
    its own block type and must not be concatenated into the answer — the JSON parser
    downstream would choke on it.
    """
    text: list[str] = []
    reasoning: list[str] = []
    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text.append(block.get("text") or "")
        elif block.get("type") == "thinking":
            reasoning.append(block.get("thinking") or "")
    return ChatResult(
        content="".join(text),
        reasoning="".join(reasoning),
        usage=pv.normalize_usage(pv.ANTHROPIC, body.get("usage")),
        client_used="http",
    )


def call_chat_completion_http(
    prompt: str,
    *,
    provider: str = pv.DEFAULT_PROVIDER,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    reasoning_effort: str,
    thinking: str,
    timeout: int,
    max_tokens: int | None,
    max_retries: int = 3,
    history: list[dict] | None = None,
    insecure_ssl: bool = False,
) -> ChatResult:
    """One completion, in whichever protocol this provider speaks."""
    spec = pv.get(provider)
    kwargs = dict(
        provider=provider,
        model=model,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        max_tokens=max_tokens,
        history=history,
    )

    if spec.protocol == pv.ANTHROPIC:
        payload = anthropic_payload(prompt, **kwargs)
        parse = _anthropic_result
    else:
        payload, extras = chat_payload(prompt, **kwargs)
        # Over raw HTTP a vendor extension is just another top-level field; the
        # extra_body wrapper only exists because the OpenAI SDK needs it.
        payload.update(extras)
        parse = _openai_result

    body = post_json(
        completion_endpoint(base_url, provider),
        payload,
        auth_headers(provider, api_key),
        timeout=timeout,
        max_retries=max_retries,
        insecure_ssl=insecure_ssl,
    )
    try:
        return parse(body)
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{spec.label} 返回了预期之外的结构：{str(body)[:300]}") from exc


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _bypass_proxy(host: str) -> bool:
    """Should this host skip the proxy?

    ``urllib.request.proxy_bypass`` says *no* for 127.0.0.1 on this Mac — macOS only
    lists the bypasses the user typed, and nobody types localhost. Sending a loopback
    address out to a proxy is never what anyone means.
    """
    return host in _LOCAL_HOSTS or bool(urllib.request.proxy_bypass(host))


def _open_connection(parts, *, timeout: int, insecure_ssl: bool) -> http.client.HTTPConnection:
    """Connect to the API — through the system proxy, if the machine has one.

    ``urllib.request.urlopen`` did this for free: it reads the proxy out of the
    environment and out of macOS System Settings. ``http.client`` does not, so moving to
    it (for cancellation and for the SSL context) silently dropped proxy support. That
    is not an edge case here: the very machine this is being written on routes through
    127.0.0.1:1082, and a school network that re-signs TLS — the thing this release
    exists to fix — is usually re-signing it *at a proxy*. Losing the proxy would have
    broken exactly the teacher we were trying to unbreak.
    """
    host = parts.hostname
    port = parts.port or (80 if parts.scheme == "http" else 443)
    context = net_tls.context(insecure_ssl)

    proxy = urllib.request.getproxies().get(parts.scheme)
    if proxy and not _bypass_proxy(host):
        # Talk to the proxy, then CONNECT through it; the TLS handshake still happens
        # against the real host, so verification is unchanged.
        endpoint = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                endpoint.hostname, endpoint.port or 80, timeout=timeout, context=context
            )
        else:
            connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port or 80, timeout=timeout)
        connection.set_tunnel(host, port)
    elif parts.scheme == "http":
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    else:
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)

    with _CONNECTIONS_LOCK:
        _LIVE_CONNECTIONS.add(connection)
    return connection


def _close_connection(connection) -> None:
    with _CONNECTIONS_LOCK:
        _LIVE_CONNECTIONS.discard(connection)
    with contextlib.suppress(Exception):
        connection.close()


def _sleep_or_cancel(seconds: float) -> None:
    """Back off, but wake immediately on 取消.

    ``time.sleep`` here could park a worker for up to 90 seconds after a 429, and
    nothing could wake it — pressing 取消 during a rate-limit backoff appeared to do
    nothing at all.
    """
    if CANCEL.wait(seconds):
        raise Cancelled("已取消")


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


def build_review_select_prompt(
    candidates: list[dict],
    target_count: int,
    section: str,
    rejected: list[str] | None = None,
) -> str:
    # Re-running the review with the same scores and the same model mostly reproduces
    # the same picks — so "老师对这批不满意" has to be said out loud, or the 重新选题
    # button looks like it did nothing.
    reselect = ""
    if rejected:
        reselect = f"""
【重要】老师对上一轮选出的这些题**不满意**，要求换一批：
{json.dumps(rejected, ensure_ascii=False)}
请尽量避开它们，从其余候选里另选。只有当某道题确实明显优于所有替代品时，才可以保留它，
并在 review_reason 里说明为什么非它不可。
"""

    return f"""任务版本：{REVIEW_SELECT_PROMPT_VERSION}

你是一名高三英语教研组长。请在程序按评分初筛出的候选题中，做最终人工式复核选择。
请严格输出 JSON，不要输出 Markdown，不要添加解释性前后缀。
{reselect}

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


# The two ways to build the handout. Not a right and a wrong one — a teaching choice,
# so the teacher makes it (基础模式 has the switch):
#
#   CHUNKED (完整) — ask about each selected question on its own, then merge. The words
#       come only from the questions the student is actually holding, so the handout and
#       the paper match exactly. But the model judges each passage in isolation.
#
#   WHOLE (困难) — read the paper end to end and pick from all of it. The model can tell
#       whether a word is genuinely hard *for this paper*, which is the judgement it is
#       being asked to make; the cost is that the list can name words from questions the
#       student's copy does not contain.
VOCAB_CHUNKED = "chunked"
VOCAB_WHOLE = "whole"
VOCAB_MODES = (VOCAB_CHUNKED, VOCAB_WHOLE)

# How many words one *question* may contribute (chunked), against one whole *paper*
# (whole). A paper is one coherent body of text and deserves one budget; 18 questions
# each allowed 20 words is why the old handout ran long and repeated itself.
VOCAB_MAX_ITEM_WORDS = 20
VOCAB_MAX_ITEM_FORMS = 15
VOCAB_MAX_READING_WORDS = 40
VOCAB_MAX_WORD_FORMS = 25

# The largest single vocab reply seen on a real run of the three sample papers at the
# deep setting: 38,934 output tokens, of which 37,022 (95%) were reasoning. Measured, not
# guessed — it is what --vocab-max-tokens is sized against, and what tells us in advance
# that a model with a smaller output ceiling (GLM caps at 32k) may not be able to finish.
VOCAB_DEEP_OBSERVED_PEAK = 38_934

# Every vocab prompt says the same two things about quotes, and it has to say both.
# Saying only "don't use English double quotes inside strings" made flash apply the rule
# to the JSON *syntax* and emit {“word”: “abandon”} — Chinese quotes as delimiters,
# the whole reply invalid. It really did die on item 9 of a real run (决策 27).
VOCAB_JSON_RULES = """请严格输出 JSON，不要输出 Markdown，不要输出表格，不要添加解释性前后缀。
JSON 的语法符号（花括号、方括号、冒号，以及包裹键名和值的引号）必须是标准英文半角字符；
只有字符串**内部**要引用原文时才用中文引号“”，不要在字符串内部用英文双引号（会让整份输出变成非法 JSON）。
正确示例：{"word": "abandon", "pos": "v.", "meaning": "放弃"}
错误示例：{“word”: “abandon”}   ← 键名和值的引号被写成了中文引号，整份 JSON 作废"""

VOCAB_CRITERIA = f"""【任务一：提取重难点阅读词汇】
筛选标准：
1. 排除高考英语考纲内的基础词汇（3500 词）。
2. 挑选出超出考纲，但在高级别英语阅读中复现率较高、对理解文章长难句起关键作用、
   值得学生积累的词汇（如熟词生义、高级动词、核心抽象名词等）。
   极僻冷门且无积累价值的专有名词请略过。

【任务二：提取语法词汇变形（重点派生与屈折变化）】
筛选标准：
1. 找出文本中具有代表性的词汇变形，特别是那些在高考“语法填空”和“短文改错”题型中极易考查的考点。
2. 包含但不限于：动词转名词/形容词、形容词转副词/名词、不规则动词的过去式/过去分词、否定前缀等。

JSON 顶层结构：
{{
  "reading_words": [
    {{"word": "英文单词", "pos": "词性", "meaning": "准确的中文释义"}}
  ],
  "word_forms": [
    {{"base": "基础词汇", "base_pos": "词性", "derived": "变形后的词汇",
      "derived_pos": "变形后的词性", "note": "考点说明（例如：v. 变 n.，加后缀 -tion）"}}
  ]
}}

词性统一用缩写：n. / v. / adj. / adv. / prep. / conj. / phr."""


def build_vocab_item_prompt(segment: dict) -> str:
    """完整（分块）: the handout asked one selected question at a time.

    Only the question text is sent — ``segment_body`` never carries the answer key, which
    is what keeps a student-facing handout clean without anyone having to remember to
    strip anything.

    The model sees this passage and nothing else, so it cannot weigh a word against the
    rest of the paper. What it buys instead is exactness: every word on the sheet comes
    from a question the student is actually holding.
    """
    return f"""任务版本：{VOCAB_PROMPT_VERSION}

你是一名高三英语教研老师。请阅读下面的文章，完成两项任务。
{VOCAB_JSON_RULES}

{VOCAB_CRITERIA}

数量要求：
- reading_words 最多 {VOCAB_MAX_ITEM_WORDS} 项。
- word_forms 最多 {VOCAB_MAX_ITEM_FORMS} 项。

题目ID：{segment.get("item_id", "")}

文章正文：
{segment_body(segment)}
"""


def build_vocab_paper_prompt(paper: str, text: str, *, part: int = 1, total: int = 1) -> str:
    """困难（整卷）: the handout asked of a whole paper rather than one question.

    The model reads the paper end to end, which is how a teacher would decide, and can
    therefore tell whether a word is genuinely hard *for this paper*. The passage it is
    given has already had the answer section cut off (see vocab_one_paper) — unlike the
    per-question path, that does not come for free here.

    Split into parts only when a paper does not fit the model's context budget. On a
    1M-window model it never does: 40% of the window is 336k tokens and a paper is about
    20k, so a paper is one turn. The split matters on a small-window model, and the parts
    share one conversation so the model still sees what it already picked.

    The answer key is not sent. This is a student handout; nothing that reveals an answer
    may influence it or leak into it.
    """
    scope = "" if total == 1 else f"（这是本卷的第 {part}/{total} 部分，稍后会让你汇总）"
    return f"""任务版本：{VOCAB_PROMPT_VERSION}

你是一名高三英语教研老师。下面是一整份高考英语模拟卷的正文{scope}。
请通读全文，站在「这份卷子里哪些词真正值得学生记」的角度，完成两项任务。
{VOCAB_JSON_RULES}

{VOCAB_CRITERIA}

数量要求：
- reading_words 最多 {VOCAB_MAX_READING_WORDS} 项。
- word_forms 最多 {VOCAB_MAX_WORD_FORMS} 项。
- 同一个词只出现一次；宁可少给几个真正的重难点，也不要凑数。

试卷：{paper}

试卷正文：
{text}
"""


def build_vocab_merge_prompt(paper: str, parts: int) -> str:
    """The final turn, when one paper had to be split.

    Asked inside the same conversation, so every part it is merging is already in the
    history — the prefix is a cache hit and this turn costs almost nothing. Local dedup
    happens anyway at export time; what this buys is *ranking*, which only a model that
    has read the whole paper can do.
    """
    return f"""任务版本：{VOCAB_PROMPT_VERSION}

以上是同一份试卷「{paper}」分 {parts} 部分给出的候选词汇。
现在请你把它们合并成这份卷子最终的一张表：
1. 去重（同一个词只保留一次，保留释义最准确的那条）。
2. 按「对学生的重难点价值」排序，最值得记的排在最前面。
3. 删掉其实属于考纲 3500 词的基础词，以及没有积累价值的生僻专有名词。

{VOCAB_JSON_RULES}

数量要求：
- reading_words 最多 {VOCAB_MAX_READING_WORDS} 项。
- word_forms 最多 {VOCAB_MAX_WORD_FORMS} 项。

只输出合并后的 JSON，结构与之前每一部分相同（reading_words / word_forms 两个键）。
"""


def chunk_by_tokens(text: str, budget: int) -> list[str]:
    """Split a paper into pieces that each fit the conversation's token budget.

    Splits on blank lines, then on single lines — never mid-sentence, because a passage
    cut in half produces vocabulary judgements made on half a sentence.

    The budget has to cover the *whole conversation*, not one turn: every part stays in
    the history so the model can merge them at the end. So each part gets a fraction of
    the budget, leaving room for the parts that follow and for their replies.

    Returns one chunk for anything that already fits, which on a 1M-window model is
    every real paper.
    """
    text = text.strip()
    if not text:
        return []
    if count_tokens(text) <= budget:
        return [text]

    # Room for roughly four parts plus their answers inside one budget. More parts than
    # that and the merge turn is reasoning over a conversation that is mostly its own
    # output, which is where quality falls off.
    per_chunk = max(budget // 5, 2_000)

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_tokens = 0

    for block in _split_for_chunking(text, per_chunk):
        block_tokens = count_tokens(block)
        if current and current_tokens + block_tokens > per_chunk:
            flush()
        current.append(block)
        current_tokens += block_tokens
    flush()
    return [chunk for chunk in chunks if chunk]


def _split_for_chunking(text: str, limit: int) -> list[str]:
    """Paragraphs, falling back to lines for a paragraph that is itself too big."""
    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if count_tokens(paragraph) <= limit:
            blocks.append(paragraph)
            continue
        # One enormous paragraph (an unbroken reading passage): fall back to lines. If a
        # single line still will not fit, it goes out oversized rather than being cut
        # mid-sentence — a truncated sentence is worse than a slightly-over-budget turn.
        blocks.extend(line.strip() for line in paragraph.splitlines() if line.strip())
    return blocks


# Which prompt file explains which section. Reading A-D share one: the thinking is
# the same, only the passage changes.
EXPLAIN_PROMPT_FILES = {
    "reading_a": "reading.md",
    "reading_b": "reading.md",
    "reading_c": "reading.md",
    "reading_d": "reading.md",
    "gap_filling": "gap_filling.md",
    "cloze": "cloze.md",
    "grammar": "grammar.md",
    "practical_writing": "practical_writing.md",
    "continuation_writing": "continuation_writing.md",
}

# How many questions to ask for in one turn. Cloze is 15 questions and grammar is
# 10; asking for all of them at once produced a JSON long enough to run into
# max_tokens with thinking on (decision 9), and a truncated reply is a hard stop.
# Reading and 七选五 are 3-5 questions, which fits comfortably in one turn.
EXPLAIN_CHUNK_SIZE = 5

NO_OFFICIAL_EXPLANATION = "原卷未提供官方解析。请你独立写出完整解析。"


def load_explain_prompt(section: str) -> str:
    """The shared style rules plus this section's own instructions.

    Markdown on disk rather than a string literal in here, so the teacher can read
    and reword the prompts without touching Python. Placeholders are ``{{NAME}}``
    (not ``str.format``) because the templates contain literal JSON braces.
    """
    filename = EXPLAIN_PROMPT_FILES.get(section)
    if not filename:
        raise RuntimeError(f"No explanation prompt for section {section!r}")
    directory = prompt_dir()
    style = (directory / "_style.md").read_text(encoding="utf-8")
    body = (directory / filename).read_text(encoding="utf-8")
    return f"{style}\n\n---\n\n{body}"


def build_explain_prompt(
    segment: dict,
    section: str,
    numbers: list[int],
    official_explanation: str,
) -> str:
    template = load_explain_prompt(section)
    fields = {
        "TASK_VERSION": EXPLAIN_PROMPT_VERSION,
        "SECTION_DISPLAY": section_display(section),
        "ITEM_ID": str(segment.get("item_id", "")),
        "QUESTION_NUMBERS": "、".join(str(n) for n in numbers) or "（本题无题号）",
        "QUESTION_TEXT": segment_body(segment),
        "ANSWER_KEY": answer_key_text(segment.get("answer_key")),
        "OFFICIAL_EXPLANATION": official_explanation.strip() or NO_OFFICIAL_EXPLANATION,
    }
    for key, value in fields.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return f"任务版本：{EXPLAIN_PROMPT_VERSION}\n\n{template}"


def explain_chunks(section: str, numbers: list[int]) -> list[list[int]]:
    """Split an item's questions into one list per conversation turn."""
    if not numbers:
        return [[]]  # writing: no numbered questions, but still one turn
    if section in {"cloze", "grammar"}:
        return [numbers[i : i + EXPLAIN_CHUNK_SIZE] for i in range(0, len(numbers), EXPLAIN_CHUNK_SIZE)]
    return [numbers]


# The name a stage is logged under is not always the name of its CLI flag.
# `review` and `review_select` are both spellings in use — apply_preset says the first,
# the run logs say the second — so both map to the same flag.
STAGE_MODEL_FLAG = {
    "segment": "segment_model",
    "score": "score_model",
    "review": "review_model",
    "review_select": "review_model",
    "enrich": "enrich_model",
    "explain": "explain_model",
    # vocab used to point at enrich_model, which meant the词汇表 ran on whatever the
    # 备课笔记 stage happened to be set to — and 备课笔记 has had no consumer since
    # 决策 14. It has its own flag now.
    "vocab": "vocab_model",
}


def stage_model_name(args: argparse.Namespace, kind: str) -> str:
    attr = STAGE_MODEL_FLAG.get(kind, f"{kind}_model")
    return str(getattr(args, attr, None) or getattr(args, "model", ""))


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
        # "review_select" is logged under that name but its flag is --review-model,
        # so the naive kind+"_model" lookup silently fell back to args.model and
        # recorded the wrong model in every review log.
        f"- model: `{stage_model_name(args, kind)}`",
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

    Both backends produce the same OcrBlock list and go through the same
    ``blocks_to_docx``, so a MinerU paper and a PaddleOCR paper are laid out identically
    and everything downstream is unaware there are two.

    The converted files land under the output directory, never next to the teacher's
    originals — input is never written to.
    """
    pdfs = collect_pdf(Path(args.input))
    if not pdfs:
        return []

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    backend = getattr(args, "pdf_backend", "paddle")

    converted_dir = out_dir / "pdf_converted"
    ensure_dir(converted_dir)
    converted: list[Path] = []
    for pdf in pdfs:
        target = converted_dir / f"{pdf.stem}.docx"
        if target.exists():
            log(args, f"  reusing existing OCR result for {pdf.name}")
            converted.append(target)
            continue

        raise_if_cancelled()
        if backend == "mineru":
            import mineru_ingest

            log(args, f"  OCR {pdf.name} via MinerU …")
            converted.append(
                mineru_ingest.ingest_pdf(
                    pdf,
                    converted_dir,
                    token=args.mineru_token,
                    # MinerU is async, so this waits in a poll loop. Route the wait
                    # through the cancel event or 取消 does nothing until it finishes.
                    sleep=_sleep_or_cancel,
                )
            )
        else:
            import pdf_ingest

            log(args, f"  OCR {pdf.name} via PaddleOCR-VL …")
            converted.append(
                pdf_ingest.ingest_pdf(
                    pdf,
                    converted_dir,
                    base_url=args.paddle_base_url,
                    token=args.paddle_token,
                    # PaddleOCR is a poll loop now too (it used to be one blocking POST,
                    # which is why it never needed this). Same reason as MinerU: without
                    # it 取消 does nothing until the OCR finishes on its own.
                    sleep=_sleep_or_cancel,
                    log=lambda message: log(args, message),
                )
            )
    return converted


_LEDGER_LOCK = threading.Lock()


def record_usage(
    args: argparse.Namespace,
    *,
    stage: str,
    item_id: str,
    model: str,
    thinking: str,
    effort: str,
    result: ChatResult,
    seconds: float,
) -> None:
    """Append one line per API call to ``<out>/usage.jsonl``.

    One ledger, written where the call actually happens, because the alternatives all
    lied. Cost used to be reconstructed by scanning ``scores/``, ``explanations/`` and
    ``vocab/`` for embedded usage blocks and recovering the model by *grepping the
    saved markdown transcripts* — so turning off 保留中间产物 made every run look like
    it ran on pro, and segment and review-select (which have no per-item output dir)
    were never counted at all.

    Writing it here also means the durations needed for the time estimate come from
    the same place as the tokens, rather than being measured twice.
    """
    out_dir = getattr(args, "out", "")
    if not out_dir:
        return
    # Already in one shape: every adapter runs its vendor's usage block through
    # providers.normalize_usage, so nothing here has to know that OpenAI nests the
    # cached count one level down and Anthropic calls it something else entirely.
    usage = result.usage if isinstance(result.usage, dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    entry = {
        "ts": time.time(),
        "stage": stage,
        "item_id": item_id,
        # Priced per (provider, model): the same model name can exist at two vendors at
        # two prices, and a run may legitimately switch providers mid-flight.
        "provider": getattr(args, "provider", pv.DEFAULT_PROVIDER),
        "model": model,
        "thinking": thinking,
        "effort": effort,
        "prompt_tokens": prompt_tokens,
        "cache_hit": cache_hit,
        "cache_miss": int(usage.get("prompt_cache_miss_tokens") or max(prompt_tokens - cache_hit, 0)),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
        "seconds": round(seconds, 3),
    }
    path = Path(out_dir) / "usage.jsonl"
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _LEDGER_LOCK:  # every stage writes from a thread pool
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def call_stage_model(
    args: argparse.Namespace,
    prompt: str,
    *,
    model: str,
    reasoning_effort: str,
    thinking: str,
    max_tokens: int | None = None,
    history: list[dict] | None = None,
    stage: str = "",
    item_id: str = "",
) -> ChatResult:
    # Checked before the request rather than only between items: on the explanation
    # stage a single call can run for half a minute, and 取消 has to mean 取消.
    raise_if_cancelled()
    started = time.time()
    result = call_chat_completion(
        prompt,
        provider=getattr(args, "provider", pv.DEFAULT_PROVIDER),
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
        history=history,
        insecure_ssl=bool(getattr(args, "insecure_ssl", False)),
    )
    record_usage(
        args,
        stage=stage,
        item_id=item_id,
        model=model,
        thinking=thinking,
        effort=reasoning_effort,
        result=result,
        seconds=time.time() - started,
    )
    return result


# DeepSeek's context cache is an automatic *prefix* cache, not a session: two
# requests share cached tokens exactly as far as their message lists are
# byte-identical from the start. So asking every question of one paper inside a
# single growing conversation makes each turn's prefix (system + instructions +
# all previous turns) a cache hit. A hit costs $0.003625/M against $0.435/M for a
# miss on v4-pro — 120x — so the extra prefix tokens are close to free.
#
# How far it may grow is now the model's business, not a constant: 200k was 20% of
# DeepSeek's 1M window, and means nothing to a 200k-window model. providers.py works
# it out from the real window — 40% for vocab, 50% elsewhere — because quality falls
# off long before the window is actually full.


class Conversation:
    """One conversation with one paper's model, reused across turns to keep the cache warm."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        model: str,
        reasoning_effort: str,
        thinking: str,
        stage: str = "",
    ):
        self.args = args
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.thinking = thinking
        self.stage = stage
        self.history: list[dict] = []
        self.usages: list[dict] = []
        self.tokens = 0
        self.turns = 0
        self.restarts = 0
        self.spec = pv.model_spec(getattr(args, "provider", pv.DEFAULT_PROVIDER), model)
        self.budget = pv.conversation_budget(self.spec, stage)

    def ask(self, prompt: str, *, max_tokens: int | None = None, item_id: str = "") -> ChatResult:
        if self.tokens >= self.budget:
            self.history = []
            self.tokens = 0
            self.restarts += 1

        result = call_stage_model(
            self.args,
            prompt,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            thinking=self.thinking,
            max_tokens=max_tokens,
            history=list(self.history),
            stage=self.stage,
            item_id=item_id,
        )
        self.history.append({"role": "user", "content": prompt})
        self.history.append({"role": "assistant", "content": result.content})
        self.turns += 1
        usage = result.usage if isinstance(result.usage, dict) else {}
        self.usages.append(usage)

        # Trust the API's own count, but never *only* it. The previous line was
        #     self.tokens = int(usage.get("total_tokens") or 0) or self.tokens
        # which reads as a fallback and behaves as a trap: a turn that comes back with
        # no usage block leaves the counter frozen at its old value, and once it is
        # frozen the ceiling can never be reached again. The conversation then grows
        # without limit — the exact thing the ceiling exists to prevent. Estimating
        # locally is imprecise; silently not counting at all is worse.
        reported = int(usage.get("total_tokens") or 0)
        estimated = self._estimated_tokens()
        self.tokens = max(reported, estimated)
        return result

    def _estimated_tokens(self) -> int:
        """A local floor for the conversation size, for when the API does not say.

        Counted with DeepSeek's tokenizer, so on another vendor it is an approximation —
        fine for deciding "is this conversation getting too long", useless for billing.
        Money is only ever read from the usage the API itself reports.
        """
        return count_tokens(SYSTEM_PROMPT) + sum(
            count_tokens(str(message.get("content") or "")) for message in self.history
        )


def require_parsed(
    parsed: object,
    chat_result: ChatResult,
    cap: int | None,
    kind: str,
    item_id: str,
    *,
    provider: str = "",
    model: str = "",
) -> dict:
    """Reject a reply we could not parse instead of storing the raw text.

    Storing it looked harmless — the field just became ``raw_score`` — but every
    downstream reader then saw a missing novelty/difficulty score and ranked the
    question as a zero. A truncated reply must stop the run, not quietly change
    which questions the teacher gets.
    """
    if isinstance(parsed, dict):
        return parsed

    usage = chat_result.usage if isinstance(chat_result.usage, dict) else {}
    produced = int(usage.get("completion_tokens") or 0)
    if cap and produced >= cap:
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        share = f"{100 * reasoning / produced:.0f}%" if produced else "?"

        # Name the lever that exists on *this* model. A measured run had vocab spending
        # 94% of its output on reasoning, and when that is what happened the cure is a
        # shallower effort, not a bigger budget — a bigger budget just buys a longer
        # chain of thought (决策 31). But GLM has no strength dial at all, and telling its
        # user to "lower the effort" would send them looking for a control that is not
        # there; on those models the only lever is thinking itself.
        spec = pv.model_spec(provider or pv.DEFAULT_PROVIDER, model)
        if spec.efforts:
            lever = f"  思考占了大半 → 把强度降一档最有效：--{kind}-reasoning-effort {spec.efforts[0]}"
        else:
            lever = f"  这个模型没有强度档，只能关掉思考：--{kind}-thinking disabled"

        headroom = ""
        if cap >= spec.max_output:
            headroom = (
                f"\n  注意：{spec.id} 的输出上限就是 {spec.max_output}，已经顶到头了，"
                f"调高 --{kind}-max-tokens 不会有任何作用。"
            )

        raise RuntimeError(
            f"{kind} 输出被 max_tokens={cap} 截断，JSON 不完整：{item_id}\n"
            f"  这次调用产生了 {produced} tokens，其中 {reasoning}（{share}）是思考。\n"
            f"{lever}\n"
            f"  确实是答案太长 → 才该调高 --{kind}-max-tokens。{headroom}"
        )
    raise RuntimeError(f"{kind} 返回的不是合法 JSON：{item_id}（前 200 字：{str(parsed)[:200]}）")


def cache_hit_ratio(usages: list[dict]) -> tuple[int, int]:
    """Return (cached prompt tokens, total prompt tokens) across calls."""
    cached = sum(int(u.get("prompt_cache_hit_tokens") or 0) for u in usages if isinstance(u, dict))
    total = sum(int(u.get("prompt_tokens") or 0) for u in usages if isinstance(u, dict))
    return cached, total


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
    answer_docx: Path | None = None,
) -> list[dict]:
    # Local segmentation is fast (well under a second per paper), so checking between
    # papers is effectively instant — 取消 needs no confirmation on this stage.
    raise_if_cancelled()
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

    # A student edition whose answers came as their own file. Its text is written out
    # under its own name too, so --mode repair-answers can find it later.
    answer_doc = None
    answer_text = ""
    if answer_docx is not None:
        answer_doc = read_docx(answer_docx)
        answer_text = answer_doc.text
        (extracted_dir / f"{safe_stem(answer_docx.name)}.txt").write_text(answer_text, encoding="utf-8")

    if args.segment_input == "local":
        segments = local_segment_paper(
            source_doc, text, doc, extra_starts,
            answer_doc=answer_doc, answer_text=answer_text,
            answer_path=str(answer_docx) if answer_docx is not None else "",
        )
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
            stage="segment",
            item_id=unit["unit_id"],
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


def confirm_pairing(args: argparse.Namespace, paper: str, paper_head: str,
                    answers: str, answers_head: str) -> dict:
    """Ask the fast model whether this answers document really belongs to this paper.

    Filenames are the cheap signal and they are often enough, but they are also often a
    scan named 扫描件2.pdf. Getting this wrong prints B 卷's answer key under A 卷's
    questions, so the名字 only ever *proposes* — this is what decides.
    """
    provider = pv.get(getattr(args, "provider", pv.DEFAULT_PROVIDER))
    prompt = (
        "下面是两份文档的文件名和开头。第一份是一张英语试卷（题目），"
        "第二份看起来是一份答案/解析文档。请判断：**第二份是不是第一份这张卷子的答案**？\n"
        "看题号范围、题型顺序、专有名词、卷子名称是否对得上。拿不准就答 false——"
        "配错答案比没有答案严重得多。\n"
        '只输出 JSON：{"ok": true 或 false, "reason": "一句话"}，JSON 的语法符号用英文半角引号。\n\n'
        f"# 试卷：{paper}\n----\n{paper_head}\n----\n\n"
        f"# 疑似答案：{answers}\n----\n{answers_head}\n----"
    )
    result = call_stage_model(
        args, prompt, model=provider.role_model(pv.FLASH),
        reasoning_effort=getattr(args, "segment_reasoning_effort", "high"),
        thinking="disabled", max_tokens=300, stage="pairing", item_id=f"{paper} ← {answers}",
    )
    parsed = parse_model_json(result.content)
    if isinstance(parsed, dict):
        return parsed
    return {"ok": None, "reason": (result.content or "").strip()[:200]}


def pair_answer_docs(args: argparse.Namespace, docx_files: list[Path],
                     texts: dict[Path, str]) -> tuple[dict[Path, Path], list[Path]]:
    """Work out which files are papers, which are answers, and what goes with what.

    Returns ``(pairing, answer_only)``: the papers to segment keep their own entry in
    ``pairing`` (mapping to the answers document, when one was matched), and every file
    in ``answer_only`` is dropped from segmentation — it is not a paper, and segmenting
    it produces zero questions, a FAIL, and a dead run.
    """
    kinds: dict[Path, str] = {}
    for path in docx_files:
        text = texts.get(path, "")
        try:
            found = len(local_segment_paper(path.name, text))
        except Exception:
            found = 0
        kinds[path] = answer_pairing.classify(
            path.name, found, _find_answer_section_start(text), len(text),
            first_answer_pos=first_answer_run(text),
        )

    papers = [p for p in docx_files if kinds[p] in (answer_pairing.PAPER, answer_pairing.BOTH, answer_pairing.UNKNOWN)]
    answers = [p for p in docx_files if kinds[p] == answer_pairing.ANSWERS]
    if not answers:
        return {}, []

    # Only a paper that has no answers of its own needs a pairing; a self-contained one
    # already has them, and overwriting that with a guess would be a downgrade.
    needy = [p for p in papers if kinds[p] != answer_pairing.BOTH]
    log(args, f"  输入里有 {len(answers)} 份看起来是「答案文档」，{len(needy)} 张卷子没有自带答案，尝试配对…")

    by_name = {p.name: p for p in docx_files}
    proposals = answer_pairing.propose_pairs([p.name for p in needy], [a.name for a in answers])
    pairing: dict[Path, Path] = {}
    paired_answers: set[Path] = set()
    for paper_name, answer_name, score in proposals:
        paper, answer = by_name[paper_name], by_name[answer_name]
        verdict = {"ok": True, "reason": f"文件名高度一致（{score:.2f}）"}
        if getattr(args, "pairing_confirm", True):
            try:
                verdict = confirm_pairing(
                    args, paper_name, texts.get(paper, "")[:1500],
                    answer_name, texts.get(answer, "")[:1500],
                )
            except Exception as exc:  # noqa: BLE001
                verdict = {"ok": None, "reason": f"复核调用失败：{exc}"}
        if verdict.get("ok") is True:
            pairing[paper] = answer
            paired_answers.add(answer)
            log(args, f"    ✅ 配对：{paper_name} ← {answer_name}（{verdict.get('reason') or ''}）")
        else:
            # Refusing to pair is the safe outcome: the paper simply has no answers, which
            # the rest of the pipeline already knows how to say.
            log(args, f"    ⚠️ 不配对：{paper_name} ← {answer_name}（{verdict.get('reason') or '模型未确认'}）")

    for answer in answers:
        if answer not in paired_answers:
            log(args, f"    ⚠️ 「{answer.name}」没能配到任何一张卷子，已跳过（不会被当成试卷）。")
    for paper in needy:
        if paper not in pairing:
            log(args, f"    ⚠️ 「{paper.name}」没有配到答案，将按「原卷未提供答案」处理。")

    return pairing, answers


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

    # One read of each file's text, shared by the smoke check and the answer-document
    # pairing below: both need it, and reading a docx twice is not free.
    texts: dict[Path, str] = {}
    for docx in docx_files:
        try:
            texts[docx] = extract_docx_text(docx)
        except Exception:
            # A file we cannot even read as text here will fail loudly inside
            # segmentation itself; these checks must not add a second failure mode.
            continue

    # Free local smoke check on the extracted text, so an obviously broken input (OCR
    # mojibake, near-empty scan) is flagged even on a straight stage1 run that never
    # touched --mode preflight. Warn-only; no model call here, escalation lives in preflight.
    input_suspects = [
        f"{docx.name}：{'；'.join(input_precheck.precheck_text(text)['reasons'])}"
        for docx, text in texts.items()
        if input_precheck.precheck_text(text)["suspect"]
    ]
    if input_suspects:
        log(args, f"  ⚠️ 输入预检发现 {len(input_suspects)} 份卷子可疑（仅警告，继续切分；建议先跑 --mode preflight 复核）：")
        for line in input_suspects:
            log(args, f"    - {line}")

    # Some papers arrive as a student edition plus a separate 答案 document. Match them up
    # before segmenting: an answers document is not a paper, and putting one through the
    # segmenter yields zero questions, a structural FAIL, and a dead run.
    pairing: dict[Path, Path] = {}
    answer_only: list[Path] = []
    if getattr(args, "answer_pairing", True) and args.segment_input == "local":
        pairing, answer_only = pair_answer_docs(args, docx_files, texts)
        if pairing:
            write_json(
                out_dir / "answer_pairing.json",
                {paper.name: answer.name for paper, answer in pairing.items()},
            )
    docx_files = [d for d in docx_files if d not in answer_only]
    if not docx_files:
        raise SystemExit("输入里只有答案文档，没有任何试卷。请把试卷也放进来。")

    if args.segment_input == "local":
        log(args, f"Segmenting {len(docx_files)} docx file(s) locally; no segment API calls will be made.")
    else:
        log(args, f"Segmenting {len(docx_files)} docx file(s) with {args.segment_model}; workers={args.segment_workers}.")
    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.segment_workers)) as executor:
        future_map = {
            executor.submit(segment_docx_file, docx, args, out_dir, None, pairing.get(docx)): docx
            for docx in docx_files
        }
        for future in concurrent.futures.as_completed(future_map):
            docx = future_map[future]
            try:
                doc_rows = future.result()
                rows.extend(doc_rows)
                log(args, f"  segmented {docx.name}: {len(doc_rows)} item(s).")
            except Cancelled:
                raise
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
                        stage="segment",
                        item_id=doc_name,
                    ).content

                extra = locate_missing_sections(
                    parsed, segments, missing, ask, log=lambda m: log(args, m)
                )
                if not extra:
                    return []
                # Keep the paired answers document across the re-cut: dropping it here
                # would hand back a repaired paper with its answers silently gone.
                return segment_docx_file(
                    path, args, out_dir, extra_starts=extra, answer_docx=pairing.get(path)
                )

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
    raise_if_cancelled()
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
        stage="score",
        item_id=item_id,
    )
    save_api_conversation(out_dir, "score", item_id, prompt, chat_result, args)
    parsed = parse_model_json(chat_result.content)
    score = require_parsed(parsed, chat_result, args.score_max_tokens, "score", item_id)
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
        # Without this the cost report has to recover the model by grepping the
        # saved conversation logs — and with 保留中间产物 off there are none, so it
        # assumed pro and billed a flash run at roughly 3x its real price.
        "model": stage_model_name(args, "score"),
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

    # Scoring shares a long, identical instruction prefix across every call, but
    # firing all 16 workers at once means all 16 miss the cache — there is nothing
    # to hit yet. Sending the first call alone writes that prefix into DeepSeek's
    # cache, and the rest then read it.
    if rows:
        results.append(score_one_segment(rows[0], args, out_dir))
        log(args, f"  scored {rows[0]['item_id']} (1/{len(rows)})  [预热缓存]")
        rows = rows[1:]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.score_workers)) as executor:
        future_map = {executor.submit(score_one_segment, row, args, out_dir): row for row in rows}
        for future in concurrent.futures.as_completed(future_map):
            row = future_map[future]
            try:
                result = future.result()
                results.append(result)
                log(args, f"  scored {row['item_id']} ({len(results)}/{len(rows) + 1})")
            except Cancelled:
                raise
            except Exception as exc:
                log(args, f"  score failed for {row['item_id']}: {exc}")
                raise

    cached, total = cache_hit_ratio([r.get("usage") for r in results])
    if total:
        log(args, f"  prompt cache: {cached}/{total} tokens hit ({100 * cached / total:.0f}%)")

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


def drop_incomplete_reading(candidates: list[dict], section: str, args: argparse.Namespace) -> list[dict]:
    """Filter out reading candidates whose source paper is missing questions.

    Local scoring is deterministic, so a paper that dropped 阅读B's questions (see
    决策 35) would be picked every single run — the teacher presses 重新选题 and gets the
    same broken passage back. Removing it here lets a complete paper take the slot.
    Never empties a section, though: if *every* candidate is defective, keep them and
    leave the shout to the export gate rather than silently produce nothing.
    """
    if section not in READING_SECTIONS:
        return candidates
    kept: list[dict] = []
    for row in candidates:
        seg_path = row.get("segment_path")
        segment: object = {}
        if seg_path:
            try:
                segment = read_json(Path(seg_path))
            except Exception:
                segment = {}
        missing = missing_question_numbers(segment) if isinstance(segment, dict) else []
        if missing:
            log(args, f"  跳过 {section_display(section)}·{row.get('source_doc', '')}：原卷缺第 {', '.join(missing)} 题，不参与选题。")
        else:
            kept.append(row)
    if not kept:
        log(args, f"  警告：{section_display(section)} 的候选全部缺题，暂不过滤（导出闸门会拦下）。")
        return candidates
    return kept


def run_select(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out)
    score_path = out_dir / "score_index.jsonl"
    if not score_path.exists():
        raise SystemExit(f"Missing {score_path}. Run --mode score first.")
    score_rows = read_jsonl(score_path)
    selected: list[dict] = []
    for section, target_count in SELECTION_TARGETS.items():
        candidates = [row for row in score_rows if row.get("section") == section]
        candidates = drop_incomplete_reading(candidates, section, args)
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

    # --reselect means "the teacher looked at this batch and wants different questions".
    # The picks the model is being asked to move away from are the ones currently on
    # disk — without naming them, the same scores and the same model just choose the
    # same questions again and the 重新选题 button appears to do nothing.
    previous: dict[str, list[str]] = {}
    if getattr(args, "reselect", False):
        current = read_json(out_dir / "selected_items.json")
        if isinstance(current, list):
            for row in current:
                if isinstance(row, dict):
                    previous.setdefault(str(row.get("section", "")), []).append(str(row.get("item_id", "")))
        if previous:
            log(args, f"  重新选题：避开上一轮选中的 {sum(len(v) for v in previous.values())} 道题。")
            # With 3 papers there are only 3 candidates per section and 2 are wanted,
            # so at most one question per section *can* change. Say so, rather than
            # letting the teacher press the button again and again.
            per_section = len(score_rows) / max(1, len(SELECTION_TARGETS))
            if per_section <= max(SELECTION_TARGETS.values()) + 1:
                log(args, "  提示：候选题不多，每个题型最多只能换掉一道。想换得更彻底，请多放几份试卷。")

    for section, target_count in SELECTION_TARGETS.items():
        candidates = [row for row in score_rows if row.get("section") == section]
        candidates = drop_incomplete_reading(candidates, section, args)
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
            prompt = build_review_select_prompt(
                [candidate_summary(row) for row in shortlist],
                target_count,
                section,
                rejected=previous.get(section),
            )
            chat_result = call_stage_model(
                args,
                prompt,
                model=args.review_model,
                reasoning_effort=args.review_reasoning_effort,
                thinking=args.review_thinking,
                max_tokens=args.review_max_tokens,
                stage="review_select",
                item_id=section,
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


def enrich_one_selected(
    row: dict,
    args: argparse.Namespace,
    out_dir: Path,
    conversation: "Conversation | None" = None,
) -> dict:
    raise_if_cancelled()
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
    if conversation is not None:
        chat_result = conversation.ask(prompt, max_tokens=args.enrich_max_tokens, item_id=item_id)
    else:
        chat_result = call_stage_model(
            args,
            prompt,
            model=args.enrich_model,
            reasoning_effort=args.enrich_reasoning_effort,
            thinking=args.enrich_thinking,
            max_tokens=args.enrich_max_tokens,
            stage="enrich",
            item_id=item_id,
        )
    save_api_conversation(out_dir, "enrich", item_id, prompt, chat_result, args)
    parsed = parse_model_json(chat_result.content)
    enrichment = require_parsed(parsed, chat_result, args.enrich_max_tokens, "enrich", item_id)
    enrichment.setdefault("item_id", item_id)
    result = {
        "item_id": item_id,
        "source_doc": row.get("source_doc", ""),
        "section": row.get("section", ""),
        "display_section": row.get("display_section", ""),
        "item_label": row.get("item_label", ""),
        "enrichment": enrichment,
        "usage": chat_result.usage,
        "model": stage_model_name(args, "enrich"),
        "client_used": chat_result.client_used,
        "prompt_version": ENRICH_PROMPT_VERSION,
    }
    write_json(enrich_path, result)
    row["enrichment"] = enrichment
    row["enrichment_path"] = str(enrich_path)
    return row


def paper_text_path(out_dir: Path, source_doc: str) -> Path:
    """Where the segment stage already dumped this paper's plain text."""
    return out_dir / "extracted_text" / f"{safe_stem(source_doc)}.txt"


def vocab_row_mode(row: dict) -> str:
    """Which mode produced this word-list row.

    Prefer what the row says. A row written before the switch existed has no ``vocab_mode``
    at all, so fall back to its *shape*, which cannot lie: 完整 is keyed by question and
    carries an ``item_id``; 困难 is keyed by paper and does not. Defaulting to one or the
    other instead would silently misread half the word lists already on disk.
    """
    mode = str(row.get("vocab_mode") or "")
    if mode in VOCAB_MODES:
        return mode
    return VOCAB_CHUNKED if row.get("item_id") else VOCAB_WHOLE


def vocab_dir_for(out_dir: Path, mode: str) -> Path:
    """Cached word lists, one directory per mode.

    Kept apart on purpose. The two modes key their cache differently (by question vs by
    paper) and produce differently-shaped rows, so sharing a directory would let a
    half-finished run of one mode be silently reused by the other.
    """
    return out_dir / "vocab" / mode


def vocab_one_selected(
    row: dict,
    args: argparse.Namespace,
    out_dir: Path,
    conversation: "Conversation | None" = None,
) -> dict:
    """完整（分块）: one selected question's word list.

    The answer key never reaches the model here, because ``segment_body`` does not carry
    it — a property the whole-paper path has to work for (see vocab_one_paper).
    """
    raise_if_cancelled()
    vocab_dir = vocab_dir_for(out_dir, VOCAB_CHUNKED)
    ensure_dir(vocab_dir)
    item_id = row["item_id"]
    vocab_path = vocab_dir / f"{safe_filename(item_id)}.json"
    if vocab_path.exists() and not args.force:
        existing = read_json(vocab_path)
        if isinstance(existing, dict):
            return existing

    segment = read_json(Path(row["segment_path"]))
    if not isinstance(segment, dict):
        raise RuntimeError(f"Invalid segment file for {item_id}")

    model = stage_model_name(args, "vocab")
    prompt = build_vocab_item_prompt(segment)
    # One repair turn, as the explanations get: a word list is dozens of entries of free
    # Chinese text, and one unescaped quote anywhere in it makes the whole reply
    # unparseable. It killed this stage 16 items into a real run (决策 27).
    parsed, chat_result, turn_usages = ask_for_json(
        conversation,
        args,
        prompt,
        model=model,
        reasoning_effort=args.vocab_reasoning_effort,
        thinking=args.vocab_thinking,
        max_tokens=args.vocab_max_tokens,
        stage="vocab",
        item_id=item_id,
    )
    save_api_conversation(out_dir, "vocab", item_id, prompt, chat_result, args)
    vocab = require_parsed(
        parsed, chat_result, args.vocab_max_tokens, "vocab", item_id,
        provider=args.provider, model=model,
    )
    result = {
        "vocab_mode": VOCAB_CHUNKED,
        "item_id": item_id,
        "source_doc": row.get("source_doc", ""),
        "section": row.get("section", ""),
        "display_section": row.get("display_section", ""),
        "item_label": row.get("item_label", ""),
        "reading_words": vocab.get("reading_words", []),
        "word_forms": vocab.get("word_forms", []),
        # Both turns, when a repair was needed: the retry alone would under-report what
        # the item actually cost.
        "usage": merge_usages(turn_usages),
        "model": model,
        "client_used": chat_result.client_used,
        "prompt_version": VOCAB_PROMPT_VERSION,
    }
    write_json(vocab_path, result)
    return result


def vocab_one_paper(
    source_doc: str,
    args: argparse.Namespace,
    out_dir: Path,
) -> dict:
    """困难（整卷）: one paper's word list, read from the paper's full text.

    Keyed by paper, not by question: the words belong to the paper, so re-picking the
    questions no longer makes the handout stale. That also means changing the selection
    does not force this to run again — see assert_selection_is_complete.
    """
    raise_if_cancelled()
    vocab_dir = vocab_dir_for(out_dir, VOCAB_WHOLE)
    ensure_dir(vocab_dir)
    stem = safe_stem(source_doc)
    vocab_path = vocab_dir / f"{stem}.json"
    if vocab_path.exists() and not args.force:
        existing = read_json(vocab_path)
        if isinstance(existing, dict):
            return existing

    text_path = paper_text_path(out_dir, source_doc)
    if not text_path.exists():
        raise SystemExit(
            f"Missing {text_path}. 「困难（整卷）」模式读整卷正文，请先跑 --mode segment。"
        )
    raw = text_path.read_text(encoding="utf-8")

    # The extracted text is the *whole* docx, answer section and all. This handout goes
    # to students, so the answer area has to come off before the model ever sees it —
    # the old per-question prompt got this for free by sending only segment_body.
    # Two distinct reasons, and the second is the one that would have been missed:
    #   * 答案绝不能进学生版 (CLAUDE.md). Only words reach the docx, but nothing that
    #     reveals an answer should be able to influence a student-facing artefact.
    #   * the answer area contains 参考范文 — model essays the students' paper does not
    #     have. Words mined from those are words from a text the student cannot read.
    # trim_answer_tail_from_text is 决策 6's boundary: the minimum of every candidate,
    # not the first one that matches.
    text = trim_answer_tail_from_text(raw).strip()
    if not text:
        raise RuntimeError(f"{source_doc}: 抽出的正文是空的，无法提词。")
    if len(text) < len(raw.strip()):
        log(args, f"  vocab {source_doc}: 已去掉答案区（{len(raw.strip()) - len(text):,} 字）")

    model = stage_model_name(args, "vocab")
    conversation = Conversation(
        args,
        model=model,
        reasoning_effort=args.vocab_reasoning_effort,
        thinking=args.vocab_thinking,
        stage="vocab",
    )

    chunks = chunk_by_tokens(text, conversation.budget)
    log(args, f"  vocab {source_doc}: {count_tokens(text):,} tokens → {len(chunks)} 轮")

    parts: list[dict] = []
    usages: list[dict] = []
    last_result: ChatResult | None = None

    for index, chunk in enumerate(chunks, start=1):
        prompt = build_vocab_paper_prompt(source_doc, chunk, part=index, total=len(chunks))
        # One repair turn, as the explanations get: a word list is dozens of entries of
        # free Chinese text, and one unescaped quote anywhere in it makes the whole reply
        # unparseable. It killed this stage 16 items into a real run (决策 27).
        parsed, chat_result, turn_usages = ask_for_json(
            conversation,
            args,
            prompt,
            model=model,
            reasoning_effort=args.vocab_reasoning_effort,
            thinking=args.vocab_thinking,
            max_tokens=args.vocab_max_tokens,
            stage="vocab",
            item_id=stem,
        )
        suffix = "" if len(chunks) == 1 else f"__part{index}"
        save_api_conversation(out_dir, "vocab", f"{stem}{suffix}", prompt, chat_result, args)
        parts.append(require_parsed(
            parsed, chat_result, args.vocab_max_tokens, "vocab", source_doc,
            provider=args.provider, model=model,
        ))
        usages.extend(turn_usages)
        last_result = chat_result

    if len(chunks) > 1:
        # Merge inside the same conversation: every part is already in the history, so
        # the prefix is a cache hit and this turn is nearly free. Local dedup happens at
        # export anyway — what this buys is ranking across the whole paper, which only a
        # model that has read all of it can do.
        prompt = build_vocab_merge_prompt(source_doc, len(chunks))
        parsed, chat_result, turn_usages = ask_for_json(
            conversation,
            args,
            prompt,
            model=model,
            reasoning_effort=args.vocab_reasoning_effort,
            thinking=args.vocab_thinking,
            max_tokens=args.vocab_max_tokens,
            stage="vocab",
            item_id=f"{stem}__merge",
        )
        save_api_conversation(out_dir, "vocab", f"{stem}__merge", prompt, chat_result, args)
        merged = require_parsed(
            parsed, chat_result, args.vocab_max_tokens, "vocab", source_doc,
            provider=args.provider, model=model,
        )
        usages.extend(turn_usages)
        last_result = chat_result
        vocab = merged
    else:
        vocab = parts[0]

    result = {
        "vocab_mode": VOCAB_WHOLE,
        "source_doc": source_doc,
        "reading_words": vocab.get("reading_words", []),
        "word_forms": vocab.get("word_forms", []),
        "parts": len(chunks),
        # Every turn, not just the last: the merge turn alone would under-report what the
        # paper actually cost.
        "usage": merge_usages(usages),
        "model": model,
        "client_used": last_result.client_used if last_result else "",
        "prompt_version": VOCAB_PROMPT_VERSION,
        "cache": cache_hit_ratio(conversation.usages),
    }
    write_json(vocab_path, result)
    return result


def vocab_papers(out_dir: Path) -> list[str]:
    """Which papers need a word list: the ones that contributed a selected question.

    Read from the selection rather than from the input folder, so a paper nobody used
    is not paid for. But the *unit* is the paper, not the question.
    """
    selection_path = out_dir / "selected_items.json"
    if not selection_path.exists():
        raise SystemExit(f"Missing {selection_path}. Run --mode select first.")
    selected = read_json(selection_path)
    if not isinstance(selected, list):
        raise SystemExit(f"Invalid {selection_path}")
    papers = {str(row.get("source_doc", "")) for row in selected if row.get("source_doc")}
    return sorted(papers)


def warn_if_output_ceiling_is_too_low(args: argparse.Namespace, model: str) -> None:
    """Say it before spending four minutes discovering it.

    A measured run of the whole-paper mode at the deep setting produced up to 38,934
    output tokens for one paper — 94% of it reasoning. A model whose entire output ceiling
    sits below that cannot finish the job at this setting, and the failure would otherwise
    arrive as a truncation error long after the teacher walked away.

    Only for the whole-paper mode: 完整（分块）feeds one question at a time, so a single
    reply is a fraction of that size and this warning would be crying wolf.
    """
    if args.vocab_mode != VOCAB_WHOLE:
        return
    spec = pv.model_spec(args.provider, model)
    if not pv.is_deepest(args.vocab_reasoning_effort, args.provider, model):
        return
    if spec.max_output >= VOCAB_DEEP_OBSERVED_PEAK:
        return

    lever = (
        f"--vocab-reasoning-effort {spec.efforts[0]}" if spec.efforts
        else "--vocab-thinking disabled"
    )
    log(
        args,
        f"  ⚠️ {spec.id} 的输出上限是 {spec.max_output:,}，而「困难（整卷）」深度思考实测最多要 "
        f"{VOCAB_DEEP_OBSERVED_PEAK:,}。可能会被截断。\n"
        f"     若报「被 max_tokens 截断」，请改用：{lever}，或换成「完整（分块）」模式。",
    )


def _run_vocab_chunked(args: argparse.Namespace, out_dir: Path, model: str) -> list[dict]:
    """完整（分块）: one call per selected question, papers in parallel.

    One conversation per paper, questions asked into it in turn, so each turn's prefix
    (system + every previous question of that paper) is a cache hit — 决策 8.
    """
    selection_path = out_dir / "selected_items.json"
    if not selection_path.exists():
        raise SystemExit(f"Missing {selection_path}. Run --mode select first.")
    selected = read_json(selection_path)
    if not isinstance(selected, list):
        raise SystemExit(f"Invalid {selection_path}")

    by_paper: dict[str, list[dict]] = {}
    for row in selected:
        by_paper.setdefault(str(row.get("source_doc", "")), []).append(row)

    log(args, f"「完整（分块）」：逐题提词，{len(selected)} 道题 / {len(by_paper)} 份卷，模型 {model}。")

    results: list[dict] = []
    conversations: list[Conversation] = []
    done = 0
    lock = threading.Lock()

    def vocab_paper(rows: list[dict]) -> list[dict]:
        nonlocal done
        conversation = Conversation(
            args,
            model=model,
            reasoning_effort=args.vocab_reasoning_effort,
            thinking=args.vocab_thinking,
            stage="vocab",
        )
        with lock:
            conversations.append(conversation)
        out: list[dict] = []
        for row in rows:
            out.append(vocab_one_selected(row, args, out_dir, conversation=conversation))
            with lock:
                done += 1
                log(args, f"  vocab {row['item_id']} ({done}/{len(selected)})")
        return out

    workers = max(1, min(args.enrich_workers, len(by_paper)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(vocab_paper, rows): doc for doc, rows in by_paper.items()}
        for future in concurrent.futures.as_completed(future_map):
            doc = future_map[future]
            try:
                results.extend(future.result())
            except Cancelled:
                raise
            except Exception as exc:
                log(args, f"  vocab failed for {doc}: {exc}")
                raise

    cached, total = cache_hit_ratio([u for c in conversations for u in c.usages])
    if total:
        log(args, f"  prompt cache: {cached}/{total} tokens hit ({100 * cached / total:.0f}%)")

    results.sort(key=lambda r: (section_order(r.get("section", "")), r.get("source_doc", ""), r.get("item_id", "")))
    return results


def _run_vocab_whole(args: argparse.Namespace, out_dir: Path, model: str) -> list[dict]:
    """困难（整卷）: one conversation per paper, reading the paper end to end."""
    papers = vocab_papers(out_dir)
    log(args, f"「困难（整卷）」：通读整卷提词，{len(papers)} 份卷，模型 {model}。")

    results: list[dict] = []
    done = 0
    lock = threading.Lock()

    def one(source_doc: str) -> dict:
        nonlocal done
        result = vocab_one_paper(source_doc, args, out_dir)
        with lock:
            done += 1
            log(args, f"  vocab {source_doc} ({done}/{len(papers)})")
        return result

    # Papers in parallel, each in its own conversation. Not one conversation for
    # everything: a single 40%-of-context conversation would be slow and would blur one
    # paper's vocabulary into the next.
    workers = max(1, min(args.enrich_workers, len(papers)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(one, doc): doc for doc in papers}
        for future in concurrent.futures.as_completed(future_map):
            doc = future_map[future]
            try:
                results.append(future.result())
            except Cancelled:
                raise
            except Exception as exc:
                log(args, f"  vocab failed for {doc}: {exc}")
                raise

    cached = sum(int(r.get("cache", (0, 0))[0]) for r in results)
    total = sum(int(r.get("cache", (0, 0))[1]) for r in results)
    if total:
        log(args, f"  prompt cache: {cached}/{total} tokens hit ({100 * cached / total:.0f}%)")

    results.sort(key=lambda r: r.get("source_doc", ""))
    return results


def run_vocab(args: argparse.Namespace) -> list[dict]:
    """The student handout, built whichever way the teacher asked for.

    Not a right way and a wrong way — a teaching choice, so 基础模式 puts the switch in
    front of her rather than this file deciding for her:

    * 完整（分块）— the words come only from the questions she is handing out.
    * 困难（整卷）— the model reads the whole paper and picks the genuinely hard ones.
    """
    out_dir = Path(args.out)
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key. Set {args.api_key_env} or pass --api-key.")

    model = stage_model_name(args, "vocab")
    warn_if_output_ceiling_is_too_low(args, model)

    if args.vocab_mode == VOCAB_CHUNKED:
        results = _run_vocab_chunked(args, out_dir, model)
    else:
        results = _run_vocab_whole(args, out_dir, model)

    vocab_index = out_dir / "vocab_index.json"
    write_json(vocab_index, results)
    log(args, f"Vocabulary outputs written: {vocab_index} ({file_size_label(vocab_index)})")
    return results


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

    # One conversation per paper: the questions of a paper are asked in sequence
    # inside it, so every turn after the first re-reads a cached prefix instead of
    # paying full price for the instruction block. Papers stay parallel, so the
    # wall-clock cost is one paper's worth of turns, not the whole run's.
    by_paper: dict[str, list[dict]] = {}
    for row in selected:
        by_paper.setdefault(str(row.get("source_doc", "")), []).append(row)

    log(
        args,
        f"Enriching {len(selected)} selected item(s) with {args.enrich_model}; "
        f"{len(by_paper)} conversation(s), one per paper.",
    )
    results: list[dict] = []
    conversations: list[Conversation] = []
    done = 0
    lock = threading.Lock()

    def enrich_paper(rows: list[dict]) -> list[dict]:
        nonlocal done
        conversation = Conversation(
            args,
            model=args.enrich_model,
            reasoning_effort=args.enrich_reasoning_effort,
            thinking=args.enrich_thinking,
            stage="enrich",
        )
        with lock:
            conversations.append(conversation)
        out: list[dict] = []
        for row in rows:
            out.append(enrich_one_selected(row, args, out_dir, conversation=conversation))
            with lock:
                done += 1
                log(args, f"  enriched {row['item_id']} ({done}/{len(selected)})")
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.enrich_workers, len(by_paper)))) as executor:
        future_map = {executor.submit(enrich_paper, rows): doc for doc, rows in by_paper.items()}
        for future in concurrent.futures.as_completed(future_map):
            doc = future_map[future]
            try:
                results.extend(future.result())
            except Cancelled:
                raise
            except Exception as exc:
                log(args, f"  enrich failed for {doc}: {exc}")
                raise

    cached, total = cache_hit_ratio([u for c in conversations for u in c.usages])
    if total:
        log(args, f"  prompt cache: {cached}/{total} tokens hit ({100 * cached / total:.0f}%)")

    results.sort(key=lambda r: (section_order(r.get("section", "")), r.get("source_doc", ""), r.get("item_id", "")))
    enriched_path = out_dir / "selected_items.enriched.json"
    write_json(enriched_path, results)
    write_json(selection_path, results)
    log(args, f"Enrichment outputs written: {enriched_path} ({file_size_label(enriched_path)})")
    return results


WRITING_SECTIONS = {"practical_writing", "continuation_writing"}


@functools.lru_cache(maxsize=8)
def cached_docx(path: str) -> DocxDoc:
    """One parse per paper, not one per question.

    A run selects ~6 questions from each paper and every one of them wants a slice
    of the same answer section, so the uncached version unzipped and parsed the
    whole file 18 times to read 18 spans out of 3 documents.
    """
    return read_docx(Path(path))


def blocks_text(doc: DocxDoc, span: list[int]) -> str:
    """The text of a half-open ``[lo, hi)`` range of body children."""
    if len(span) != 2:
        return ""
    lo, hi = span
    return "\n".join(
        block.text.strip()
        for block in doc.blocks
        if lo <= block.body_index < hi and block.text.strip()
    )


def official_explanation_text(segment: dict) -> str:
    """What the paper itself already says about this item's answer.

    For the numbered sections that is its 【N题详解】 blocks; only the indices are
    stored on the segment (see ``make_local_segment``), so the text is recovered
    from the source file here.

    For the two writing sections the paper writes no 详解 at all — its answer *is*
    the 参考范文, which the segmenter stored as the answer key. Returning "" for
    those left the writing prompt telling the model to "写得比它更好" than a model
    essay it had never been shown, while the teacher edition printed that same
    essay right above our answer.

    Still "" when the paper genuinely explained nothing: 广东's answer section
    skips four whole sections, and there the model writes the explanation alone.
    """
    span = segment.get("official_explanation_blocks") or []
    # Usually the paper itself, but a student edition's answers can live in a separate
    # document; then the indices point into that one. Falling back to source_path keeps
    # every segment written before the field existed working unchanged.
    source = segment.get("official_explanation_path") or segment.get("source_path") or ""
    if len(span) == 2 and source and Path(source).exists():
        text = blocks_text(cached_docx(source), span)
        if text:
            return text

    if segment.get("section") in WRITING_SECTIONS:
        essay = segment.get("answer_key")
        if isinstance(essay, str) and essay.strip() and essay.strip() != NO_ANSWER_MARKER:
            return essay.strip()
    return ""


# Two different ways the model breaks its own JSON, and telling it the wrong one makes
# things worse. Saying "don't use double quotes" — which is right for the *contents* of a
# string — got flash to render the JSON's own syntax in Chinese curly quotes
# (`{“word”: “x”}`), and then the repair turn said it again and it did it again.
JSON_REPAIR_SMART_QUOTES = (
    "你刚才那条输出不是合法 JSON：**JSON 的语法符号被写成了中文引号**。"
    "花括号、方括号、冒号，以及包裹键名和值的那对引号，必须是标准英文半角字符：\n"
    '正确：{"word": "abandon", "meaning": "放弃"}\n'
    '错误：{“word”: “abandon”, “meaning”: “放弃”}\n'
    "只有字符串**内部**引用原文时才用中文引号“”。请重新输出一遍，只输出合法 JSON，"
    "不要用代码块包裹，不要加任何解释。"
)

JSON_REPAIR_TURN = (
    "你刚才那条输出不是合法 JSON（多半是在字符串内部用了没转义的英文双引号）。"
    "请把同样的内容重新输出一遍：JSON 的语法符号仍用标准英文半角引号，"
    "但字符串内部引用原文时改用中文引号“”。"
    "不要用代码块包裹，不要加任何解释。"
)

# How many corrective turns to allow before giving up. One was not enough for the big
# nested 读后续写 JSON: the model rendered its own braces in Chinese quotes, the single
# repair turn said "don't" and it did it again, and the whole paper failed. Each retry
# re-reads a cached prefix, so a couple more attempts is nearly free.
JSON_REPAIR_MAX_TURNS = 2


def _repair_instruction(content: str, echo: bool = False) -> str:
    """Name the actual mistake. A generic scolding just produces the same reply again."""
    head = content.lstrip()[:400]
    base = JSON_REPAIR_SMART_QUOTES if ("“" in head or "”" in head) else JSON_REPAIR_TURN
    if echo and head:
        # By the second try, show the model its own opening so it can see where it broke.
        return f"{base}\n\n你上一条的开头是：\n{head[:200]}\n\n就是这里坏了，请对照上面的规则改对。"
    return base


def ask_for_json(
    conversation: "Conversation | None",
    args: argparse.Namespace,
    prompt: str,
    *,
    model: str,
    reasoning_effort: str,
    thinking: str,
    max_tokens: int,
    stage: str = "",
    item_id: str = "",
) -> tuple[dict | str, ChatResult, list[dict]]:
    """Ask, and give one corrective turn if the reply is not valid JSON.

    A model writing an English model essay reaches for a quotation mark sooner or
    later — `like "accept your feelings" helped me` — and one unescaped `"` makes
    the whole reply unparseable. Repairing the string here would be exactly the
    silent degradation this pipeline refuses to do, and failing the run over a
    punctuation slip wastes the whole paper. So it is handed back to the model,
    which is cheap: inside a conversation the retry re-reads a cached prefix.

    A reply that is still broken after that is a hard error, as before.
    """
    usages: list[dict] = []

    def ask(text: str) -> ChatResult:
        if conversation is not None:
            return conversation.ask(text, max_tokens=max_tokens, item_id=item_id)
        return call_stage_model(
            args, text, model=model, reasoning_effort=reasoning_effort,
            thinking=thinking, max_tokens=max_tokens, stage=stage, item_id=item_id,
        )

    result = ask(prompt)
    usages.append(result.usage if isinstance(result.usage, dict) else {})
    parsed = parse_model_json(result.content)
    if isinstance(parsed, dict):
        return parsed, result, usages

    # A few corrective turns, not one. Each retry re-reads a cached prefix inside the
    # conversation, so it is nearly free, and the big nested writing JSON sometimes needs
    # a second nudge before the model stops rendering its braces in Chinese quotes.
    for attempt in range(JSON_REPAIR_MAX_TURNS):
        # Truncation is a different failure: retrying just truncates again, and
        # require_parsed says so with the token counts. Only offer a repair turn when
        # there was room to answer.
        produced = int((result.usage or {}).get("completion_tokens") or 0)
        if max_tokens and produced >= max_tokens:
            return parsed, result, usages
        result = ask(_repair_instruction(result.content, echo=attempt > 0))
        usages.append(result.usage if isinstance(result.usage, dict) else {})
        parsed = parse_model_json(result.content)
        if isinstance(parsed, dict):
            return parsed, result, usages

    return parsed, result, usages


def dump_failure(
    out_dir: Path,
    kind: str,
    item_id: str,
    prompt: str,
    chat_result: ChatResult,
    error: object,
    *,
    provider: str = "",
    model: str = "",
    preset: str = "",
) -> Path:
    """Keep the whole broken reply, not the 200 chars the error prints.

    A JSON that breaks its own syntax is worth looking at in full, and grepping
    ``api_conversations`` after the fact stops working the moment "keep intermediates"
    is off. So on a hard parse failure we write the entire model output, the prompt we
    sent, and the run's coordinates to ``<out>/failures/`` for a post-mortem.
    """
    failures = out_dir / "failures"
    ensure_dir(failures)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = failures / f"{stamp}_{safe_filename(item_id)}.md"
    usage = chat_result.usage if isinstance(chat_result.usage, dict) else {}
    produced = usage.get("completion_tokens")
    lines = [
        f"# {kind} 解析失败：{item_id}",
        "",
        f"- 时间：{stamp}",
        f"- provider / model：{provider or '?'} / {model or '?'}",
        f"- preset：{preset or '?'}",
        f"- completion_tokens：{produced}",
        f"- 报错：{error}",
        "",
        "## 模型原始输出（完整）",
        "",
        "```",
        str(chat_result.content or ""),
        "```",
        "",
        "## 发出去的 prompt",
        "",
        "```",
        prompt,
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# 读后续写 / 应用文默认给几个思路（老师上课对比着讲「同一题可以怎么写」）。
WRITING_IDEA_COUNT = 3


def _writing_idea_prompt(ordinal: int, prior_angles: list[str]) -> str:
    """The short follow-up that asks for one more idea, inside the same conversation."""
    template = (prompt_dir() / "writing_more_idea.md").read_text(encoding="utf-8")
    if prior_angles:
        prior = "（前面已经给过的角度：" + "；".join(a for a in prior_angles if a) + "）"
    else:
        prior = ""
    return (
        template
        .replace("{{IDEA_ORDINAL}}", str(ordinal))
        .replace("{{PRIOR_ANGLES}}", prior)
    )


def _normalize_idea(piece: object) -> dict | None:
    """One idea turn should return a single {角度,提纲,范文,...} object; accept the
    wrappers a model reaches for (a ``思路`` key, or a one-element list)."""
    if not isinstance(piece, dict):
        return None
    inner = piece.get("思路")
    if isinstance(inner, list) and inner:
        inner = inner[0]
    if isinstance(inner, dict):
        return inner
    if any(key in piece for key in ("范文", "角度", "提纲")):
        return piece
    return None


def explain_writing(
    conversation: "Conversation | None",
    args: argparse.Namespace,
    out_dir: Path,
    segment: dict,
    section: str,
    official: str,
    item_id: str,
) -> tuple[dict, list[dict], list[str]]:
    """Ask for a writing item in small pieces instead of one giant nested JSON.

    读后续写 used to come back as 审题 + 2–3 full essays + 亮点 in a single object — the
    shape most likely to break its own JSON, and one broken essay failed the whole paper.
    Here the brief (审题 + 评分要点) is one small turn and each idea is another, each a
    small JSON re-reading a cached prefix. The merged result is the exact shape
    ``render_explanation`` already expects, so export is untouched.
    """
    usages: list[dict] = []
    clients: list[str] = []

    def one_turn(prompt: str, label: str) -> dict:
        parsed, chat_result, turn_usages = ask_for_json(
            conversation, args, prompt,
            model=args.explain_model,
            reasoning_effort=args.explain_reasoning_effort,
            thinking=args.explain_thinking,
            max_tokens=args.explain_max_tokens,
            stage="explain", item_id=item_id,
        )
        save_api_conversation(out_dir, "explain", label, prompt, chat_result, args)
        usages.extend(turn_usages)
        clients.append(chat_result.client_used)
        try:
            return require_parsed(
                parsed, chat_result, args.explain_max_tokens, "explain", label,
                provider=getattr(args, "provider", ""), model=args.explain_model,
            )
        except RuntimeError as exc:
            dump_failure(
                out_dir, "explain", label, prompt, chat_result, exc,
                provider=getattr(args, "provider", ""), model=args.explain_model,
                preset=getattr(args, "preset", ""),
            )
            raise

    brief = one_turn(build_explain_prompt(segment, section, [], official), item_id)
    merged: dict = {
        "审题": brief.get("审题"),
        "评分要点": brief.get("评分要点", ""),
        "思路": [],
    }
    prior_angles: list[str] = []
    for k in range(1, WRITING_IDEA_COUNT + 1):
        raise_if_cancelled()
        piece = one_turn(_writing_idea_prompt(k, prior_angles), f"{item_id}__idea{k}")
        idea = _normalize_idea(piece)
        if idea is None:
            continue
        merged["思路"].append(idea)
        angle = str(idea.get("角度", "") or "").strip()
        if angle:
            prior_angles.append(angle)
    if not merged["思路"]:
        raise RuntimeError(f"explain 写作题一个思路都没生成：{item_id}")
    return merged, usages, clients


def explain_one_selected(
    row: dict,
    args: argparse.Namespace,
    out_dir: Path,
    conversation: "Conversation | None" = None,
) -> dict:
    raise_if_cancelled()
    explain_dir = out_dir / "explanations"
    ensure_dir(explain_dir)
    item_id = row["item_id"]
    explain_path = explain_dir / f"{safe_filename(item_id)}.json"
    if explain_path.exists() and not args.force:
        existing = read_json(explain_path)
        if isinstance(existing, dict):
            row["explanation"] = existing.get("explanation", existing)
            row["explanation_path"] = str(explain_path)
            row["has_official_explanation"] = bool(existing.get("has_official_explanation"))
            return row

    segment = read_json(Path(row["segment_path"]))
    if not isinstance(segment, dict):
        raise RuntimeError(f"Invalid segment file for {item_id}")
    section = str(row.get("section") or segment.get("section") or "")
    official = official_explanation_text(segment)
    numbers = question_numbers(section, segment.get("answer_key"))

    # Cloze (15 questions) and grammar (10) are asked five at a time, inside the
    # same conversation: the passage and the instructions are then a cached prefix
    # on every turn after the first, and no single reply is long enough to be cut
    # off by max_tokens.
    merged: dict = {}
    usages: list[dict] = []
    clients: list[str] = []
    if section in WRITING_SECTIONS:
        # Writing has no numbered questions; it comes back as a brief plus a few ideas,
        # each its own small turn (see explain_writing).
        merged, usages, clients = explain_writing(
            conversation, args, out_dir, segment, section, official, item_id
        )
    else:
        for chunk in explain_chunks(section, numbers):
            raise_if_cancelled()
            prompt = build_explain_prompt(segment, section, chunk, official)
            parsed, chat_result, turn_usages = ask_for_json(
                conversation,
                args,
                prompt,
                model=args.explain_model,
                reasoning_effort=args.explain_reasoning_effort,
                thinking=args.explain_thinking,
                max_tokens=args.explain_max_tokens,
                stage="explain",
                item_id=item_id,
            )
            label = item_id if len(numbers) == len(chunk) else f"{item_id}__q{chunk[0]}"
            save_api_conversation(out_dir, "explain", label, prompt, chat_result, args)
            try:
                piece = require_parsed(
                    parsed, chat_result, args.explain_max_tokens, "explain", label,
                    provider=getattr(args, "provider", ""), model=args.explain_model,
                )
            except RuntimeError as exc:
                dump_failure(
                    out_dir, "explain", label, prompt, chat_result, exc,
                    provider=getattr(args, "provider", ""), model=args.explain_model,
                    preset=getattr(args, "preset", ""),
                )
                raise
            usages.extend(turn_usages)
            clients.append(chat_result.client_used)

            # Question-by-question sections come back as {"questions": [...]}; the two
            # writing sections have their own shape and are handled above.
            if "questions" in piece:
                merged.setdefault("questions", []).extend(piece.get("questions") or [])
            else:
                merged.update(piece)

    if numbers:
        got = {str(q.get("number")) for q in merged.get("questions", []) if isinstance(q, dict)}
        missing = [n for n in numbers if str(n) not in got]
        if missing:
            # Silently shipping a page with holes in it is how a teacher discovers
            # mid-lesson that question 47 has no explanation.
            raise RuntimeError(f"explain 漏讲了题目 {missing}：{item_id}。请重试或调高 --explain-max-tokens。")

    merged.setdefault("item_id", item_id)
    result = {
        "item_id": item_id,
        "source_doc": row.get("source_doc", ""),
        "section": section,
        "display_section": row.get("display_section", ""),
        "item_label": row.get("item_label", ""),
        "has_official_explanation": bool(official),
        "explanation": merged,
        "usage": merge_usages(usages),
        "model": stage_model_name(args, "explain"),
        "client_used": clients[0] if clients else "",
        "prompt_version": EXPLAIN_PROMPT_VERSION,
    }
    write_json(explain_path, result)
    row["explanation"] = merged
    row["explanation_path"] = str(explain_path)
    row["has_official_explanation"] = bool(official)
    return row


def merge_usages(usages: list[dict]) -> dict:
    """Add up the turns an item took, so the cost report still sees one call's worth."""
    total: dict = {}
    for usage in usages:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            total[key] = total.get(key, 0) + int(usage.get(key) or 0)
    return total


def run_explain(args: argparse.Namespace) -> list[dict]:
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

    # One conversation per paper, exactly as enrich does it: the turns of a paper
    # run in sequence so each re-reads a cached prefix, and papers run in parallel
    # so the wall clock is one paper's worth of turns, not the whole run's.
    by_paper: dict[str, list[dict]] = {}
    for row in selected:
        by_paper.setdefault(str(row.get("source_doc", "")), []).append(row)

    log(
        args,
        f"Explaining {len(selected)} selected item(s) with {args.explain_model}; "
        f"{len(by_paper)} conversation(s), one per paper.",
    )
    results: list[dict] = []
    conversations: list[Conversation] = []
    done = 0
    lock = threading.Lock()

    def explain_paper(rows: list[dict]) -> list[dict]:
        nonlocal done
        conversation = Conversation(
            args,
            model=args.explain_model,
            reasoning_effort=args.explain_reasoning_effort,
            thinking=args.explain_thinking,
            stage="explain",
        )
        with lock:
            conversations.append(conversation)
        out: list[dict] = []
        for row in rows:
            out.append(explain_one_selected(row, args, out_dir, conversation=conversation))
            with lock:
                done += 1
                log(args, f"  explained {row['item_id']} ({done}/{len(selected)})")
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.explain_workers, len(by_paper)))) as executor:
        future_map = {executor.submit(explain_paper, rows): doc for doc, rows in by_paper.items()}
        for future in concurrent.futures.as_completed(future_map):
            doc = future_map[future]
            try:
                results.extend(future.result())
            except Cancelled:
                raise
            except Exception as exc:
                log(args, f"  explain failed for {doc}: {exc}")
                raise

    cached, total = cache_hit_ratio([u for c in conversations for u in c.usages])
    if total:
        log(args, f"  prompt cache: {cached}/{total} tokens hit ({100 * cached / total:.0f}%)")

    missing = [r["item_id"] for r in results if not r.get("has_official_explanation")]
    if missing:
        log(args, f"  {len(missing)} 道题原卷未提供官方解析，教师版将只有 AI 生成的详细解析：{', '.join(missing)}")

    results.sort(key=lambda r: (section_order(r.get("section", "")), r.get("source_doc", ""), r.get("item_id", "")))
    explained_path = out_dir / "selected_items.explained.json"
    write_json(explained_path, results)
    write_json(selection_path, results)
    log(args, f"Explanation outputs written: {explained_path} ({file_size_label(explained_path)})")
    return results


def render_explanation(explanation: object) -> str:
    """The AI explanation as the plain text that goes under a question in Word.

    Word has no Markdown renderer, so anything Markdown-ish here reaches the
    teacher as literal asterisks. Writing items have their own shape (审题 / 思路 /
    评分要点); everything else is a list of questions.
    """
    if not isinstance(explanation, dict):
        return ""
    if "questions" in explanation:
        return _render_question_explanations(explanation.get("questions"))
    return _render_writing_explanation(explanation)


def _render_question_explanations(questions: object) -> str:
    if not isinstance(questions, list):
        return ""
    lines: list[str] = []
    for entry in questions:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("number", "")).strip()
        answer = str(entry.get("answer", "")).strip()
        # Each section names its "what kind of question is this" field differently:
        # 阅读 calls it question_type, 七选五 function (空格功能), 完形/语法 point (考点).
        kind = str(entry.get("question_type") or entry.get("function") or entry.get("point") or "").strip()
        head = f"{number}. {answer}".strip(". ")
        if kind:
            head = f"{head}　{kind}"
        lines.append(head)

        for label, key in (
            ("定位", "locate"),
            ("线索", "clues"),
            ("这个空缺什么", "need"),
            ("依据", "reasoning"),
        ):
            value = str(entry.get(key, "") or "").strip()
            if value:
                lines.append(f"　{label}：{value}")

        for wrong in entry.get("distractors") or []:
            if isinstance(wrong, dict):
                option = str(wrong.get("option", "")).strip()
                why = str(wrong.get("why_wrong", "")).strip()
                if why:
                    lines.append(f"　为什么不选 {option}：{why}" if option else f"　易错点：{why}")

        note = str(entry.get("language_note", "") or "").strip()
        if note:
            lines.append(f"　语言点：{note}")
        lines.append("")
    return "\n".join(lines).strip()


# A letter's greeting and sign-off do not count towards the word limit, so neither
# can our count of them — the teacher checks it against the 100–120 the prompt asked
# for, and a number that silently includes "Dear Mr. Smith / Yours, Li Hua" is four
# words of nonsense at exactly the moment she is deciding whether the essay is too long.
_SALUTATION = re.compile(r"^\s*(?:Dear\b|To whom)", re.I)
_SIGN_OFF = re.compile(
    r"^\s*(?:Yours|Sincerely|Best wishes|Best regards|Regards|Li Hua|Faithfully)\b[,.\s]*$",
    re.I,
)


def essay_word_count(essay: str) -> int:
    """Words in the body of a model essay, greeting and sign-off excluded."""
    body = [
        line for line in essay.split("\n")
        if line.strip() and not _SALUTATION.match(line) and not _SIGN_OFF.match(line)
    ]
    return len(re.findall(r"[A-Za-z][A-Za-z'’\-]*", "\n".join(body)))


def _render_writing_explanation(explanation: dict) -> str:
    lines: list[str] = []
    brief = explanation.get("审题")
    if isinstance(brief, dict):
        lines.append("审题")
        for key, value in brief.items():
            if isinstance(value, list):
                value = "；".join(str(v) for v in value)
            if str(value).strip():
                lines.append(f"　{key}：{value}")
        lines.append("")

    for n, idea in enumerate(explanation.get("思路") or [], start=1):
        if not isinstance(idea, dict):
            continue
        lines.append(f"思路 {n}：{str(idea.get('角度', '')).strip()}")
        outline = idea.get("提纲")
        if isinstance(outline, list) and outline:
            for step in outline:
                lines.append(f"　· {step}")
        essay = str(idea.get("范文", "") or "").strip()
        if essay:
            # Counted, not trusted: the model leaves 词数 out often enough, and the
            # word count is the first thing a teacher checks against the 100–120 /
            # 150–180 the prompt asked for.
            lines.append(f"　范文（{essay_word_count(essay)} 词）：")
            lines.extend(f"　{line}" for line in essay.split("\n"))
        highlights = idea.get("亮点")
        if isinstance(highlights, dict):
            for key, value in highlights.items():
                if isinstance(value, list):
                    value = "；".join(str(v) for v in value)
                if str(value).strip():
                    lines.append(f"　{key}：{value}")
        lines.append("")

    scoring = str(explanation.get("评分要点", "") or "").strip()
    if scoring:
        lines.append("评分要点")
        lines.append(f"　{scoring}")
    return "\n".join(lines).strip()


def md_escape_heading(text: str) -> str:
    return str(text or "").replace("\n", " ").strip()


def assert_selection_is_complete(out_dir: Path) -> None:
    """Refuse to export a selection whose questions have not all been worked through.

    Re-selecting is the case this exists for. ``run_select`` and ``run_review_select``
    both overwrite ``selected_items.json`` with plain score rows, so after the teacher
    swaps a question in:

    * the new question has no ``explanations/<item_id>.json``, and the teacher edition
      used to export it anyway — one paper in the middle of the handout with an empty
      「详细解析和解答步骤」 section and no warning anywhere;
    * the word list must cover what is being exported — and what that means depends on how
      the list was built, so the check is dispatched on the list's own ``vocab_mode``
      rather than on whatever the command line happens to say today.

    Both are the kind of wrong-but-plausible file that gets handed out before anyone
    notices, so this stops the export instead.
    """
    selection = out_dir / "selected_items.json"
    rows = read_json(selection)
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"Missing or empty {selection}. Run --mode select first.")
    wanted = [str(row.get("item_id", "")) for row in rows if isinstance(row, dict)]

    missing = [item_id for item_id in wanted if not (out_dir / "explanations" / f"{safe_filename(item_id)}.json").exists()]
    if missing:
        raise SystemExit(
            f"{len(missing)} 道选中的题还没有逐题解析，教师讲解版会缺页：{', '.join(missing[:3])}"
            f"{' …' if len(missing) > 3 else ''}\n请先跑 --mode explain（已生成的题会走缓存，不会重复花钱）。"
        )

    index_path = out_dir / "vocab_index.json"
    if not index_path.exists():
        return  # the handout is optional; export_vocab already skips it with a note
    index = read_json(index_path)
    if not isinstance(index, list) or not index:
        return

    # Ask the word list which rules it was built under — not the command line.
    #
    # The teacher can flip 完整/困难 in 基础模式 and export without re-running vocab. If
    # this gate read args.vocab_mode it would check 分块's rules against an 整卷 list and
    # refuse a handout that is perfectly good. What has to be checked is whether *this
    # list*, on its own terms, covers what is being exported.
    #
    modes = {vocab_row_mode(row) for row in index if isinstance(row, dict)}
    if len(modes) > 1:
        raise SystemExit(
            "重难点词汇表里混着两种模式的结果（完整/困难各一半），说明上一次跑到一半换了档。\n"
            "请跑 --mode vocab --force 重出一份。"
        )
    mode = modes.pop()

    if mode == VOCAB_CHUNKED:
        # 完整（分块）: the words are per-question, so a question that was swapped in after
        # the list was built has no words on the sheet at all.
        covered = {str(row.get("item_id", "")) for row in index if isinstance(row, dict)}
        stale = [item_id for item_id in wanted if item_id not in covered]
        if stale:
            raise SystemExit(
                f"重难点词汇表还是上一批选题的（{len(stale)} 道新题不在里面）。\n"
                "请先跑 --mode vocab，否则学生拿到的是旧词表。"
            )
        return

    # 困难（整卷）: the words belong to the paper, so re-picking questions inside a paper we
    # have already read cannot stale anything. What still has to hold is that every paper
    # contributing a question was actually read.
    covered = {str(row.get("source_doc", "")) for row in index if isinstance(row, dict)}
    needed = {str(row.get("source_doc", "")) for row in rows if isinstance(row, dict) and row.get("source_doc")}
    stale = sorted(needed - covered)
    if stale:
        raise SystemExit(
            f"重难点词汇表少了 {len(stale)} 份卷子：{', '.join(stale[:3])}"
            f"{' …' if len(stale) > 3 else ''}\n请先跑 --mode vocab，否则学生拿到的词表不完整。"
        )


def merge_cached_explanations(selected: list[dict], explained_cache: object) -> int:
    """Restore explanations onto rows that lost them.

    ``selected_items.json`` is rewritten by several stages, so a row can come back
    without the explanation the export needs. The cache refills those, but a row
    that already carries an explanation keeps it — the cache is a fallback, not the
    authority, or a re-run with ``--force`` would be undone by its own leftovers.
    """
    if not isinstance(explained_cache, list):
        return 0
    by_id = {
        str(item.get("item_id") or ""): item
        for item in explained_cache
        if isinstance(item, dict) and isinstance(item.get("explanation"), dict)
    }
    merged = 0
    for item in selected:
        if not isinstance(item, dict) or isinstance(item.get("explanation"), dict):
            continue
        cached = by_id.get(str(item.get("item_id") or ""))
        if cached:
            item["explanation"] = cached["explanation"]
            item["has_official_explanation"] = bool(cached.get("has_official_explanation"))
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
    explained_cache_path = out_dir / "selected_items.explained.json"
    if explained_cache_path.exists():
        merged = merge_cached_explanations(selected, read_json(explained_cache_path))
        if merged:
            log(args, f"Merged cached explanations into {merged} selected item(s) before assembly.")
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
        "# 教师讲解",
        "",
        "> 每题：原卷的官方答案与解析，再加一份 AI 逐题详细解析。",
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
                f"- 入选理由：{score.get('selection_reason', '')}",
                "",
                "#### 官方答案与解析",
                "",
                official_explanation_text(segment) or f"{answer_key_text(segment.get('answer_key'))}\n（原卷未提供逐题解析）",
                "",
                "#### 详细解析和解答步骤",
                "",
                render_explanation(row.get("explanation")) or "暂无",
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

    # A paper whose answers came as their own document has nothing to scan in its own
    # text — the answers are in the file it was paired with.
    pairing_path = out_dir / "answer_pairing.json"
    pairing = read_json(pairing_path) if pairing_path.exists() else {}
    if not isinstance(pairing, dict):
        pairing = {}

    for source_doc, rows in sorted(by_doc.items()):
        answers_doc = pairing.get(source_doc) or source_doc
        stem = safe_stem(answers_doc)
        text_path = extracted_dir / f"{stem}.txt"
        if not text_path.exists():
            log(args, f"  skip {source_doc}: extracted text not found at {text_path}")
            continue
        if answers_doc != source_doc:
            log(args, f"  {source_doc}: 答案取自配对的「{answers_doc}」")

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


def precheck_ask_model(args: argparse.Namespace, name: str, text: str) -> dict:
    """Ask the fast model whether a suspect paper is a coherent English exam paper.

    Only ever called for papers the local checks already flagged, so the spend is bounded
    to the handful that look broken. Runs on the FLASH role with thinking off; the call is
    recorded in the usage ledger like any other.
    """
    provider = pv.get(getattr(args, "provider", pv.DEFAULT_PROVIDER))
    model = provider.role_model(pv.FLASH)
    sample = (text or "").strip()[:3000]
    prompt = (
        "下面是从一个文件里抽取出来的文本，本应是一份高中英语试卷。"
        "请判断它是不是内容连贯、可用的英语试卷，还是抽取/OCR 出了乱码、残缺或错乱。"
        '只输出 JSON：{"ok": true 或 false, "reason": "一句话"}，JSON 的语法符号用英文半角引号。\n\n'
        f"----\n{sample}\n----"
    )
    result = call_stage_model(
        args, prompt, model=model,
        reasoning_effort=getattr(args, "segment_reasoning_effort", "high"),
        thinking="disabled", max_tokens=200, stage="precheck", item_id=name,
    )
    parsed = parse_model_json(result.content)
    if isinstance(parsed, dict):
        return parsed
    return {"ok": None, "reason": (result.content or "").strip()[:200]}


def precheck_findings(args: argparse.Namespace, suspects: list[tuple[str, str, dict]]) -> list[str]:
    """Turn local-suspect papers into human lines for the summary, escalating each to the
    fast model when escalation is on and an API key is available. Warn-only; never aborts."""
    escalate = getattr(args, "precheck_escalate", True)
    api_key = (args.api_key or os.environ.get(args.api_key_env)) if escalate else None
    lines: list[str] = []
    for name, text, result in suspects:
        line = f"{name}：{'；'.join(result['reasons'])}"
        if escalate and api_key:
            verdict = precheck_ask_model(args, name, text)
            ok = verdict.get("ok")
            reason = str(verdict.get("reason") or "").strip()
            if ok is False:
                line += f" ／ flash 复核：判为坏（{reason}）"
            elif ok is True:
                line += f" ／ flash 复核：看着还能用（{reason}）"
            else:
                line += f" ／ flash 复核未给明确结论（{reason}）"
        elif escalate and not api_key:
            line += " ／ 未设 API key，已跳过 flash 复核"
        lines.append(line)
    return lines


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
    suspect_docs: list[tuple[str, str, dict]] = []
    total_tokens = 0
    local_segment_tokens = 0
    for docx in docx_files:
        text = extract_docx_text(docx)
        total_chars += len(text)
        total_tokens += count_tokens(text)
        pc = input_precheck.precheck_text(text)
        if pc["suspect"]:
            suspect_docs.append((docx.name, text, pc))
        if args.segment_input == "local":
            segments = local_segment_paper(docx.name, text)
            local_segment_count += len(segments)
            local_segment_chars += sum(len(segment_body(segment)) for segment in segments)
            local_segment_tokens += sum(count_tokens(segment_body(segment)) for segment in segments)
            if len(segments) < 7:
                low_confidence_docs.append(f"{docx.name}（本地只识别到 {len(segments)} 个单元）")

    api_segment_calls = 0 if args.segment_input == "local" else len(docx_files)
    score_calls = local_segment_count if args.segment_input == "local" else "取决于 segment 输出"
    approx_score_prompt_tokens = local_segment_tokens if args.segment_input == "local" else "取决于 segment 输出"
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
    counter = "DeepSeek 分词器" if deepseek_tokens.is_exact() else "字符估算（分词器未加载）"
    log(args, f"  extracted text chars: {total_chars:,} → {total_tokens:,} tokens（{counter}）")
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
    if suspect_docs:
        log(args, f"  ⚠️ 输入预检发现 {len(suspect_docs)} 份卷子可疑（仅警告，不影响流程）：")
        for line in precheck_findings(args, suspect_docs):
            log(args, f"    - {line}")
    else:
        log(args, "  输入预检：全部通过。")


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
    # The teacher edition is 原题 + 官方解析 + 详细解析和解答步骤, so what it needs from the
    # model is per-question explanation, not the vocabulary/long-sentence notes that
    # `enrich` produces — nothing renders those any more. `enrich-selected` is still
    # a mode you can run by hand; it is just no longer part of the chain.
    run_explain(args)
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
        choices=["prompts", "analyze", "final", "preflight", "segment", "score", "select", "review-select", "enrich-selected", "explain", "vocab", "assemble", "repair-answers", "quality-report", "export-docx", "stage1"],
        default="prompts",
    )
    parser.add_argument("--prompt-template", default="config/analysis_prompt_template.md")
    parser.add_argument(
        "--provider",
        choices=list(pv.PROVIDER_ORDER),
        default=pv.DEFAULT_PROVIDER,
        help="Which API to talk to. Decides the protocol, the thinking field and which "
             "strength levels exist — see scripts/providers.py.",
    )
    # These three default to None and are filled in from the provider after parsing, so
    # that picking a provider is enough and nobody has to also remember its URL, its key
    # variable and its model names.
    parser.add_argument("--base-url", default=None, help="API root (not the /chat/completions path).")
    parser.add_argument("--api-key-env", default=None, help="Env var holding the key. Defaults to the provider's.")
    parser.add_argument("--model", default=None, help="Model for the legacy analyze/final modes.")
    # The per-stage model/effort/thinking flags below default to None so that
    # --preset can fill them without overriding anything the caller asked for
    # explicitly. apply_preset() resolves them right after parsing.
    parser.add_argument(
        "--preset",
        choices=[mp.SPEED, mp.QUALITY],
        default=mp.SPEED,
        help="speed = flash everywhere (cheap, for test runs); quality = pro (for the batch you hand out). "
             "Individual --score-model/--explain-model/... still win over the preset.",
    )
    parser.add_argument("--segment-model", default=None, help="Model used to split full papers into section JSON files.")
    parser.add_argument("--score-model", default=None, help="Model used to score each segmented item.")
    parser.add_argument("--review-model", default=None, help="Model used to review local shortlist selections.")
    parser.add_argument("--enrich-model", default=None, help="Model used for the legacy enrich mode.")
    parser.add_argument("--explain-model", default=None, help="Model used to write the per-question explanations in the teacher edition.")
    # vocab used to ride on --enrich-model with its effort hardcoded, so the词汇表
    # silently ignored whichever preset the teacher had picked. It is its own stage now.
    parser.add_argument("--vocab-model", default=None, help="Model used for the student vocabulary handout.")
    parser.add_argument("--segment-workers", type=int, default=16, help="Concurrent docx segmentation requests.")
    parser.add_argument("--score-workers", type=int, default=16, help="Concurrent scoring requests.")
    parser.add_argument("--enrich-workers", type=int, default=16, help="Concurrent vocabulary/enrichment conversations (one per paper).")
    # Defaults to --enrich-workers so the GUI, which passes only that one, still
    # drives this. Both are capped by the number of papers anyway: a conversation
    # has to stay on one paper for its prefix to keep hitting the cache.
    parser.add_argument("--explain-workers", type=int, default=None, help="Concurrent explanation conversations (one per paper).")
    parser.add_argument("--segment-input", choices=["local", "rough", "full"], default="local", help="local parses common exam structure without API; rough/full use model segmentation.")
    parser.add_argument(
        "--segment-warning-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When local segmentation has structural WARN/FAIL, re-segment only affected papers with rough model chunks.",
    )
    parser.add_argument(
        "--precheck-escalate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="In preflight, escalate a locally-suspect paper (mojibake/near-empty) to the fast model for a coherence check. Warn-only, never blocks.",
    )
    parser.add_argument(
        "--answer-pairing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recognise a separate 答案 document and match it to its paper, instead of segmenting it as if it were one.",
    )
    parser.add_argument(
        "--pairing-confirm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Have the fast model confirm each proposed paper↔answers pairing. Off means trust the filenames alone.",
    )
    parser.add_argument("--answer-tail-chars", type=int, default=8000, help="Characters of the final answer area appended to rough segment chunks.")
    parser.add_argument("--review-candidates", type=int, default=6, help="Local shortlist size per section before pro review.")
    # No `choices=` on any effort flag: argparse fixes its choices at parse time, but
    # which levels exist is a property of the provider you have not chosen yet (DeepSeek
    # has 2, OpenAI 6, GLM none). apply_preset() normalises them once the provider is
    # known, and an unknown level folds onto a real one instead of aborting the run.
    parser.add_argument("--segment-reasoning-effort", default="high")
    parser.add_argument("--score-reasoning-effort", default=None)
    parser.add_argument("--review-reasoning-effort", default=None)
    parser.add_argument("--enrich-reasoning-effort", default=None)
    parser.add_argument("--explain-reasoning-effort", default=None)
    parser.add_argument("--segment-thinking", choices=["enabled", "disabled", "omit"], default="disabled")
    parser.add_argument("--score-thinking", choices=["enabled", "disabled", "omit"], default=None)
    parser.add_argument("--review-thinking", choices=["enabled", "disabled", "omit"], default=None)
    parser.add_argument("--enrich-thinking", choices=["enabled", "disabled", "omit"], default=None)
    parser.add_argument("--explain-thinking", choices=["enabled", "disabled", "omit"], default=None)
    # PDF input.
    parser.add_argument("--pdf-backend", choices=["paddle", "mineru"], default="paddle", help="Which OCR service turns a PDF into a .docx.")
    # PaddleOCR's layout-parsing service is deployed per AI Studio account, so the base
    # URL has to come from the user's own console — there is no global one.
    parser.add_argument("--paddle-base-url", default="", help="PaddleOCR layout-parsing URL (or set PADDLEOCR_BASE_URL).")
    parser.add_argument("--paddle-token", default="", help="AI Studio access token (or set PADDLEOCR_ACCESS_TOKEN).")
    parser.add_argument("--mineru-token", default="", help="MinerU API token (or set MINERU_TOKEN).")
    parser.add_argument("--api-key", default="")
    # DEEPSEEK TUNING: `auto` tries the OpenAI SDK first, then falls back to raw HTTP.
    parser.add_argument("--client", choices=["auto", "sdk", "http"], default="auto")
    parser.add_argument(
        "--insecure-ssl",
        action="store_true",
        help="Skip HTTPS certificate verification. Only for a network that intercepts TLS and whose "
             "root certificate is not in the macOS Keychain — see scripts/net_tls.py.",
    )
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--thinking", choices=["enabled", "disabled", "omit"], default="enabled")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=8, help="Max retries for retryable API errors such as 429 rate limits.")
    parser.add_argument("--max-tokens", type=int, default=6000, help="Output token cap for legacy analyze/final modes.")
    parser.add_argument("--segment-max-tokens", type=int, default=16000, help="Output token cap for AI segmentation. Ignored by local segmentation.")
    # DEEPSEEK TUNING: reasoning tokens are billed and *counted* as output, so
    # max_tokens has to cover the thinking as well as the answer. With thinking on,
    # a 1200-token cap left one scoring call literally 0 tokens for its JSON: the
    # reply was truncated, the score silently became unparseable, and the item was
    # then ranked as if it had scored zero. These caps are sized for reasoning +
    # answer, not answer alone.
    parser.add_argument("--score-max-tokens", type=int, default=4000, help="Output token cap for each lightweight score call (includes reasoning tokens).")
    parser.add_argument("--review-max-tokens", type=int, default=6000, help="Output token cap for each review-select call (includes reasoning tokens).")
    parser.add_argument("--enrich-max-tokens", type=int, default=8000, help="Output token cap for each selected-item enrichment call (includes reasoning tokens).")
    # An explain turn covers at most 5 questions (see EXPLAIN_CHUNK_SIZE), but a
    # writing item asks for 2-3 full model essays plus their analysis in one reply,
    # which is the largest thing this pipeline asks any model to produce.
    parser.add_argument("--explain-max-tokens", type=int, default=16000, help="Output token cap for each explanation turn (includes reasoning tokens).")
    # Vocabulary reads a whole paper at once now, and runs at the preset's 深度 setting
    # like every other stage. 决策 9 warned about exactly this, and a measured run proves
    # it was right to: on the three sample papers, **94% of the output tokens were
    # reasoning** (87,395 of 92,853), and the worst single call spent 38,934 tokens to
    # emit a word list of about 1,900. At the old 16,000 cap all three calls would have
    # been truncated.
    #
    # So the cap has to clear that by a real margin, not by luck. 22000 x3 lands on
    # MAX_EFFORT_TOKEN_CEILING (64,000), which leaves ~39% headroom over the worst
    # observed call rather than the 19% that 48,000 gave.
    #
    # If this ever starts reporting 「被 max_tokens 截断」 again, the answer is not another
    # bump: pin vocab back to the standard effort. It produced a word list of the same
    # size (114 vs 116) in a quarter of the time and a quarter of the money.
    # Which way to build the handout. A teaching choice, not a technical one — 基础模式
    # puts it in front of the teacher:
    #   chunked (完整) — one call per selected question; the words match the paper she
    #       is handing out, question for question.
    #   whole   (困难) — read the paper end to end; the model can judge whether a word is
    #       genuinely hard for *this* paper, at the cost of naming words from questions
    #       the student's copy does not contain.
    parser.add_argument("--vocab-mode", choices=list(VOCAB_MODES), default=VOCAB_WHOLE,
                        help="chunked = 完整（逐题分块）; whole = 困难（通读整卷）。")
    parser.add_argument("--vocab-max-tokens", type=int, default=22000, help="Output token cap for each vocabulary call (includes reasoning tokens).")
    parser.add_argument("--vocab-reasoning-effort", default=None)
    parser.add_argument("--vocab-thinking", choices=["enabled", "disabled", "omit"], default=None)
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
    parser.add_argument("--notify", action="store_true", help="Post a macOS notification when the run finishes (success or failure). For background/scripted runs.")
    parser.add_argument("--review-select", action="store_true", help="In --mode stage1, run pro review-select after local select.")
    parser.add_argument(
        "--reselect",
        action="store_true",
        help="In --mode review-select, tell the model the current picks were rejected and to choose different ones.",
    )
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
    return apply_preset(parser.parse_args(argv))


# (stage, effort flag, output-cap flag) for every stage that talks to the model.
# segment is here for its caps even though no preset drives it: it runs locally, and its
# model is only the fallback for a paper whose structure no rule can read.
STAGE_EFFORT_CAPS = (
    ("segment", "segment_reasoning_effort", "segment_max_tokens"),
    ("score", "score_reasoning_effort", "score_max_tokens"),
    ("review", "review_reasoning_effort", "review_max_tokens"),
    ("enrich", "enrich_reasoning_effort", "enrich_max_tokens"),
    ("explain", "explain_reasoning_effort", "explain_max_tokens"),
    ("vocab", "vocab_reasoning_effort", "vocab_max_tokens"),
)


def apply_preset(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in everything the caller left unset, now that we know who we are talking to.

    The flags default to ``None`` rather than to a model name so that "unset" is
    distinguishable from "set to the same value the preset would have chosen" — an
    explicit ``--score-model deepseek-v4-pro`` has to survive ``--preset speed``. The
    same trick now covers the provider's own URL, key variable and models, so that
    ``--provider anthropic`` alone is a complete instruction.
    """
    provider = pv.get(args.provider)

    if not args.base_url:
        args.base_url = provider.base_url
    if not args.api_key_env:
        args.api_key_env = provider.api_key_env
    if not args.model:
        args.model = provider.role_model(pv.PRO)
    if not args.segment_model:
        args.segment_model = mp.default_segment_model(args.provider)

    for key, value in mp.preset_values(args.preset, args.provider).items():
        # --score-reasoning-effort lands in args as score_reasoning_effort.
        attr = key.replace("_effort", "_reasoning_effort")
        if getattr(args, attr, None) is None:
            setattr(args, attr, value)
    if args.explain_workers is None:
        args.explain_workers = args.enrich_workers

    # Fold away any level this provider does not really have (a `max` saved against
    # DeepSeek arriving at GLM, which has no strength dial at all), then scale the output
    # caps for every stage running at its model's deepest setting. Done here, once, so
    # the *same* number reaches both the request and require_parsed() — scaling it inside
    # the client would leave the truncation check comparing against the old cap and
    # calling a perfectly healthy reply truncated.
    for stage, effort_attr, cap_attr in STAGE_EFFORT_CAPS:
        model = stage_model_name(args, stage)
        effort = mp.normalize_effort(getattr(args, effort_attr, "") or "", args.provider, model)
        setattr(args, effort_attr, effort)
        setattr(
            args,
            cap_attr,
            effective_max_tokens(getattr(args, cap_attr, None), effort, args.provider, model),
        )
    return args


def main(argv: list[str]) -> int:
    # Before any TLS happens: on a Mac whose network re-signs HTTPS (school proxy,
    # antivirus), Python does not see the interception root that macOS already trusts,
    # and every call dies with "self-signed certificate in certificate chain".
    net_tls.install()

    args = parse_args(argv)
    log(args, f"Pipeline started: mode={args.mode}, input={args.input}, out={args.out}")
    if args.insecure_ssl:
        log(args, "  ⚠️ 已跳过 HTTPS 证书校验（--insecure-ssl）。仅在网络拦截证书时才该这样用。")
    if args.init or args.init_only:
        reset_output_dir(args)
        if args.init_only:
            log(args, "Initialization finished; exiting because --init-only was used.")
            return 0
    try:
        dispatch_mode(args)
    except BaseException as exc:  # noqa: BLE001 — notify on any exit, then re-raise unchanged
        if getattr(args, "notify", False):
            notify_mod.notify("AI英语试卷整理工具", f"运行失败：{type(exc).__name__}: {exc}", success=False)
        raise
    if getattr(args, "notify", False):
        notify_mod.notify("AI英语试卷整理工具", f"整理完成（{args.mode}）：{Path(args.out)}", success=True)
    return 0


def dispatch_mode(args: argparse.Namespace) -> None:
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
    elif args.mode == "explain":
        run_explain(args)
        log(args, f"Pipeline finished successfully: explained selected items under {Path(args.out)}")
    elif args.mode == "vocab":
        run_vocab(args)
        log(args, f"Pipeline finished successfully: vocabulary extracted under {Path(args.out)}")
    elif args.mode == "assemble":
        run_assemble(args)
        log(args, f"Pipeline finished successfully: wrote assembled markdown under {Path(args.out)}")
    elif args.mode == "export-docx":
        # The export script lives alongside this file; add it to sys.path
        _scripts_dir = Path(__file__).resolve().parent
        if str(_scripts_dir) not in sys.path:
            sys.path.insert(0, str(_scripts_dir))
        import export_docx_splice
        import export_vocab_docx
        docx_out = Path(args.out) / "docx_exports"
        assert_selection_is_complete(Path(args.out))
        created = export_docx_splice.export_selected(
            Path(args.out), sys.modules[__name__], log=lambda m: log(args, m)
        )
        # The handout is optional: a run that skipped --mode vocab still exports.
        vocab_doc = export_vocab_docx.export_vocab(Path(args.out), log=lambda m: log(args, m))
        if vocab_doc:
            created.append(vocab_doc)
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
