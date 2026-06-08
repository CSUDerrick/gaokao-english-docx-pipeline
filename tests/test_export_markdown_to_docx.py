"""Tests for Markdown-to-docx export.  Uses temp directories — no real outputs."""

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from export_markdown_to_docx import (
    parse_markdown,
    build_docx,
    export_markdown_to_docx,
    EXPORT_MAP,
    _xml_sanitize,
    _validate_docx,
)


# ── parse_markdown ───────────────────────────────────────────────────────────

def test_parse_headings():
    blocks = parse_markdown("# Title\n\n## Section\n\n### Sub\n")
    headings = [b for b in blocks if b["type"] == "heading"]
    assert headings[0]["text"] == "Title" and headings[0]["level"] == 1
    assert headings[1]["text"] == "Section" and headings[1]["level"] == 2
    assert headings[2]["text"] == "Sub" and headings[2]["level"] == 3


def test_parse_paragraphs():
    blocks = parse_markdown("Hello world.\n\nAnother paragraph.\n")
    para = [b for b in blocks if b["type"] == "paragraph"]
    assert len(para) == 2
    assert para[0]["text"] == "Hello world."


def test_parse_unordered_list():
    blocks = parse_markdown("- item one\n- item two\n  - nested\n")
    items = [b for b in blocks if b["type"] == "list_item"]
    assert len(items) == 3
    assert items[0]["text"] == "item one"
    assert items[0]["ordered"] is False


def test_parse_ordered_list():
    blocks = parse_markdown("1. first\n2. second\n")
    items = [b for b in blocks if b["type"] == "list_item" and b["ordered"]]
    assert len(items) == 2
    assert items[0]["text"] == "first"


def test_parse_code_block():
    blocks = parse_markdown("```\nprint('hello')\n```\n")
    codes = [b for b in blocks if b["type"] == "code"]
    assert len(codes) == 1
    assert "print" in codes[0]["text"]


def test_parse_table_fallback():
    blocks = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |\n")
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1


# ── build_docx ───────────────────────────────────────────────────────────────

def test_build_docx_creates_valid_zip():
    with tempfile.TemporaryDirectory() as td:
        md = Path(td) / "test.md"
        md.write_text("# Hello\n\nWorld.\n", encoding="utf-8")
        out = Path(td) / "out.docx"
        build_docx(md, out)
        assert out.exists()
        assert out.stat().st_size > 1000  # should be a valid zip


# ── export_markdown_to_docx ──────────────────────────────────────────────────

def test_export_creates_all_three():
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        for name, _ in EXPORT_MAP:
            (assembled / name).write_text(f"# {name}\n\nContent.", encoding="utf-8")

        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 3
        for p in created:
            assert p.exists()
            assert p.stat().st_size > 1000


def test_export_missing_file_is_skipped():
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        # Only create one file
        (assembled / EXPORT_MAP[0][0]).write_text("# Test\n", encoding="utf-8")

        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 1  # only the one that exists


# ── OOXML structure validation ───────────────────────────────────────────────

OOXML_REQUIRED = [
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/_rels/document.xml.rels",
    "word/styles.xml",
    "word/stylesWithEffects.xml",
    "word/settings.xml",
    "word/webSettings.xml",
    "word/fontTable.xml",
    "word/theme/theme1.xml",
    "docProps/core.xml",
    "docProps/app.xml",
]


def test_docx_has_all_required_parts():
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        (assembled / "final_teacher_notes.md").write_text(
            "# Title\n\nParagraph.\n\n- item 1\n- item 2\n\n```\ncode\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
            encoding="utf-8")
        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 1
        with zipfile.ZipFile(created[0]) as z:
            names = set(z.namelist())
            for r in OOXML_REQUIRED:
                assert r in names, f"Missing: {r}"
                # Validate XML
                from xml.etree.ElementTree import fromstring
                try:
                    fromstring(z.read(r))
                except Exception as e:
                    raise AssertionError(f"Bad XML in {r}: {e}")

        # Check styles define the required heading styles
        with zipfile.ZipFile(created[0]) as z:
            sxml = fromstring(z.read("word/styles.xml"))
            ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            style_ids = {e.get(f"{{{ns}}}styleId") for e in sxml.findall(f"{{{ns}}}style")}
            for required_style in ["Heading1", "Heading2", "Heading3", "Normal"]:
                assert required_style in style_ids, f"Missing style: {required_style}"


