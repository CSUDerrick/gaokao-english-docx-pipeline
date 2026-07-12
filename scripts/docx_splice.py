#!/usr/bin/env python3
"""Clone-and-splice docx writer.

The old export rebuilt a Word file from Markdown, which meant every font, indent,
bold run and image in the teacher's original paper had to be re-invented — and
images simply vanished.  This module instead *copies the original paragraphs*.

The one hard problem is that selected questions come from several different exam
papers, and Chinese Word files all use auto-generated style ids that collide:
``a3`` means ``footer`` in one paper, ``Title`` in another, and ``rId6`` is an
image in one and ``endnotes.xml`` in another.  Naively moving paragraphs between
documents silently renders a footer as a headline and points pictures at notes.

So the work is split in two, and neither half has to solve that:

1. :func:`clone_subset` — copy ONE source .docx byte-for-byte and keep only the
   chosen paragraphs.  Single source, so every style id and relationship id is
   still the one it was authored against.  Images, tables and fonts come along
   for free because ``word/media/*``, ``styles.xml`` and ``_rels`` are untouched.
2. :func:`merge` — concatenate those single-source files with ``docxcompose``,
   which exists to remap exactly these id collisions.

New content (the AI explanations) has no original to clone, so it is written
with python-docx against named styles that are *registered in styles.xml before
being referenced* — see ``docs/word_compatibility.md`` for why that matters.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.shared import Pt
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Word chokes on these and reports "unreadable content" — see word_compatibility.md
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

NOTE_BODY = "讲解正文"
NOTE_HEADING = "讲解标题"
NOTE_ANSWER = "讲解答案"


def sanitize(text: str) -> str:
    return ILLEGAL_XML.sub("", text or "")


def _element_children(body) -> list:
    return [c for c in body if isinstance(c.tag, str)]


def clone_subset(src: Path, body_indices: list[int], out: Path) -> Path:
    """Copy ``src`` keeping only the body children in ``body_indices``.

    Everything except ``word/document.xml`` is copied verbatim, so styles, theme,
    fonts, numbering, relationships and ``word/media/*`` all still resolve.  The
    original ``w:sectPr`` is kept so page size and margins survive.
    """
    wanted = sorted(set(body_indices))

    with zipfile.ZipFile(src) as zin:
        doc_xml = zin.read("word/document.xml")
        entries = [(i, zin.read(i.filename)) for i in zin.infolist()]

    root = etree.fromstring(doc_xml)
    body = root.find(f"{{{W}}}body")
    if body is None:
        raise RuntimeError(f"{src} has no w:body")

    children = _element_children(body)
    sect_pr = body.find(f"{{{W}}}sectPr")

    keep = [children[i] for i in wanted if 0 <= i < len(children)]
    # sectPr is a body child too; it must stay last and must not be duplicated.
    keep = [c for c in keep if c is not sect_pr]

    for child in list(body):
        body.remove(child)
    for child in keep:
        body.append(child)
    if sect_pr is not None:
        body.append(sect_pr)

    new_doc = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in entries:
            zout.writestr(info, new_doc if info.filename == "word/document.xml" else data)
    return out


def ensure_note_styles(doc: Document) -> None:
    """Register the explanation styles before anything references them.

    Named styles (rather than direct run formatting) so the teacher can restyle
    every explanation in one go via Word's style pane.
    """
    specs = [
        (NOTE_HEADING, 12, True),
        (NOTE_BODY, 10.5, False),
        (NOTE_ANSWER, 10.5, True),
    ]
    existing = {s.name for s in doc.styles}
    for name, size, bold in specs:
        if name in existing:
            continue
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.font.name = "Times New Roman"
        style.font.size = Pt(size)
        style.font.bold = bold
        # East-Asian font must be set on rPr/rFonts or Word falls back for Chinese.
        style.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial Unicode MS")


def decorate(path: Path, heading: str = "", notes: list[tuple[str, str]] | None = None) -> None:
    """Add a heading above, and explanation paragraphs below, a cloned question."""
    notes = notes or []
    if not heading and not notes:
        return
    doc = Document(str(path))
    ensure_note_styles(doc)
    if heading:
        first = doc.paragraphs[0] if doc.paragraphs else doc.add_paragraph()
        first.insert_paragraph_before(sanitize(heading), style=NOTE_HEADING)
    for style_name, text in notes:
        for line in sanitize(text).split("\n"):
            doc.add_paragraph(line, style=style_name)
    doc.save(str(path))


AUTHOR = "高三英语试卷整理工具"


def scrub_metadata(path: Path, title: str) -> None:
    """Drop the original paper's author out of the exported file.

    The clone copies ``docProps`` verbatim, which carries the name of whoever
    authored the source exam — a real person — into the teacher's output.

    Two things make this fiddlier than it looks, and both have bitten:

    * python-docx's ``core_properties`` setter does not reach the core.xml that
      docxcompose actually writes, so the name survived a merge.
    * core.xml can end up holding a *second* ``lastModifiedBy`` whose ``cp:``
      prefix is bound to the custom-properties namespace rather than
      core-properties, so a namespace-qualified lookup misses it.

    So: rewrite the part in the zip, and match on local name only.
    """
    scrub = {"creator": AUTHOR, "lastModifiedBy": AUTHOR, "title": title,
             "description": "", "lastPrinted": "", "category": ""}

    with zipfile.ZipFile(path) as zin:
        entries = [(i, zin.read(i.filename)) for i in zin.infolist()]

    out: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in entries:
        if info.filename == "docProps/core.xml":
            root = etree.fromstring(data)
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                name = el.tag.rsplit("}", 1)[-1]
                if name in scrub:
                    el.text = scrub[name]
            data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        out.append((info, data))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in out:
            zout.writestr(info, data)


def merge(parts: list[Path], out: Path, page_break: bool = True) -> Path:
    """Concatenate single-source docx files, remapping colliding ids."""
    if not parts:
        raise ValueError("nothing to merge")

    from docxcompose.composer import Composer

    master = Document(str(parts[0]))
    composer = Composer(master)
    for part in parts[1:]:
        composer.append(Document(str(part)), remove_property_fields=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    composer.save(str(out))
    return out


def media_count(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        return len([n for n in z.namelist() if n.startswith("word/media/")])


def validate(path: Path) -> None:
    """Fail loudly rather than hand the teacher a file Word wants to repair."""
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                etree.fromstring(z.read(name))  # raises on malformed XML
        doc = etree.fromstring(z.read("word/document.xml"))
        styles = etree.fromstring(z.read("word/styles.xml"))

    pg = doc.find(f".//{{{W}}}sectPr/{{{W}}}pgSz")
    if pg is None:
        raise RuntimeError(f"{path.name}: no page size")
    width, height = int(pg.get(f"{{{W}}}w")), int(pg.get(f"{{{W}}}h"))
    if not (11800 <= width <= 12050 and 16700 <= height <= 17000):
        raise RuntimeError(f"{path.name}: page is {width}x{height} twips, expected A4 portrait")

    defined = {s.get(f"{{{W}}}styleId") for s in styles.findall(f"{{{W}}}style")}
    used = {
        e.get(f"{{{W}}}val")
        for tag in ("pStyle", "rStyle", "tblStyle")
        for e in doc.findall(f".//{{{W}}}{tag}")
    }
    if not used <= defined:
        raise RuntimeError(f"{path.name}: references undefined styles {used - defined}")

    dangling = unresolved_rids(path)
    if dangling:
        raise RuntimeError(f"{path.name}: unresolved relationship ids {dangling}")


def unresolved_rids(path: Path) -> list[str]:
    """Relationship ids referenced by the body but absent from the rels part.

    A non-empty result means a picture points at nothing — the exact corruption
    that cross-document splicing causes if ids are not remapped.
    """
    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        try:
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        except KeyError:
            rels = ""
    used = set(re.findall(r'r:(?:embed|id|link)="([^"]+)"', doc))
    defined = set(re.findall(r'Id="([^"]+)"', rels))
    return sorted(used - defined)
