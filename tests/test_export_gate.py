"""Changing the selection must not silently ship a broken set of documents.

Both stages that pick questions (`select`, `review-select`) overwrite
`selected_items.json` with plain score rows. Before this gate existed, swapping one
question in and re-exporting produced a teacher edition with that question's
「详细解析和解答步骤」 section empty — no explanation had ever been generated for it, and
nothing warned. It looks fine until someone reads it, which is the worst kind of wrong.

**The vocabulary half of this gate changed shape.** Word lists used to be extracted per
selected question, so swapping a question left the handout stale and the gate had to
catch it. Vocabulary is now extracted per *paper*, from the paper's whole text — so
re-picking questions inside a paper we have already read cannot stale anything, and the
gate is about papers instead. What it still has to catch is a paper entering the
selection that has never been read.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gaokao_english_docx_pipeline as pipeline  # noqa: E402


def _run(
    tmp: Path,
    *,
    selected: list[tuple[str, str]],       # (item_id, source_doc)
    explained: list[str],
    vocab: list[str] | None = None,        # 困难（整卷）: one row per source_doc
    vocab_items: list[str] | None = None,  # 完整（分块）: one row per item_id
    stamp_mode: bool = True,
) -> Path:
    out = tmp / "run"
    (out / "explanations").mkdir(parents=True)
    (out / "selected_items.json").write_text(
        json.dumps(
            [{"item_id": i, "source_doc": doc, "section": "reading_a"} for i, doc in selected],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for item_id in explained:
        (out / "explanations" / f"{pipeline.safe_filename(item_id)}.json").write_text("{}", encoding="utf-8")

    rows: list[dict] | None = None
    if vocab_items is not None:
        rows = [{"item_id": i, "source_doc": "p1.docx"} for i in vocab_items]
        if stamp_mode:
            for row in rows:
                row["vocab_mode"] = pipeline.VOCAB_CHUNKED
    elif vocab is not None:
        rows = [{"source_doc": doc} for doc in vocab]
        if stamp_mode:
            for row in rows:
                row["vocab_mode"] = pipeline.VOCAB_WHOLE
    if rows is not None:
        (out / "vocab_index.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return out


def _refused(out: Path) -> str:
    try:
        pipeline.assert_selection_is_complete(out)
    except SystemExit as exc:
        return str(exc)
    raise AssertionError("the export should have been refused")


def test_a_complete_selection_exports():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("b", "p1.docx")],
            explained=["a", "b"],
            vocab=["p1.docx"],
        )
        pipeline.assert_selection_is_complete(out)  # must not raise


def test_a_newly_selected_question_with_no_explanation_stops_the_export():
    with tempfile.TemporaryDirectory() as tmp:
        # "b" was just swapped in by 重新选题 and has never been explained.
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("b", "p1.docx")],
            explained=["a"],
            vocab=["p1.docx"],
        )
        message = _refused(out)
        assert "b" in message
        assert "--mode explain" in message, "say which stage fixes it"


def test_swapping_a_question_within_a_paper_no_longer_stales_a_whole_paper_handout():
    """困难（整卷）: the whole point of reading the paper rather than the question.

    Under 完整（分块） this has to be refused — the word list is the previous selection's.
    Under 困难（整卷） the list belongs to the paper, and the paper has not changed, so
    重新选题 costs nothing in vocabulary and the teacher is not made to pay for a re-run
    that would produce byte-identical output.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("brand-new", "p1.docx")],
            explained=["a", "brand-new"],
            vocab=["p1.docx"],  # extracted before "brand-new" was ever picked
        )
        pipeline.assert_selection_is_complete(out)  # must not raise


def test_swapping_a_question_DOES_stale_a_chunked_handout():
    """完整（分块）: the words are per-question, so a new question has no words at all.

    The same swap the whole-paper mode shrugs off has to be caught here — this is exactly
    why the gate asks the list which mode built it instead of assuming.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("brand-new", "p1.docx")],
            explained=["a", "brand-new"],
            vocab_items=["a"],  # "brand-new" was swapped in afterwards
        )
        message = _refused(out)
        assert "--mode vocab" in message
        assert "旧词表" in message


def test_a_paper_with_no_word_list_at_all_stops_the_export():
    """困难（整卷）: a paper contributing questions but no vocabulary is a handout with a hole."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("b", "p2.docx")],
            explained=["a", "b"],
            vocab=["p1.docx"],  # p2 was never read
        )
        message = _refused(out)
        assert "p2.docx" in message
        assert "--mode vocab" in message, "say which stage fixes it"


def test_the_gate_reads_the_mode_off_the_list_not_off_the_command_line():
    """A teacher can flip 完整/困难 and export without re-running vocab.

    If the gate consulted the current setting it would check 分块's rules against an 整卷
    list and refuse a handout that is perfectly good — or worse, wave through a chunked
    list that is genuinely missing a question. The list carries its own mode.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # A chunked list that covers every selected question is fine, whatever the
        # teacher has the toggle set to right now.
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("b", "p1.docx")],
            explained=["a", "b"],
            vocab_items=["a", "b"],
        )
        pipeline.assert_selection_is_complete(out)


def test_a_word_list_from_before_the_switch_is_read_by_its_shape():
    """Rows written before vocab_mode existed carry no mode at all.

    Guessing one default for all of them would misread half the lists already on disk, so
    the shape decides: 完整 is keyed by question and has an item_id, 困难 is keyed by paper
    and does not.
    """
    assert pipeline.vocab_row_mode({"item_id": "a", "source_doc": "p1"}) == pipeline.VOCAB_CHUNKED
    assert pipeline.vocab_row_mode({"source_doc": "p1"}) == pipeline.VOCAB_WHOLE
    # An explicit stamp always wins over the guess.
    assert pipeline.vocab_row_mode({"vocab_mode": "whole", "item_id": "a"}) == pipeline.VOCAB_WHOLE

    with tempfile.TemporaryDirectory() as tmp:
        # An un-stamped per-question list, missing a swapped-in question, must still be caught.
        out = _run(
            Path(tmp),
            selected=[("a", "p1.docx"), ("brand-new", "p1.docx")],
            explained=["a", "brand-new"],
            vocab_items=["a"],
            stamp_mode=False,
        )
        assert "旧词表" in _refused(out)


def test_a_word_list_half_in_each_mode_is_refused():
    """A run that was interrupted and restarted under the other toggle."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(Path(tmp), selected=[("a", "p1.docx")], explained=["a"])
        (out / "vocab_index.json").write_text(json.dumps([
            {"vocab_mode": "chunked", "item_id": "a", "source_doc": "p1.docx"},
            {"vocab_mode": "whole", "source_doc": "p1.docx"},
        ]), encoding="utf-8")
        message = _refused(out)
        assert "两种模式" in message
        assert "--force" in message, "tell them how to fix it"


def test_a_run_that_never_made_a_handout_still_exports():
    # The vocabulary stage is optional — export_vocab skips it with a note. Only an
    # *incomplete* index is a problem; a missing one is not.
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(Path(tmp), selected=[("a", "p1.docx")], explained=["a"], vocab=None)
        pipeline.assert_selection_is_complete(out)


def test_an_empty_selection_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(Path(tmp), selected=[], explained=[], vocab=None)
        assert "select" in _refused(out)
