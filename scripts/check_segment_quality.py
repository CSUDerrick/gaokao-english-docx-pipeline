#!/usr/bin/env python3
"""Local segment quality checker — no AI, no network.

Reads segment_index.csv and segments/*.json, then writes a Markdown
quality report with per-paper diagnostics and risk grades.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from segment_quality import (
    EXPECTED_SECTIONS,
    HARD_LEAK_TOKENS,
    SECTION_DISPLAY,
    SOFT_LAYOUT_TOKENS,
    WRITING_LEAK_TOKENS,
    evaluate_document,
)


def collect_rows(out_dir: Path) -> list[dict]:
    csv_path = out_dir / "segment_index.csv"
    if not csv_path.exists():
        raise SystemExit(f"Missing {csv_path}. Run --mode segment first.")
    with csv_path.open("r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def check_hard_leak_tokens(text: str) -> list[str]:
    """Return answer/transcript leak tokens found in text."""
    found = []
    for tok in HARD_LEAK_TOKENS:
        if tok in text:
            found.append(tok)
    return found


def check_soft_layout_tokens(text: str) -> list[str]:
    """Return layout/instruction tokens found in text."""
    found = []
    for tok in SOFT_LAYOUT_TOKENS:
        if tok in text:
            found.append(tok)
    return found


def check_writing_leak(text: str) -> list[str]:
    """Return writing-specific leak tokens found in text."""
    found = []
    for tok in WRITING_LEAK_TOKENS:
        if tok in text:
            found.append(tok)
    return found


def run(out_dir: Path) -> None:
    rows = collect_rows(out_dir)
    report_path = out_dir / "segment_quality_report.md"

    # Group by doc
    by_doc: dict[str, list[dict]] = defaultdict(list)
    doc_order: list[str] = []
    for r in rows:
        doc = r["source_doc"]
        if doc not in by_doc:
            doc_order.append(doc)
        by_doc[doc].append(r)

    doc_results = [evaluate_document(doc, by_doc[doc]) for doc in doc_order]

    # --- build report ---
    lines: list[str] = []
    def w(s: str = "") -> None: lines.append(s)

    w("# Segment Quality Report")
    w()
    w(f"**输出目录**：`{out_dir}`")
    w()

    # ---- A. Overview ----
    w("## A. 总览")
    w()
    total_segs = len(rows)
    grades = Counter(d["grade"] for d in doc_results)
    w("| 指标 | 值 |")
    w("|---|---|")
    w(f"| 输入 docx | {len(doc_order)} |")
    w(f"| 成功切分 docx | {sum(1 for d in doc_results if d['seg_count'] > 0)} |")
    w(f"| 总 segment 数 | {total_segs} |")
    w(f"| 平均 segment 数 | {total_segs / len(doc_order):.1f} |")
    clean = sum(1 for d in doc_results if d['sub_grade'] == 'clean')
    tail_bleed = sum(1 for d in doc_results if d['sub_grade'] == 'tail-bleed')
    w(f"| PASS (clean) | {clean} |")
    w(f"| PASS (tail bleed) | {tail_bleed} |")
    w(f"| WARN (structural) | {grades.get('WARN', 0)} |")
    w(f"| FAIL | {grades.get('FAIL', 0)} |")
    w()
    w("### Segment 类型分布")
    w()
    section_dist = Counter(r["section"] for r in rows)
    w("| 题型 | 数量 | 覆盖率 |")
    w("|---|---|---|")
    for s in EXPECTED_SECTIONS:
        cnt = section_dist.get(s, 0)
        pct = f"{cnt / len(doc_order) * 100:.0f}%"
        w(f"| {SECTION_DISPLAY.get(s, s)} | {cnt} | {pct} |")
    w()

    # ---- B. Per-paper detail ----
    w("## B. 每份试卷切分详情")
    w()
    for d in doc_results:
        grade_emoji = {"PASS": "🟢", "WARN": "🟡", "FAIL": "🔴"}.get(d["grade"], "⚪")
        sub_label = {"clean": "clean", "tail-bleed": "tail bleed only", "structural": "structural"}.get(d.get("sub_grade", ""), "")
        label = f"{d['grade']} ({sub_label})" if sub_label else d["grade"]
        w(f"### {grade_emoji} {d['seg_count']} segments · {label}")
        w()
        w(f"**文件**：`{d['doc']}`")
        w()
        w(f"**题型列表**：{', '.join(SECTION_DISPLAY.get(s, s) for s in d['sections'])}")
        w()
        if d["missing"]:
            w(f"**缺少**：{', '.join(SECTION_DISPLAY.get(s, s) for s in d['missing'])}")
            w()
        if d["duplicates"]:
            w(f"**重复**：{', '.join(SECTION_DISPLAY.get(s, s) for s in d['duplicates'])}")
            w()

        w("| 题型 | 标签 | 字符数 | 结构疑点 | 尾部粘连 |")
        w("|---|---|---|---|---|")
        for sd in d["details"]:
            s_text = "; ".join(sd["structural"]) if sd["structural"] else "—"
            c_text = "; ".join(sd["cosmetic"]) if sd["cosmetic"] else "—"
            w(f"| {sd['display']} | {sd['label']} | {sd['chars']:,} | {s_text} | {c_text} |")
        w()

        if d["structural_issues"]:
            w("**结构疑点**：")
            for iss in d["structural_issues"]:
                w(f"- {iss}")
            w()
        if d["cosmetic_issues"]:
            w("**尾部粘连（已知限制）**：")
            for iss in d["cosmetic_issues"]:
                w(f"- {iss}")
            w()

    # ---- C. Anomaly rules summary ----
    w("## C. 异常规则说明")
    w()
    w("""
