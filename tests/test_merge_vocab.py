"""Merging several word-list handouts into one deduped handout, locally.

The inputs here are real .docx files written by the exporter itself, not fixtures with
hand-built XML: the merger has to survive the shape the pipeline actually produces, and
the one bug that made the feature useless (headers read three times over, so no table was
ever recognised) is invisible to anything that skips the round trip.
"""

from __future__ import annotations

import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docx_blocks as db  # noqa: E402
import docx_splice as ds  # noqa: E402
import export_vocab_docx as ev  # noqa: E402
import merge_vocab_docx as mv  # noqa: E402
from export_docx_splice import assert_student_edition_is_clean  # noqa: E402
from lxml import etree  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _handout(tmp: Path, name: str, reading: list[dict], forms: list[dict]) -> Path:
    return ev.build([{"reading_words": reading, "word_forms": forms}], tmp / name)


def _tables(path: Path) -> list:
    doc = etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    return list(doc.iter(W + "tbl"))


def _rows(path: Path, index: int) -> list[list[str]]:
    rows = []
    for tr in _tables(path)[index].iter(W + "tr"):
        rows.append([db.node_text(tc).strip() for tc in tr.iter(W + "tc")])
    return rows[1:]  # drop the header


# --- reading the pipeline's own output back


def test_a_handout_written_by_the_exporter_can_be_read_back():
    # The bug this pins: lxml's itertext() yields each run three times over on
    # python-docx's element classes, so 「英文单词」 came back as 「英文单词英文单词英文单词」,
    # no header matched, and every file was reported as 「没找到词汇表」.
    with tempfile.TemporaryDirectory() as tmp:
        path = _handout(
            Path(tmp), "a.docx",
            [{"word": "muse", "pos": "n.", "meaning": "灵感源泉"}],
            [{"base": "engrave", "base_pos": "v.", "derived": "engraving",
              "derived_pos": "n.", "note": "v. 变 n.，加后缀 -ing"}],
        )
        source = mv.read_handout(path)
        assert source.skipped == ""
        assert source.reading == [{"word": "muse", "pos": "n.", "meaning": "灵感源泉"}]
        assert source.forms[0]["derived"] == "engraving"


def test_a_docx_that_is_not_a_word_list_is_skipped_with_a_reason_not_guessed_at():
    # A 3-column table is not necessarily a word list. Guessing would put a row on a
    # student's sheet that no model ever chose, so an unknown header is skipped and said.
    with tempfile.TemporaryDirectory() as tmp:
        doc = ds.blank_template(ev.template_dir() / "student_reference.docx")
        table = doc.add_table(rows=2, cols=3)
        for col, value in enumerate(("姓名", "班级", "分数")):
            table.cell(0, col).text = value
        path = Path(tmp) / "roster.docx"
        doc.save(str(path))

        source = mv.read_handout(path)
        assert not source.used
        assert "没找到词汇表" in source.skipped


def test_a_file_that_cannot_be_opened_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "broken.docx"
        broken.write_text("this is not a zip", encoding="utf-8")
        source = mv.read_handout(broken)
        assert not source.used
        # Not python-docx's "Package not found at '<full temp path>'": the file name is
        # already on the line, so the reason has to be the part the teacher can act on.
        assert source.skipped == "不是有效的 Word 文件"


# --- the dedupe rules


def test_the_same_word_from_two_papers_becomes_one_row():
    reading = [{"word": "durable", "pos": "adj.", "meaning": "耐用的"}]
    other = [{"word": "Durable", "pos": "adj.", "meaning": "耐用的"}]
    merged, _ = mv.merge_entries([
        mv.Source(Path("a"), reading=reading),
        mv.Source(Path("b"), reading=other),
    ])
    assert merged == [{"word": "durable", "pos": "adj.", "meaning": "耐用的"}]


def test_one_word_with_two_parts_of_speech_keeps_both_senses_on_one_row():
    # 'yield' really is n. 产量 in one paper and v. 屈服 in another. Keying on (word, 词性)
    # would print it twice with nothing saying they are the same word.
    merged, _ = mv.merge_entries([
        mv.Source(Path("a"), reading=[{"word": "yield", "pos": "n.", "meaning": "产量（熟词生义）"}]),
        mv.Source(Path("b"), reading=[{"word": "yield", "pos": "v.", "meaning": "屈服，让步"}]),
    ])
    assert len(merged) == 1
    assert merged[0]["pos"] == "n./v."
    assert merged[0]["meaning"] == "产量（熟词生义）；屈服；让步"


