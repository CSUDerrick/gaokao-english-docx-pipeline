"""Tests for Markdown-to-docx export.  Uses temp directories — no real outputs."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from export_markdown_to_docx import (
    parse_markdown,
    build_docx,
    export_markdown_to_docx,
    EXPORT_MAP,
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