| 规则 | 阈值 | 级别 |
|---|---|---|
| segment 数量过少 | < 8 | WARN |
| segment 数量过多 | > 12 | WARN |
| 缺少关键题型 | 阅读/完形/语法/写作任一缺失 | WARN |
| 某 segment 文本过短 | < 100 字符 | WARN |
| 某 segment 文本异常过长 | > 12,000 字符 | WARN |
| 重复题型 | 同一 docx 中同一 type 出现 > 1 次 | WARN |
| 含答案/听力边界词 | "答案解析""听力原文""Text 1"等 | WARN |
| 含普通版面提示 | "答题卡""考号""准考证号"等 | PASS* |
| 写作段含范文标记 | "One possible version""【导语】"等 | PASS* |
| 缺少全部写作题型 | 无 practical_writing 且无 continuation_writing | WARN |
| 缺少 3 种以上关键题型 | 3+ critical sections missing | FAIL |
""")

    # ---- D. Risk summary ----
    w("## D. 风险分级")
    w()
    w("| 文件 | segments | 缺少 | 结构 | 粘连 | 等级 |")
    w("|---|---|---|---|---|---|")
    for d in doc_results:
        missing_str = ", ".join(SECTION_DISPLAY.get(s, s) for s in d["missing"]) or "—"
        grade_emoji = {"PASS": "🟢", "WARN": "🟡", "FAIL": "🔴"}.get(d["grade"], "⚪")
        struct_str = f"{len(d['structural_issues'])}" if d["structural_issues"] else "—"
        cosm_str = f"{len(d['cosmetic_issues'])}" if d["cosmetic_issues"] else "—"
        sg = d.get("sub_grade", "")
        sg_short = {"clean": "PASS", "tail-bleed": "PASS*", "structural": "WARN"}.get(sg, d["grade"])
        w(f"| `{d['doc'][:45]}` | {d['seg_count']} | {missing_str} | {struct_str} | {cosm_str} | {grade_emoji} {sg_short} |")
    w()

    # ---- E. Manual review suggestions ----
    w("## E. 人工抽查建议")
    w()
    to_review = [d for d in doc_results if d["grade"] in ("WARN", "FAIL")]
    if not to_review:
        w("✅ 无需人工抽查。")
    else:
        for d in to_review:
            w(f"### {d['doc']}")
            w()
            if d["structural_issues"]:
                w("**需关注**：")
                for iss in d["structural_issues"]:
                    w(f"- {iss}")
                w()
            for sd in d["details"]:
                if sd["structural"]:
                    seg_rel = Path(sd["path"]).name if sd["path"] else "?"
                    w(f"- `segments/{seg_rel}` — {sd['display']} — {', '.join(sd['structural'])}")
            w()

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report written: {report_path}")
    print(f"  PASS: {grades.get('PASS', 0)}  WARN: {grades.get('WARN', 0)}  FAIL: {grades.get('FAIL', 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local segment quality checker")
    parser.add_argument("--out", default="outputs/gaokao_english_segment_check",
                        help="Output directory containing segment_index.csv and segments/")
    args = parser.parse_args()
    run(Path(args.out))


if __name__ == "__main__":
    main()
