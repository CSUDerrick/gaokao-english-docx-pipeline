"""Fidelity tests for the clone-and-splice export.

The bug these pin down: the old export read the source .docx into a flat string,
so every bold run, underline (which is how a blank is drawn on an English paper),
table and image was gone before segmentation even started — the exported student
paper contained zero images and one 10,000-character paragraph.

These tests build their own .docx fixtures rather than reading ``input_docx/``,
which is gitignored, so they run on a fresh clone.
"""

from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docx_blocks as db  # noqa: E402
import docx_splice as ds  # noqa: E402
from docx import Document  # noqa: E402
from docx.shared import Inches  # noqa: E402
from lxml import etree  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _png() -> io.BytesIO:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 60, 60)).save(buf, format="PNG")
    buf.seek(0)
    return buf


def _fixture(path: Path, marker: str = "X") -> Path:
    """A paper-ish docx: styled runs, a blank-line underline, a table, an image."""
    doc = Document()
    doc.add_paragraph(f"{marker} 阅读理解 Passage")

    p = doc.add_paragraph()
    p.add_run("The word ").bold = False
    p.add_run("crucial").bold = True
    p.add_run(" means ")
    p.add_run("        ").underline = True  # a fill-in blank
    p.add_run(" here.")

    p2 = doc.add_paragraph()
    p2.add_run("Emphasis").italic = True

    doc.add_paragraph("21. What does the author mean?  ____21____")

    t = doc.add_table(rows=2, cols=2)
    t.style = "Table Grid"
    t.cell(0, 0).text = "A"
    t.cell(0, 1).text = "B"
    t.cell(1, 0).text = "C"
    t.cell(1, 1).text = "D"

    doc.add_paragraph("Figure:")
    doc.add_picture(_png(), width=Inches(1))
    doc.add_paragraph("Tail paragraph")
    doc.save(str(path))
    return path


def _count(path: Path, tag: str) -> int:
    with zipfile.ZipFile(path) as z:
        dx = etree.fromstring(z.read("word/document.xml"))
    return len(dx.findall(".//" + W + tag))


def test_flat_text_drops_empty_paragraphs_and_joins_table_rows():
    with tempfile.TemporaryDirectory() as tmp:
        src = _fixture(Path(tmp) / "a.docx")
        doc = db.read_docx(src)
        assert "阅读理解 Passage" in doc.text
        assert "A | B" in doc.text and "C | D" in doc.text
        # every block's recorded span must actually index its own text
        for b in doc.blocks:
            assert doc.text[b.char_start : b.char_end] == b.text


def test_clone_preserves_bold_underline_table_and_image():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")
        doc = db.read_docx(src)

        out = ds.clone_subset(src, list(range(len(doc.body_children))), tmp / "clone.docx")

        for tag in ("b", "u", "i", "tbl", "drawing"):
            assert _count(out, tag) == _count(src, tag), f"<w:{tag}> not preserved"
        assert ds.media_count(out) == ds.media_count(src) == 1
        assert ds.unresolved_rids(out) == []


def test_clone_subset_keeps_only_requested_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")
        doc = db.read_docx(src)
        first = [b for b in doc.blocks if "Passage" in b.text][0]

        out = ds.clone_subset(src, [first.body_index], tmp / "one.docx")
        text = db.read_docx(out).text
        assert "Passage" in text
        assert "Tail paragraph" not in text


def test_clone_preserves_root_namespaces_and_mc_ignorable():
    # Re-serializing the root with ElementTree drops namespace declarations that
    # mc:Ignorable still names, which is what made Word report "unreadable
    # content". Every prefix listed in mc:Ignorable must stay declared.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")
        doc = db.read_docx(src)
        out = ds.clone_subset(src, list(range(len(doc.body_children))), tmp / "c.docx")

        with zipfile.ZipFile(out) as z:
            root = etree.fromstring(z.read("word/document.xml"))
        ignorable = (root.get("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable") or "").split()
        dangling = [p for p in ignorable if p not in root.nsmap]
        assert dangling == [], f"mc:Ignorable names undeclared prefixes: {dangling}"


