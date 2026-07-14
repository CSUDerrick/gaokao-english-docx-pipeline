#!/usr/bin/env python3
"""Build the student and teacher Word files by cloning the source papers.

Replaces the Markdown -> Pandoc path for the two documents that contain question
text. Going through Markdown meant the original typesetting had to be re-invented
(and images were dropped entirely); here each question's original ``w:p``/``w:tbl``
nodes are copied straight out of the paper it came from.

The answers-only document has no source typesetting to preserve — it is answer
letters and model essays — so it is built from the structured data instead.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

import docx_blocks as db
import docx_splice as ds
from bundle_paths import template_dir
from segment_quality import answer_leaks

STUDENT = "高三英语精选试题_学生版.docx"
TEACHER = "高三英语精选试题_教师讲解版.docx"
ANSWERS = "高三英语精选试题_答案汇总版.docx"
VOCAB = "高三英语精选试题_重难点词汇表_学生版.docx"

# Anything here in the student sheet means a defect upstream leaked an answer or
# a paper name onto the page a student actually sits with.
_STUDENT_FORBIDDEN = [
    ("题目来源", re.compile(r"来源[：:]")),
    ("答案解析", re.compile(r"【导语】|【\d+题详解】|【详解】")),
    ("参考范文", re.compile(r"参考范文|One possible version")),
    ("Markdown 符号", re.compile(r"\*\*")),
]


def assert_student_edition_is_clean(path: Path) -> None:
    """Refuse to ship a student paper that carries answers or source attribution.

    A wrong-but-plausible file is worse than no file: the teacher would hand it
    out. This is the last gate, so it checks the finished .docx rather than the
    data that built it.
    """
    text = db.read_docx(path).text
    found = [name for name, pattern in _STUDENT_FORBIDDEN if pattern.search(text)]
    found += answer_leaks(text)
    if found:
        raise RuntimeError(f"{path.name}: 学生版混入了不该出现的内容 ({', '.join(found)})")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


OFFICIAL_HEADING = "官方答案与解析"
AI_HEADING = "详细解析和解答步骤"
NO_OFFICIAL = "（原卷未提供逐题解析）"


def _ai_notes(row: dict, pipeline) -> list[tuple[str, str]]:
    """The per-question explanation that goes below the paper's own."""
    body = pipeline.render_explanation(row.get("explanation"))
    if not body.strip():
        return []
    return [(ds.NOTE_HEADING, AI_HEADING), (ds.NOTE_BODY, body)]


def _official_fallback_text(segment: dict, pipeline) -> str:
    """What to print when there are no 【N题详解】 blocks to clone.

    Not the same thing as the paper saying nothing: for the writing sections the
    paper's answer *is* its 参考范文, and ``official_explanation_text`` hands that
    back. Only when it comes back empty has the paper truly explained nothing —
    and even then the answers are known, because they come from the answer key.
    So print those and say plainly that no worked explanation exists.
    """
    official = pipeline.official_explanation_text(segment)
    if official:
        return official
    return f"{pipeline.answer_key_text(segment.get('answer_key'))}\n{NO_OFFICIAL}"


TEMPLATE_DIR = template_dir()