def test_the_same_sense_written_three_ways_is_printed_once():
    # 「大量，丰富」/「丰富，充裕」/「充裕，大量」 is one word with three senses. Joining the
    # strings would print each sense up to three times.
    merged, _ = mv.merge_entries([
        mv.Source(Path("a"), reading=[{"word": "abundance", "pos": "n.", "meaning": "大量，丰富"}]),
        mv.Source(Path("b"), reading=[{"word": "abundance", "pos": "n.", "meaning": "丰富，充裕"}]),
        mv.Source(Path("c"), reading=[{"word": "abundance", "pos": "n.", "meaning": "充裕，大量"}]),
    ])
    assert merged[0]["meaning"] == "大量；丰富；充裕"


def test_the_annotated_wording_of_a_sense_wins():
    # The 熟词生义 note is the whole reason the word is on the sheet — it must not be the
    # copy that gets dropped.
    merged, _ = mv.merge_entries([
        mv.Source(Path("a"), reading=[{"word": "retreat", "pos": "n.", "meaning": "隐居处"}]),
        mv.Source(Path("b"), reading=[
            {"word": "retreat", "pos": "n.", "meaning": '隐居处（熟词生义：原义"撤退"）'}
        ]),
    ])
    assert merged[0]["meaning"] == '隐居处（熟词生义：原义"撤退"）'


def test_a_note_is_not_split_on_the_commas_inside_it():
    assert mv.split_senses("（尤指灾难或事件的）后果；余波") == ["（尤指灾难或事件的）后果", "余波"]


def test_a_more_specific_part_of_speech_absorbs_the_looser_one():
    assert mv.merge_pos(["n. phr.", "phr."]) == "n. phr."
    assert mv.merge_pos(["v./n.", "n."]) == "v./n."
    assert mv.merge_pos(["n.", "adj."]) == "n./adj."


def test_word_forms_are_deduped_on_the_pair_and_keep_one_explanation():
    # 「v.变n.，加后缀 -ment」 and 「v. 变 n.，加后缀 -ment」 are the same rule typed twice;
    # concatenating them would print the same explanation to the student twice over.
    _, forms = mv.merge_entries([
        mv.Source(Path("a"), forms=[{"base": "commit", "base_pos": "v.", "derived": "commitment",
                                     "derived_pos": "n.", "note": "v.变n.，加后缀 -ment"}]),
        mv.Source(Path("b"), forms=[{"base": "Commit", "base_pos": "v.", "derived": "Commitment",
                                     "derived_pos": "n.", "note": "v. 变 n.，加后缀 -ment"}]),
    ])
    assert len(forms) == 1
    assert forms[0]["note"] == "v. 变 n.，加后缀 -ment"


# --- the whole job


def test_two_handouts_merge_into_one_sorted_deduped_handout():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        first = _handout(
            tmp, "paper1.docx",
            [{"word": "zebra", "pos": "n.", "meaning": "斑马"},
             {"word": "muse", "pos": "n.", "meaning": "灵感源泉"}],
            [{"base": "engrave", "base_pos": "v.", "derived": "engraving",
              "derived_pos": "n.", "note": "加 -ing"}],
        )
        second = _handout(
            tmp, "paper2.docx",
            [{"word": "muse", "pos": "v.", "meaning": "沉思"},
             {"word": "apex", "pos": "n.", "meaning": "顶点"}],
            [{"base": "engrave", "base_pos": "v.", "derived": "engraving",
              "derived_pos": "n.", "note": "v. 变 n.，加后缀 -ing"},
             {"base": "rely", "base_pos": "v.", "derived": "reliable",
              "derived_pos": "adj.", "note": "y 变 i 加 -able"}],
        )

        out = tmp / "merged.docx"
        report = mv.merge_handouts([first, second], out, log=lambda _line: None)

        assert report.reading_before == 4 and len(report.reading) == 3
        assert report.forms_before == 3 and len(report.forms) == 2
        # Alphabetical: a term's worth of words is looked up, not read in paper order.
        assert [row[0] for row in _rows(out, 0)] == ["apex", "muse", "zebra"]
        assert [row[0] for row in _rows(out, 1)] == ["engrave", "rely"]
        assert _rows(out, 0)[1][1] == "n./v.", "muse is n. in one paper and v. in the other"


def test_the_merged_handout_passes_the_same_gates_as_the_exported_one():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = _handout(
            tmp, "paper.docx",
            [{"word": "muse", "pos": "n.", "meaning": "灵感源泉"}],
            [{"base": "rely", "base_pos": "v.", "derived": "reliable",
              "derived_pos": "adj.", "note": "y 变 i 加 -able"}],
        )
        out = tmp / "merged.docx"
        mv.merge_handouts([source], out, log=lambda _line: None)
        ds.validate(out)  # merge_handouts already calls it; a second call must still pass
        assert_student_edition_is_clean(out)
        assert len(_tables(out)) == 2, "the two tables, and no template demo table"