def test_merge_across_documents_resolves_id_collisions():
    # Two independently authored Word files reuse the same styleIds and rIds for
    # different things. Merging must remap them, or a picture ends up pointing at
    # a footer and a table loses its borders.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = _fixture(tmp / "a.docx", marker="A")
        b = _fixture(tmp / "b.docx", marker="B")
        pa = ds.clone_subset(a, list(range(len(db.read_docx(a).body_children))), tmp / "pa.docx")
        pb = ds.clone_subset(b, list(range(len(db.read_docx(b).body_children))), tmp / "pb.docx")

        out = ds.merge([pa, pb], tmp / "merged.docx")

        assert ds.unresolved_rids(out) == []
        assert _count(out, "drawing") == 2, "both images must survive the merge"
        assert ds.media_count(out) >= 1

        # every image relationship must point at a file that is actually present
        with zipfile.ZipFile(out) as z:
            doc_xml = z.read("word/document.xml").decode()
            rels = z.read("word/_rels/document.xml.rels").decode()
            media = {n for n in z.namelist() if n.startswith("word/media/")}
        import re

        targets = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
        for rid in set(re.findall(r'r:embed="([^"]+)"', doc_xml)):
            assert "word/" + targets[rid] in media, f"{rid} points at a missing part"

        text = db.read_docx(out).text
        assert "A 阅读理解" in text and "B 阅读理解" in text


def test_every_referenced_style_is_defined():
    # The historical corruption: referencing a style that styles.xml never
    # declares. Must hold after notes are added and documents merged.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")
        out = ds.clone_subset(src, list(range(len(db.read_docx(src).body_children))), tmp / "c.docx")
        ds.decorate(out, heading="阅读A｜来源：某卷", notes=[(ds.NOTE_BODY, "讲解正文")])

        with zipfile.ZipFile(out) as z:
            dx = etree.fromstring(z.read("word/document.xml"))
            st = etree.fromstring(z.read("word/styles.xml"))
        defined = {s.get(W + "styleId") for s in st.findall(W + "style")}
        used = {e.get(W + "val") for tag in ("pStyle", "rStyle", "tblStyle") for e in dx.findall(".//" + W + tag)}
        assert used <= defined, f"undefined styles referenced: {used - defined}"


def test_notes_strip_illegal_control_characters():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")
        out = ds.clone_subset(src, [0], tmp / "c.docx")
        ds.decorate(out, notes=[(ds.NOTE_BODY, "答案\x07：B\x00 blank ____36____")])

        with zipfile.ZipFile(out) as z:
            raw = z.read("word/document.xml")
        for ch in (b"\x00", b"\x07", b"\x0b", b"\x1f"):
            assert ch not in raw, "illegal XML control character reached the docx"
        # the blank marker must survive literally, not become bold (the old
        # Markdown path read ____ as __strong__)
        assert "____36____" in db.read_docx(out).text


def test_heading_is_prepended_not_appended():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")
        out = ds.clone_subset(src, list(range(len(db.read_docx(src).body_children))), tmp / "c.docx")
        ds.decorate(out, heading="阅读A｜来源：某卷")
        assert db.read_docx(out).text.startswith("阅读A｜来源：某卷")


def test_validate_rejects_non_a4_page():
    from docx.shared import Cm

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _fixture(tmp / "a.docx")

        # python-docx defaults to US Letter; a paper for a Chinese classroom
        # must be A4, so validate() has to catch this rather than ship it.
        out = ds.clone_subset(src, [0], tmp / "letter.docx")
        try:
            ds.validate(out)
            raise AssertionError("validate() accepted a US Letter page")
        except RuntimeError as exc:
            assert "A4" in str(exc)

        d = Document(str(src))
        for section in d.sections:
            section.page_width, section.page_height = Cm(21.0), Cm(29.7)
        d.save(str(src))
        a4 = ds.clone_subset(src, [0], tmp / "a4.docx")
        ds.validate(a4)  # must not raise


def _docprops(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return "".join(
            z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.startswith("docProps")
        )


def test_scrub_metadata_removes_original_author_after_merge():
    # Must be asserted on a MERGED file, not just a clone: python-docx's
    # core_properties setter does not reach the core.xml that docxcompose
    # writes, so scrubbing a clone passed while the real export still leaked
    # the name of the teacher who wrote the source paper.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, name in enumerate(("a", "b")):
            src = _fixture(tmp / f"{name}.docx", marker=name.upper())
            d = Document(str(src))
            d.core_properties.author = "某位老师"
            d.core_properties.last_modified_by = "某位老师"
            d.save(str(src))
            parts.append(ds.clone_subset(src, [0, 1], tmp / f"p{i}.docx"))

        assert "某位老师" in _docprops(parts[0]), "the clone should inherit it (that's the bug)"

        merged = ds.merge(parts, tmp / "merged.docx")
        ds.scrub_metadata(merged, "学生版")

        assert "某位老师" not in _docprops(merged), "original author leaked into the export"
