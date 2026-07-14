#!/usr/bin/env python3
"""End-to-end health check on a finished run's Word output.

The failures this catches all shipped at least once: answers printed inside the
student paper, the source paper's filename on a student handout, literal "**" on
the page, a bookmark whose partner was never cloned (Word: "发现无法读取的内容"),
and a section that swallowed the heading of the section after it.

    python3 scripts/check_export_quality.py --out outputs/verify_full
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import answer_explanation as ax  # noqa: E402
import docx_blocks as db  # noqa: E402
import docx_splice as ds  # noqa: E402
import export_docx_splice as ex  # noqa: E402
import export_vocab_docx as ev  # noqa: E402
import gaokao_english_docx_pipeline as pipeline  # noqa: E402
from lxml import etree  # noqa: E402
from segment_quality import answer_leaks  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# A heading that introduces the *next* section must not close the previous one.
NEXT_SECTION_HEADING = re.compile(r"^\s*(?:第[一二三四五六七八九十]+部分|第[一二三四五六]节)")

CANONICAL = [
    "reading_a", "reading_b", "reading_c", "reading_d",
    "gap_filling", "cloze", "grammar", "practical_writing", "continuation_writing",
]


def _fail(checks: list[tuple[bool, str]], ok: bool, message: str) -> None:
    checks.append((ok, message))


def check_segments(out_dir: Path, checks: list) -> None:
    seg_dir = out_dir / "segments"
    segments = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(seg_dir.glob("*.json"))]
    segments = [s for s in segments if s.get("question_text")]
    _fail(checks, bool(segments), f"segments/ 有 {len(segments)} 个切分")

    leaking = []
    trailing = []
    for seg in segments:
        text = seg.get("question_text") or ""
        if answer_leaks(text):
            leaking.append(seg.get("item_id"))
        last = [ln for ln in text.strip().splitlines() if ln.strip()]
        if last and NEXT_SECTION_HEADING.match(last[-1]):
            trailing.append(f"{seg.get('item_id')} → {last[-1][:24]}")
    _fail(checks, not leaking, f"题干内无答案键/逐题解析（越界 {len(leaking)}: {leaking[:2]}）")
    _fail(checks, not trailing, f"题干未吞下一节标题（越界 {len(trailing)}: {trailing[:2]}）")

    by_paper: dict[str, list] = {}
    for seg in segments:
        by_paper.setdefault(seg.get("source_doc", ""), []).append(seg)
    disordered = []
    for paper, segs in by_paper.items():
        order = [CANONICAL.index(s["section"]) for s in segs if s.get("section") in CANONICAL]
        starts = [s["source_blocks"][0] for s in segs if s.get("section") in CANONICAL and s.get("source_blocks")]
        pairs = sorted(zip(starts, order))
        if [o for _, o in pairs] != sorted(o for _, o in pairs):
            disordered.append(paper)
    _fail(checks, not disordered, f"每份卷的题型顺序与出现位置一致（错序 {disordered}）")


def check_student(path: Path, checks: list) -> None:
    if not path.exists():
        _fail(checks, False, f"缺少 {path.name}")
        return
    text = db.read_docx(path).text
    _fail(checks, "来源" not in text, "学生版不含「来源」")
    _fail(checks, not answer_leaks(text), f"学生版不含答案键/逐题解析 ({answer_leaks(text)[:1]})")
    _fail(checks, "【导语】" not in text and "题详解】" not in text, "学生版不含解析")
    _fail(checks, "参考范文" not in text, "学生版不含参考范文")
    _fail(checks, "**" not in text, "学生版不含 Markdown 符号")
    numbered = re.findall(r"第 (\d+) 篇", text)
    _fail(checks, numbered == [str(i) for i in range(1, len(numbered) + 1)],
          f"学生版按「第 N 篇」连续编号（共 {len(numbered)} 篇）")


def check_teacher(out_dir: Path, path: Path, checks: list) -> None:
    """The teacher edition is 原题 + 官方答案与解析 + 详细解析和解答步骤, once per question."""
    if not path.exists():
        _fail(checks, False, f"缺少 {path.name}")
        return
    text = db.read_docx(path).text
    papers = len(re.findall(r"第 \d+ 篇", text))
    official = text.count(ex.OFFICIAL_HEADING)
    ours = text.count(ex.AI_HEADING)
    _fail(checks, papers > 0, f"教师版有 {papers} 篇")
    _fail(checks, official == papers, f"每篇都有「{ex.OFFICIAL_HEADING}」（{official}/{papers}）")
    _fail(checks, ours == papers, f"每篇都有「{ex.AI_HEADING}」（{ours}/{papers}）")

    # The paper's own explanation is cloned by block index, so the one thing that
    # could go wrong silently is cloning the *wrong* question's blocks. Re-derive
    # which questions each item owns and check nothing outside that range appears.
    strayed = []
    for row in json.loads((out_dir / "selected_items.json").read_text(encoding="utf-8")):
        segment = json.loads(Path(row["segment_path"]).read_text(encoding="utf-8"))
        span = segment.get("official_explanation_blocks") or []
        if len(span) != 2:
            continue
        wanted = set(ax.question_numbers(segment.get("section", ""), segment.get("answer_key")))
        cloned = pipeline.blocks_text(pipeline.cached_docx(segment["source_path"]), span)
        for number in re.findall(r"【(\d+)题详解】", cloned):
            n = int(number)
            if n in ax.ALL_EXPLAINED_QUESTIONS and n not in wanted:
                strayed.append(f"{row['item_id']} 混入了第 {n} 题的解析")
    _fail(checks, not strayed, f"官方解析未串题（越界 {len(strayed)}: {strayed[:2]}）")


def check_word_readable(path: Path, checks: list) -> None:
    if not path.exists():
        _fail(checks, False, f"缺少 {path.name}")
        return
    doc = etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    orphans = ds.orphan_range_markers(doc)
    _fail(checks, not orphans, f"{path.name}: 书签成对（悬空 {orphans[:2]}）")
    try:
        ds.validate(path)
        _fail(checks, True, f"{path.name}: 通过 docx_splice.validate()")
    except RuntimeError as exc:
        _fail(checks, False, f"{path.name}: validate 失败 — {exc}")


def check_vocab(path: Path, checks: list) -> None:
    if not path.exists():
        _fail(checks, False, f"缺少 {path.name}（先跑 --mode vocab）")
        return
    doc = etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    tables = list(doc.iter(W + "tbl"))
    _fail(checks, len(tables) == 2, f"词汇表恰有 2 张表（实际 {len(tables)}）")
    if len(tables) == 2:
        rows = [len(list(t.iter(W + "tr"))) - 1 for t in tables]
        _fail(checks, all(r > 0 for r in rows), f"两张表都有内容（词汇 {rows[0]} 条，词形 {rows[1]} 条）")
    text = db.read_docx(path).text
    _fail(checks, "来源" not in text and not answer_leaks(text), "词汇表不含来源/答案")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="A finished run's output folder.")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    docx_dir = out_dir / "docx_exports"
    checks: list[tuple[bool, str]] = []

    check_segments(out_dir, checks)
    check_student(docx_dir / ex.STUDENT, checks)
    check_teacher(out_dir, docx_dir / ex.TEACHER, checks)
    for name in (ex.STUDENT, ex.TEACHER, ex.ANSWERS):
        check_word_readable(docx_dir / name, checks)
    check_vocab(docx_dir / ev.VOCAB, checks)

    for ok, message in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {message}")
    failed = [m for ok, m in checks if not ok]
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