def test_merging_a_merged_handout_again_changes_nothing():
    # The teacher will do this — 「上次合并的那份，再把这周的加进去」. If a second pass moved
    # rows or re-split a merged 释义, the sheet would churn for no reason.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        first = _handout(tmp, "a.docx",
                         [{"word": "yield", "pos": "n.", "meaning": "产量"}], [])
        second = _handout(tmp, "b.docx",
                          [{"word": "yield", "pos": "v.", "meaning": "屈服"}], [])
        once = mv.merge_handouts([first, second], tmp / "m1.docx", log=lambda _l: None)
        twice = mv.merge_handouts([tmp / "m1.docx"], tmp / "m2.docx", log=lambda _l: None)
        assert once.reading == twice.reading and once.forms == twice.forms


def test_a_folder_is_expanded_and_the_output_is_never_read_back_in():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _handout(tmp, "a.docx", [{"word": "muse", "pos": "n.", "meaning": "灵感"}], [])
        _handout(tmp, "b.docx", [{"word": "apex", "pos": "n.", "meaning": "顶点"}], [])
        (tmp / "~$a.docx").write_text("word lock file", encoding="utf-8")
        out = tmp / "merged.docx"
        out.write_text("a stale previous merge", encoding="utf-8")

        found = mv.handouts_in([tmp], exclude=out)
        assert [p.name for p in found] == ["a.docx", "b.docx"], found

        # And end to end: a stale output in the same folder must not break the run.
        report = mv.merge_handouts([tmp], out, log=lambda _l: None)
        assert len(report.reading) == 2


def test_a_folder_with_nothing_usable_says_so_instead_of_writing_an_empty_handout():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "notes.docx").write_text("not a docx at all", encoding="utf-8")
        out = tmp / "merged.docx"
        try:
            mv.merge_handouts([tmp], out, log=lambda _l: None)
        except RuntimeError as exc:
            assert "没有认得出来的词汇表" in str(exc)
        else:
            raise AssertionError("an empty merge must fail, not ship a blank sheet")
        assert not out.exists()


def test_no_input_at_all_is_an_error_the_ui_can_show():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            mv.merge_handouts([], Path(tmp) / "m.docx", log=lambda _l: None)
        except RuntimeError as exc:
            assert "没有找到" in str(exc)
        else:
            raise AssertionError("merging nothing must fail")


# --- the dialog. The merge runs on a worker thread, and the first version reported its
# --- result from a plain closure, which Qt runs in the *emitting* thread — so every
# --- widget update happened off the UI thread. That is a crash waiting for a slow disk,
# --- and nothing in the pure-logic tests above can see it.


def test_the_dialog_merges_what_was_dropped_on_it_and_reports_it_on_the_ui_thread():
    import os
    import threading

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT / "app"))
    from PySide6.QtWidgets import QApplication

    import main as gui

    app = QApplication.instance() or QApplication([])
    ui_thread = threading.current_thread()
    seen: list = []

    class Watched(gui.MergeVocabDialog):
        """Records which thread the result arrived on.

        A subclass, not a monkeypatched attribute: assigning a plain function over
        `_merge_finished` would make the connection a functor again — i.e. it would
        recreate the very bug this test exists to catch, and then pass.
        """

        def _merge_finished(self, ok: bool, message: str) -> None:
            seen.append(threading.current_thread())
            super()._merge_finished(ok, message)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _handout(tmp, "a.docx", [{"word": "muse", "pos": "n.", "meaning": "灵感源泉"}], [])
        _handout(tmp, "b.docx", [{"word": "muse", "pos": "v.", "meaning": "沉思"}], [])
        (tmp / "notes.docx").write_text("not a docx", encoding="utf-8")

        out = tmp / "merged.docx"
        dialog = Watched(None, out, tmp)
        # A folder drop: the same call dropEvent makes.
        assert dialog.files.add([tmp]) == 3, "the junk file is listed, then skipped by name"
        assert dialog.merge_btn.isEnabled()

        dialog.merge()

        deadline = time.time() + 30
        while time.time() < deadline and not seen:
            app.processEvents()
            time.sleep(0.01)

        assert seen, "the merge never reported back"
        assert seen[0] is ui_thread, "the result was delivered on the worker thread"
        assert dialog.merged == out and out.exists()
        assert "不是有效的 Word 文件" in dialog.report.toPlainText()
        assert "阅读词汇 2 → 1" in dialog.report.toPlainText()

        dialog.files.clear_all()
        assert not dialog.merge_btn.isEnabled(), "nothing selected, nothing to merge"
