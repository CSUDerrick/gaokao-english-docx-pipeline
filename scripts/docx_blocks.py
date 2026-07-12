#!/usr/bin/env python3
"""Block model for docx input.

The pipeline used to read a .docx straight into a flat string, which threw away
every ``w:rPr`` (bold/underline), ``w:pPr`` (indent/alignment) and ``w:drawing``
(images) in the file.  Everything downstream then had to re-invent the layout.

This module reads the same file into a list of :class:`Block` while *keeping a
handle on the original OOXML node* for each one, so the export step can copy the
teacher's existing typesetting instead of rebuilding it.

The flat text it produces is byte-identical to the old ``extract_docx_text``, so
the regex segmenter keeps working unchanged.  See ``tests/test_docx_blocks.py``
for the equivalence test that pins this.

A PDF/OCR source produces the same :class:`Block` list with ``element=None`` —
there is no original OOXML to clone, so those get typeset from a template.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def node_text(node: ET.Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        tag = local_name(child.tag)
        if tag == "t" and child.text:
            parts.append(child.text)
        elif tag == "tab":
            parts.append("\t")
        elif tag in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def normalize_inline_text(text: str) -> str:
    text = unescape(text)
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class Block:
    """One emitted line of the flat text, tied back to its source.

    A table emits one block per row, so several blocks can share a
    ``body_index``; the splicer copies the enclosing ``w:tbl`` once.
    """

    body_index: int
    kind: str  # "p" | "tbl"
    text: str
    char_start: int
    char_end: int  # exclusive; does not cover the joining newline
    label: str = ""  # OCR only: paragraph_title / text / table / image / formula


@dataclass
class DocxDoc:
    path: Path
    text: str
    blocks: list[Block]
    body_children: list[ET.Element] = field(default_factory=list)

    def body_range(self, char_start: int, char_end: int) -> tuple[int, int]:
        """Map a char span in ``text`` to a half-open body-child index range.

        A segment boundary can land mid-block (the segmenter cuts on line
        starts, but the answer-key trim can cut anywhere), so a block counts as
        included when it overlaps the span at all.
        """
        hit = [b.body_index for b in self.blocks if b.char_start < char_end and b.char_end > char_start]
        if not hit:
            return (0, 0)
        return (min(hit), max(hit) + 1)


def element_children(body) -> list:
    """Body children, comments and processing instructions excluded.

    ``docx_splice`` re-derives these indices with lxml, which (unlike
    ElementTree) keeps comment nodes.  Both sides filter on ``tag`` being a
    string so the indices refer to the same paragraphs on both sides.
    """
    return [c for c in body if isinstance(c.tag, str)]


def _blocks_from_body(body: ET.Element) -> tuple[str, list[Block], list[ET.Element]]:
    children = element_children(body)
    blocks: list[Block] = []
    lines: list[str] = []
    pos = 0

    def emit(body_index: int, kind: str, text: str) -> None:
        nonlocal pos
        start = pos
        blocks.append(Block(body_index, kind, text, start, start + len(text)))
        lines.append(text)
        pos = start + len(text) + 1  # +1 for the "\n" that will join them

    for idx, child in enumerate(children):
        tag = local_name(child.tag)
        if tag == "p":
            text = normalize_inline_text(node_text(child))
            if text:
                emit(idx, "p", text)
        elif tag == "tbl":
            for row in child.findall(".//w:tr", NS):
                cells = [normalize_inline_text(node_text(c)) for c in row.findall("./w:tc", NS)]
                cells = [c for c in cells if c]
                if cells:
                    emit(idx, "tbl", " | ".join(cells))

    return "\n".join(lines), blocks, children


def read_docx(path: Path) -> DocxDoc:
    try:
        with zipfile.ZipFile(path) as zf:
            xml_bytes = zf.read("word/document.xml")
    except KeyError as exc:
        raise RuntimeError(f"{path} does not look like a normal docx: missing word/document.xml") from exc

    root = ET.fromstring(xml_bytes)
    body = root.find("w:body", NS)
    if body is None:
        return DocxDoc(path=path, text="", blocks=[], body_children=[])

    text, blocks, children = _blocks_from_body(body)
    return DocxDoc(path=path, text=text, blocks=blocks, body_children=children)


def extract_docx_text(path: Path) -> str:
    """Backwards-compatible flat-text reader."""
    return read_docx(path).text
