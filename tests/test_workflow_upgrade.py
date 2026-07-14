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


def test_answer_bleed_is_structural_not_cosmetic():
    """An answer inside a question body must stop the run, not be waved through.

    The writing sections used to be *exempted* from the leak check, on the theory
    that a bit of trailing answer text was only cosmetic. It was not: that
    exemption is how model essays and 【21题详解】 blocks reached the student paper.
    """
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

        for leak in ("Writing prompt 参考答案", "Writing prompt 参考范文", "Writing prompt 【32题详解】", "21—23. CBC"):
            bled = list(clean)
            bled[-1] = _row(folder, "continuation_writing", question=leak)
            graded = evaluate_document("paper.docx", bled)
            assert graded["grade"] == "WARN", leak
            assert graded["sub_grade"] == "structural", leak
            assert graded["needs_model_fallback"] is True, leak

        # A layout hint is still merely cosmetic — it is not an answer.
        cosmetic = list(clean)
        cosmetic[-1] = _row(folder, "continuation_writing", question="Writing prompt 请在答题卡上作答")
        tail = evaluate_document("paper.docx", cosmetic)
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

        # The repair step asks the model for a boundary; stub it so the test stays
        # about *which* papers get repaired, not about the model call itself.
        repaired: list[str] = []

        def fake_locate(doc, segments, missing, ask, log=print):
            repaired.append(missing[0] if missing else "")
            return [(0, "gap_filling", "七选五")]

        def fake_segment_with_extra(docx: Path, args, target: Path, extra_starts=None):
            if extra_starts:
                calls.append((docx.name, "repair"))
                rows = fake_segment(docx, args, target)
                # the recovered section is now present
                rows.append({
                    "item_id": f"{docx.stem}-gap_filling-99", "source_doc": docx.name,
                    "section": "gap_filling", "display_section": "七选五", "item_label": "七选五",
                    "title": "七选五", "char_count": 300, "answer_count": 5, "confidence": .9,
                    "rough_unit": "local", "segment_path": str(target / "segments" / f"{docx.stem}-gap.json"),
                })
                (target / "segments").mkdir(parents=True, exist_ok=True)
                (target / "segments" / f"{docx.stem}-gap.json").write_text("{}", encoding="utf-8")
                return rows
            return fake_segment(docx, args, target)

        original = pipeline.segment_docx_file
        original_locate = pipeline.locate_missing_sections
        original_read = pipeline.read_docx
        pipeline.segment_docx_file = fake_segment_with_extra
        pipeline.locate_missing_sections = fake_locate
        pipeline.read_docx = lambda p: object()  # repair only needs it to hand to locate
        try:
            args = parse_args([str(input_dir), "--out", str(out_dir), "--mode", "segment"])
            args.api_key = "test-key"
            rows = pipeline.run_segment(args)
        finally:
            pipeline.segment_docx_file = original
            pipeline.locate_missing_sections = original_locate
            pipeline.read_docx = original_read

        assert ("bad.docx", "repair") in calls, "the mis-split paper must be repaired"
        assert ("good.docx", "repair") not in calls, "a healthy paper must not be touched"
        assert repaired == ["gap_filling"], "only the section that went missing is asked about"

        report = json.loads((out_dir / "segment_fallback_report.json").read_text(encoding="utf-8"))
        assert len(report) == 1 and report[0]["source_doc"] == "bad.docx"
        assert report[0]["fallback_mode"] == "boundary"
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


def test_cached_explanation_is_restored_without_overwriting_current_data():
    # selected_items.json is rewritten by several stages, so a row can come back
    # without the explanation the export needs. The cache refills those, but must
    # never clobber an explanation that is already there.
    selected = [
        {"item_id": "a"},
        {"item_id": "b", "explanation": {"questions": [{"number": "24"}]}},
    ]
    cache = [
        {"item_id": "a", "explanation": {"questions": [{"number": "21"}]}, "has_official_explanation": True},
        {"item_id": "b", "explanation": {"questions": [{"number": "99"}]}, "has_official_explanation": False},
    ]
    merged = pipeline.merge_cached_explanations(selected, cache)
    assert merged == 1
    assert selected[0]["explanation"]["questions"][0]["number"] == "21"
    assert selected[0]["has_official_explanation"] is True
    assert selected[1]["explanation"]["questions"][0]["number"] == "24", "current data wins"
