"""The student vocabulary handout: two tables, deduped, and safe to hand out.

The words come from the `vocab` stage, which sees only question text — a handout
that leaked an answer or a paper name would be worse than no handout at all.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docx_blocks as db  # noqa: E402
import docx_splice as ds  # noqa: E402
import export_vocab_docx as ev  # noqa: E402
from lxml import etree  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

ROWS = [
    {
        "item_id": "a",
        "reading_words": [
            {"word": "recurrent", "pos": "adj.", "meaning": "反复出现的；周期性的"},
            {"word": "muse", "pos": "n.", "meaning": "灵感源泉"},
        ],
        "word_forms": [
            {"base": "engrave", "base_pos": "v.", "derived": "engraving",
             "derived_pos": "n.", "note": "v. 变 n.，加后缀 -ing"},
        ],
    },
    {
        "item_id": "b",
        "reading_words": [
            {"word": "Recurrent", "pos": "adj.", "meaning": "同一个词，大小写不同"},
            {"word": "durable", "pos": "adj.", "meaning": "持久的；耐用的"},
        ],
        "word_forms": [
            {"base": "engrave", "base_pos": "v.", "derived": "engraving",
             "derived_pos": "n.", "note": "重复项"},
        ],
    },
]


def _tables(path: Path) -> list:
    doc = etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    return list(doc.iter(W + "tbl"))


def test_words_and_forms_are_deduped_across_items():
    words = ev.collect_words(ROWS)
    assert [w[0] for w in words] == ["recurrent", "muse", "durable"], "case-insensitive dedupe, first wins"
    assert ev.collect_words(ROWS)[0][2] == "反复出现的；周期性的"

    forms = ev.collect_forms(ROWS)
    assert len(forms) == 1, "the same base->derived pair must appear once"
    assert forms[0][:4] == ("engrave", "v.", "engraving", "n.")


def test_handout_has_exactly_the_two_tables_with_the_asked_for_headers():
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")  # build() calls ds.validate()

        tables = _tables(out)
        assert len(tables) == 2, "the template's own demo table must not ride along"

        def header(table):
            row = list(table.iter(W + "tr"))[0]
            return tuple("".join(c.itertext()).strip() for c in row.iter(W + "tc"))

        assert header(tables[0]) == ev.WORDS_HEADER
        assert header(tables[1]) == ev.FORMS_HEADER
        # header row + one row per entry
        assert len(list(tables[0].iter(W + "tr"))) == 1 + 3
        assert len(list(tables[1].iter(W + "tr"))) == 1 + 1


def test_handout_draws_borders_without_naming_a_style():
    # Referencing a style styles.xml never declares is a known cause of Word's
    # "unreadable content" prompt, so the grid is written as explicit borders.
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")
        for table in _tables(out):
            assert table.find(W + "tblPr/" + W + "tblBorders") is not None
            assert table.find(W + "tblPr/" + W + "tblStyle") is None


def test_handout_carries_no_answers_and_no_source():
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")
        text = db.read_docx(out).text
        assert "来源" not in text
        assert "**" not in text
        assert ds.orphan_range_markers(
            etree.fromstring(zipfile.ZipFile(out).read("word/document.xml"))
        ) == []


# --- print formatting. The teacher prints this one and marks it up by hand, so the
# --- typography is part of the deliverable, not decoration. Every value below is
# --- read off the handout she already uses (0713高三英语精选试题_重难点词汇表_学生版.docx).


def _styles(path: Path) -> dict:
    root = etree.fromstring(zipfile.ZipFile(path).read("word/styles.xml"))
    found = {}
    for style in root.iter(W + "style"):
        name = style.find(W + "name")
        if name is not None:
            found[name.get(W + "val")] = style
    return found


def test_handout_is_set_in_times_new_roman_and_songti_at_the_asked_for_sizes():
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")
        styles = _styles(out)

        for name, half_points in ((ds.VOCAB_TITLE, "32"), (ds.VOCAB_BODY, "24")):
            rpr = styles[name].find(W + "rPr")
            fonts = rpr.find(W + "rFonts")
            assert fonts.get(W + "ascii") == "Times New Roman"
            assert fonts.get(W + "hAnsi") == "Times New Roman"
            assert fonts.get(W + "eastAsia") == "宋体"
            # 三号 = 16pt = 32 half-points; 小四 = 12pt = 24.
            assert rpr.find(W + "sz").get(W + "val") == half_points

        title = styles[ds.VOCAB_TITLE]
        assert title.find(W + "rPr/" + W + "b") is not None, "标题加粗"
        assert title.find(W + "pPr/" + W + "jc").get(W + "val") == "center", "标题居中"


def test_paragraph_spacing_is_zero_and_the_two_word_checkboxes_are_off():
    # 段前/段后 0 行; 「在相同样式的段落间不添加空格」 and 「定义文档网格时对齐网格」 both
    # unchecked. The first two are values; the third is an *absence* — an emitted
    # w:contextualSpacing would tick the box no matter what else we wrote.
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")
        styles = _styles(out)

        for name in (ds.VOCAB_TITLE, ds.VOCAB_BODY):
            ppr = styles[name].find(W + "pPr")
            spacing = ppr.find(W + "spacing")
            assert spacing.get(W + "before") == "0"
            assert spacing.get(W + "after") == "0"
            assert ppr.find(W + "snapToGrid").get(W + "val") == "0"
            assert ppr.find(W + "contextualSpacing") is None

        assert b"contextualSpacing" not in zipfile.ZipFile(out).read("word/document.xml")


def test_the_two_lists_do_not_share_a_page():
    # The teacher's own handout does this with 17 empty paragraphs, which only lands
    # right for the exact word count it was built with.
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")
        body = etree.fromstring(zipfile.ZipFile(out).read("word/document.xml")).find(W + "body")
        paragraphs = [c for c in body if c.tag == W + "p"]

        titles = [p for p in paragraphs if "语法词汇变形" in "".join(p.itertext())]
        assert len(titles) == 1
        assert titles[0].find(W + "pPr/" + W + "pageBreakBefore") is not None


def test_the_table_fits_the_window_and_the_handout_has_no_header():
    with tempfile.TemporaryDirectory() as tmp:
        out = ev.build(ROWS, Path(tmp) / "v.docx")
        for table in _tables(out):
            width = table.find(W + "tblPr/" + W + "tblW")
            assert width.get(W + "type") == "pct", "「根据窗口自动调整」"
            assert width.get(W + "w") == "5000", "full text column"

        body = etree.fromstring(zipfile.ZipFile(out).read("word/document.xml")).find(W + "body")
        sect_pr = body.find(W + "sectPr")
        assert sect_pr.findall(W + "headerReference") == [], "打印稿不要页眉"
        assert sect_pr.find(W + "pgMar").get(W + "top") == "720"