def _answers_doc(rows: list[dict], out: Path, pipeline) -> Path:
    # Built on the existing A4 template so the answer sheet keeps the Chinese
    # font mapping and footer that the student/teacher versions inherit from the
    # source papers. blank_template() also strips the template's own demo table,
    # which used to ride along as a stray "Table 1 2" grid.
    doc = ds.blank_template(TEMPLATE_DIR / "answers_reference.docx")
    ds.ensure_note_styles(doc)
    doc.add_paragraph("高三英语答案汇总", style=ds.NOTE_HEADING)

    current = ""
    for row in rows:
        section = row.get("display_section") or pipeline.section_display(row.get("section", ""))
        if section != current:
            doc.add_paragraph(section, style=ds.NOTE_HEADING)
            current = section
        segment = _load(Path(row["segment_path"]))
        doc.add_paragraph(str(row.get("item_label", "")), style=ds.NOTE_ANSWER)
        doc.add_paragraph(pipeline.answer_key_text(segment.get("answer_key")), style=ds.NOTE_BODY)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def _plain_doc(heading: str, text: str, notes: list[tuple[str, str]], out: Path) -> Path:
    """Fallback for a question that could not be traced back to its source.

    Loses the original typesetting, but keeps the question in the paper.
    """
    doc = ds.blank_template(TEMPLATE_DIR / "student_reference.docx")
    ds.ensure_note_styles(doc)
    doc.add_paragraph(ds.sanitize(heading), style=ds.NOTE_HEADING)
    for line in ds.sanitize(text).split("\n"):
        if line.strip():
            doc.add_paragraph(line, style=ds.NOTE_BODY)
    for style_name, body in notes:
        for line in ds.sanitize(body).split("\n"):
            if line.strip():
                doc.add_paragraph(line, style=style_name)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def export_selected(out_dir: Path, pipeline, log=print) -> list[Path]:
    selection = out_dir / "selected_items.json"
    if not selection.exists():
        raise SystemExit(f"Missing {selection}. Run --mode select first.")
    rows = _load(selection)

    explained = out_dir / "selected_items.explained.json"
    if explained.exists():
        pipeline.merge_cached_explanations(rows, _load(explained))

    rows = sorted(
        rows,
        key=lambda r: (pipeline.section_order(r.get("section", "")), r.get("source_doc", ""), r.get("item_id", "")),
    )

    docx_dir = out_dir / "docx_exports"
    docx_dir.mkdir(parents=True, exist_ok=True)

    student_parts: list[Path] = []
    teacher_parts: list[Path] = []
    skipped: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for n, row in enumerate(rows):
            segment = _load(Path(row["segment_path"]))
            src = segment.get("source_path")
            blocks = segment.get("source_blocks") or []
            label = row.get("item_label", "")
            # The student sheet must not say which paper a question came from,
            # but teacher and student have to be able to say "第 3 篇" and mean
            # the same question — hence the shared running number.
            # docxcompose takes a few seconds over ~40 parts; check between questions so
            # 取消 lands here too rather than only at the stage boundary.
            pipeline.raise_if_cancelled()

            student_heading = f"第 {n + 1} 篇　{label}"
            teacher_heading = f"第 {n + 1} 篇　{label}｜来源：{row.get('source_doc', '')}"
            cloneable = bool(src) and len(blocks) == 2 and Path(src).exists()

            # The teacher edition is three blocks per question: the question, the
            # paper's own answer and explanation, then ours. The first two are
            # clones of the original OOXML; only the third is generated text.
            notes = _ai_notes(row, pipeline)
            official = segment.get("official_explanation_blocks") or []

            if not cloneable:
                # No pointer back into an original docx (AI-segmented text the
                # anchor could not place). Render the text we do have rather than
                # dropping the question — a missing question is far worse than an
                # unstyled one.
                skipped.append(str(row.get("item_id")))
                text = segment.get("question_text") or ""
                sp = _plain_doc(student_heading, text, [], tmp / f"s{n:03d}.docx")
                tp = _plain_doc(teacher_heading, text, [], tmp / f"t{n:03d}.docx")
                student_parts.append(sp)
                teacher_parts.append(tp)
                teacher_parts.append(
                    _plain_doc(
                        OFFICIAL_HEADING,
                        _official_fallback_text(segment, pipeline),
                        notes,
                        tmp / f"t{n:03d}x.docx",
                    )
                )
                continue

            lo, hi = blocks
            indices = list(range(lo, hi))

            sp = tmp / f"s{n:03d}.docx"
            ds.clone_subset(Path(src), indices, sp)
            ds.decorate(sp, heading=student_heading)
            student_parts.append(sp)

            tp = tmp / f"t{n:03d}.docx"
            ds.clone_subset(Path(src), indices, tp)
            ds.decorate(tp, heading=teacher_heading)
            teacher_parts.append(tp)

            if len(official) == 2:
                # Clone the paper's own 【N题详解】 paragraphs, with their original
                # typesetting, the same way the question itself is cloned.
                ep = tmp / f"t{n:03d}x.docx"
                ds.clone_subset(Path(src), list(range(official[0], official[1])), ep)
                ds.decorate(ep, heading=OFFICIAL_HEADING, notes=notes)
            else:
                # The paper never explained this one (广东 skips four whole
                # sections). Print the answers anyway and say so.
                ep = _plain_doc(
                    OFFICIAL_HEADING,
                    _official_fallback_text(segment, pipeline),
                    notes,
                    tmp / f"t{n:03d}x.docx",
                )
            teacher_parts.append(ep)

        if skipped:
            log(f"  WARNING: {len(skipped)} 道题无法定位到原卷段落，已按纯文本导出（格式需手动调整）：{', '.join(skipped)}")
        if not student_parts:
            raise SystemExit("No selected item produced any content; nothing to export.")

        created: list[Path] = []
        for parts, name, title in (
            (student_parts, STUDENT, "高三英语精选试题（学生版）"),
            (teacher_parts, TEACHER, "高三英语精选试题（教师讲解版）"),
        ):
            target = docx_dir / name
            ds.merge(parts, target)
            ds.scrub_metadata(target, title)
            created.append(target)
            log(f"  wrote {target.name}  (images: {ds.media_count(target)})")

    answers = _answers_doc(rows, docx_dir / ANSWERS, pipeline)
    ds.scrub_metadata(answers, "高三英语答案汇总")
    log(f"  wrote {answers.name}")
    created.append(answers)

    for path in created:
        ds.validate(path)
    assert_student_edition_is_clean(docx_dir / STUDENT)

    return created
