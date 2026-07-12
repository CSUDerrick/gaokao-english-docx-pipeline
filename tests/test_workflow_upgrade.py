"""Regression tests for the 2026 workflow and export upgrade."""

from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from export_docx_splice import ANSWERS, STUDENT, TEACHER, TEMPLATE_DIR
import gaokao_english_docx_pipeline as pipeline
from gaokao_english_docx_pipeline import parse_args
from segment_quality import evaluate_document


def _row(folder: Path, section: str, chars: int = 300, question: str = "Normal exam text") -> dict:
    path = folder / f"{section}.json"
    path.write_text(json.dumps({"question_text": question}, ensure_ascii=False), encoding="utf-8")
    return {
        "source_doc": "paper.docx",
        "section": section,
        "display_section": section,
        "item_label": section,
        "char_count": chars,
        "segment_path": str(path),
    }


def test_worker_defaults_are_sixteen():
    args = parse_args(["input_docx"])
    assert args.segment_workers == 16
    assert args.score_workers == 16
    assert args.enrich_workers == 16
    assert args.segment_warning_fallback is True


def test_clean_and_tail_bleed_do_not_trigger_fallback():
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        sections = [
            "reading_a", "reading_b", "reading_c", "reading_d", "gap_filling",
            "cloze", "grammar", "practical_writing", "continuation_writing",
        ]
        clean = [_row(folder, section) for section in sections]
        result = evaluate_document("paper.docx", clean)
        assert result["grade"] == "PASS"
        assert result["sub_grade"] == "clean"
        assert result["needs_model_fallback"] is False

        clean[-1] = _row(folder, "continuation_writing", question="Writing prompt 参考答案")
        tail = evaluate_document("paper.docx", clean)
        assert tail["grade"] == "PASS"
        assert tail["sub_grade"] == "tail-bleed"
        assert tail["needs_model_fallback"] is False


def test_structural_warn_and_fail_trigger_fallback():
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        warn = [_row(folder, "reading_a", chars=50)]
        warn_result = evaluate_document("paper.docx", warn)
        assert warn_result["grade"] in {"WARN", "FAIL"}
        assert warn_result["needs_model_fallback"] is True
        fail_result = evaluate_document("paper.docx", [])
        assert fail_result["grade"] == "FAIL"
        assert fail_result["needs_model_fallback"] is True


def test_run_segment_replaces_only_abnormal_paper():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        input_dir = root / "input"
        out_dir = root / "out"
        input_dir.mkdir()
        out_dir.mkdir()
        for name in ("good.docx", "bad.docx"):
            (input_dir / name).write_bytes(b"placeholder")

        sections = [
            "reading_a", "reading_b", "reading_c", "reading_d", "gap_filling",
            "cloze", "grammar", "practical_writing", "continuation_writing",
        ]
        calls: list[tuple[str, str]] = []

        def fake_segment(docx: Path, args, target: Path) -> list[dict]:
            calls.append((docx.name, args.segment_input))
            chosen = sections
            if docx.name == "bad.docx" and args.segment_input == "local":
                chosen = [section for section in sections if section != "gap_filling"]
            rows = []
            segment_dir = target / "segments"
            segment_dir.mkdir(parents=True, exist_ok=True)
            for index, section in enumerate(chosen, 1):
                path = segment_dir / f"{docx.stem}-{args.segment_input}-{section}.json"
                path.write_text(json.dumps({"question_text": "Normal exam text"}), encoding="utf-8")
                rows.append({
                    "item_id": f"{docx.stem}-{section}-{index}",
                    "source_doc": docx.name,
                    "section": section,
                    "display_section": section,
                    "item_label": section,
                    "title": section,
                    "char_count": 300,
                    "answer_count": 1,
                    "confidence": .95,
                    "rough_unit": args.segment_input,
                    "segment_path": str(path),
                })
            return rows

        original = pipeline.segment_docx_file
        pipeline.segment_docx_file = fake_segment
        try:
            args = parse_args([str(input_dir), "--out", str(out_dir), "--mode", "segment"])
            args.api_key = "test-key"
            rows = pipeline.run_segment(args)
        finally:
            pipeline.segment_docx_file = original

        assert ("bad.docx", "rough") in calls
        assert ("good.docx", "rough") not in calls
        assert len(rows) == 18
        report = json.loads((out_dir / "segment_fallback_report.json").read_text(encoding="utf-8"))
        assert len(report) == 1 and report[0]["source_doc"] == "bad.docx"
        assert report[0]["final_grade"] == "PASS"


def test_run_segment_requires_key_when_fallback_is_needed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        input_dir = root / "input"
        out_dir = root / "out"
        input_dir.mkdir()
        out_dir.mkdir()
        (input_dir / "bad.docx").write_bytes(b"placeholder")

        def fake_warn(docx: Path, args, target: Path) -> list[dict]:
            segment_dir = target / "segments"
            segment_dir.mkdir(parents=True, exist_ok=True)
            path = segment_dir / "short.json"
            path.write_text(json.dumps({"question_text": "short"}), encoding="utf-8")
            return [{
                "item_id": "short", "source_doc": docx.name, "section": "reading_a",
                "display_section": "reading_a", "item_label": "reading_a", "title": "short",
                "char_count": 20, "answer_count": 0, "confidence": .2,
                "rough_unit": "local", "segment_path": str(path),
            }]

        original = pipeline.segment_docx_file
        pipeline.segment_docx_file = fake_warn
        try:
            args = parse_args([str(input_dir), "--out", str(out_dir), "--mode", "segment"])
            try:
                pipeline.run_segment(args)
                raise AssertionError("missing key must stop structural fallback")
            except SystemExit as exc:
                assert "requires an API key" in str(exc)
        finally:
            pipeline.segment_docx_file = original


def test_chinese_export_names_and_templates_exist():
    assert [STUDENT, TEACHER, ANSWERS] == [
        "高三英语精选试题_学生版.docx",
        "高三英语精选试题_教师讲解版.docx",
        "高三英语精选试题_答案汇总版.docx",
    ]
    # Only the answers sheet still needs a template — the student and teacher
    # versions inherit page setup from the source paper they are cloned from.
    assert (TEMPLATE_DIR / "answers_reference.docx").exists()


def test_directory_clear_is_scoped_and_independent():
    sys.path.insert(0, str(ROOT))
    from gui_app import safe_clear_directory, validate_clear_target

    try:
        validate_clear_target(ROOT)
        raise AssertionError("project root must be protected")
    except ValueError:
        pass
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        target = Path(td)
        (target / "nested").mkdir()
        (target / "nested" / "sample.txt").write_text("x", encoding="utf-8")
        removed = safe_clear_directory(target)
        assert removed >= 2
        assert target.exists()
        assert not list(target.iterdir())


def test_cached_enrichment_is_restored_without_overwriting_current_data():
    selected = [
        {"item_id": "a"},
        {"item_id": "b", "enrichment": {"teaching_notes": "current"}},
    ]
    cache = [
        {"item_id": "a", "enrichment": {"teaching_notes": "cached"}},
        {"item_id": "b", "enrichment": {"teaching_notes": "old"}},
    ]
    merged = pipeline.merge_cached_enrichments(selected, cache)
    assert merged == 1
    assert selected[0]["enrichment"]["teaching_notes"] == "cached"
    assert selected[1]["enrichment"]["teaching_notes"] == "current"