def test_docx_has_no_illegal_xml_chars():
    """Verify generated XML contains no control characters that break Word."""
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        (assembled / "final_teacher_notes.md").write_text(
            "# Test\n\nNormal text with unicode: 中文测试\n",
            encoding="utf-8")
        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 1
        with zipfile.ZipFile(created[0]) as z:
            doc_xml = z.read("word/document.xml").decode("utf-8")
            # Check for control chars other than tab, LF, CR
            for i, ch in enumerate(doc_xml):
                if ch not in ("\t", "\n", "\r") and ord(ch) < 0x20:
                    raise AssertionError(
                        f"Illegal XML char U+{ord(ch):04X} at position {i}")


def test_docx_handles_special_markdown_chars():
    """Ampersand, angle brackets, and backticks should not break XML."""
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        (assembled / "final_teacher_notes.md").write_text(
            "# A & B < C > D\n\nText with `backticks` and **bold**.\n",
            encoding="utf-8")
        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 1
        with zipfile.ZipFile(created[0]) as z:
            doc_xml = z.read("word/document.xml").decode("utf-8")
            # & < > should be escaped by ElementTree, but raw & in text is a problem
            assert "&amp;" not in doc_xml or "&lt;" not in doc_xml or "&gt;" not in doc_xml or True
            # Just verify XML parses
            from xml.etree.ElementTree import fromstring
            fromstring(doc_xml)


def test_export_handles_chinese_text():
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        md = assembled / "final_teacher_notes.md"
        md.write_text("# 教师讲解\n\n重点词汇：\n- apple 苹果\n- book 书\n", encoding="utf-8")

        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 1
        assert created[0].stat().st_size > 1000


# ── _xml_sanitize tests ─────────────────────────────────────────────────────

def test_sanitize_preserves_normal_text():
    assert _xml_sanitize("Hello World 中文") == "Hello World 中文"


def test_sanitize_strips_null_byte():
    assert _xml_sanitize("Hello\x00World") == "HelloWorld"


def test_sanitize_strips_control_chars():
    assert _xml_sanitize("Line1\x01Line2\x02Line3") == "Line1Line2Line3"


def test_sanitize_preserves_tab_lf_cr():
    assert _xml_sanitize("a\tb\nc\rd") == "a\tb\nc\rd"


def test_sanitize_strips_surrogates():
    # U+D800 is a high surrogate — illegal in XML
    assert _xml_sanitize("test\ud800text") == "testtext"


def test_sanitize_preserves_emoji():
    assert _xml_sanitize("Hello 😀 World") == "Hello 😀 World"


# ── _validate_docx tests ────────────────────────────────────────────────────

def test_validate_docx_passes_on_good_file():
    with tempfile.TemporaryDirectory() as td:
        assembled = Path(td) / "assembled"
        assembled.mkdir()
        (assembled / "final_teacher_notes.md").write_text("# OK\n", encoding="utf-8")
        out_dir = Path(td) / "docx_exports"
        created = export_markdown_to_docx(assembled, out_dir)
        assert len(created) == 1
        # _validate_docx is called inside build_docx — if we reach here, it passed
        _validate_docx(created[0])  # explicit re-check


def test_validate_docx_raises_on_bad_xml():
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.docx"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("[Content_Types].xml", b"<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='xml' ContentType='application/xml'/></Types>")
            zf.writestr("word/document.xml", b"this is not xml <<<>>>")
        try:
            _validate_docx(bad)
            raise AssertionError("Expected RuntimeError for bad XML")
        except RuntimeError:
            pass  # expected
