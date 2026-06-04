#!/usr/bin/env python3
"""Export Markdown files to .docx using only the Python standard library.

No external dependencies required — builds a valid Office Open XML package
with zipfile + xml.etree.ElementTree, the same way extract_docx_text reads
docx files in the main pipeline.

Usage:
    python3 scripts/export_markdown_to_docx.py \\
      --assembled-dir outputs/gaokao_english/assembled \\
      --out-dir outputs/gaokao_english/docx_exports
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


# ── namespaces ──────────────────────────────────────────────────────────────
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"

ET.register_namespace("w", W)
ET.register_namespace("r", R)


def _el(tag: str, parent: ET.Element | None = None, **kwargs) -> ET.Element:
    """Create an element in the WordprocessingML namespace."""
    el = ET.Element(f"{{{W}}}{tag}", **{k: str(v) for k, v in kwargs.items() if v is not None})
    if parent is not None:
        parent.append(el)
    return el


def _set_text(paragraph: ET.Element, text: str, bold: bool = False,
              font_cn: str = "等线", font_en: str = "Calibri", size: int = 22) -> None:
    """Add a text run to a paragraph."""
    r = _el("r", paragraph)
    rpr = _el("rPr", r)
    if bold:
        _el("b", rpr)
    _el("sz", rpr, val=str(size))
    _el("szCs", rpr, val=str(size))
    # Fonts
    rfonts = _el("rFonts", rpr, ascii=font_en, hAnsi=font_en, eastAsia=font_cn)
    t = _el("t", r, space="preserve")
    t.text = text


def _add_paragraph(body: ET.Element, text: str, style: str = "",
                   font_cn: str = "等线", font_en: str = "Calibri",
                   size: int = 22, bold: bool = False, spacing_after: int = 120) -> ET.Element:
    """Add a paragraph with optional style."""
    p = _el("p", body)
    ppr = _el("pPr", p)
    if style:
        _el("pStyle", ppr, val=style)
    if spacing_after:
        _el("spacing", ppr, after=str(spacing_after), line="276", lineRule="auto")
    _set_text(p, text, bold=bold, font_cn=font_cn, font_en=font_en, size=size)
    return p


def _add_heading(body: ET.Element, text: str, level: int) -> None:
    """Add a heading paragraph."""
    sizes = {1: 36, 2: 30, 3: 26}
    p = _el("p", body)
    ppr = _el("pPr", p)
    _el("pStyle", ppr, val=f"Heading{level}")
    _el("spacing", ppr, before="240", after="120", line="276", lineRule="auto")
    _set_text(p, text, bold=True, size=sizes.get(level, 26))


def _add_blank_line(body: ET.Element) -> None:
    """Add an empty paragraph for spacing."""
    p = _el("p", body)
    _el("pPr", p)


# ── Markdown parsing ────────────────────────────────────────────────────────

def parse_markdown(text: str) -> list[dict]:
    """Parse a Markdown string into a list of block descriptors.

    Each block is a dict with keys:
      type: "heading", "paragraph", "blank", "list_item"
      text: str
      level: int (for headings)
      ordered: bool (for list items)
      list_index: int or None (for list items, 1-based)
    """
    blocks: list[dict] = []
    lines = text.splitlines()
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i].rstrip()

        # code block fence
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                blocks.append({"type": "code_block_start"})
            else:
                blocks.append({"type": "code_block_end"})
            i += 1
            continue

        if in_code_block:
            # Inside a code block: collect as monospaced text
            code_lines: list[str] = []
            while i < len(lines):
                l = lines[i].rstrip()
                if l.strip().startswith("```"):
                    in_code_block = False
                    break
                code_lines.append(l)
                i += 1
            blocks.append({"type": "code", "text": "\n".join(code_lines)})
            if not in_code_block:
                blocks.append({"type": "code_block_end"})
            i += 1
            continue

        # --- blank line ---
        if not line.strip():
            blocks.append({"type": "blank"})
            i += 1
            continue

        # --- table: skip rendering for now, pass as plain text ---
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            table_lines = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            blocks.append({"type": "table", "text": "\n".join(table_lines)})
            continue

        # --- heading ---
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            blocks.append({"type": "heading", "text": m.group(2).strip(),
                          "level": len(m.group(1))})
            i += 1
            continue

        # --- unordered list ---
        m = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if m:
            indent = len(m.group(1))
            blocks.append({"type": "list_item", "text": m.group(2).strip(),
                          "ordered": False, "indent": indent})
            i += 1
            continue

        # --- ordered list ---
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.+)$", line)
        if m:
            indent = len(m.group(1))
            blocks.append({"type": "list_item", "text": m.group(3).strip(),
                          "ordered": True, "index": int(m.group(2)),
                          "indent": indent})
            i += 1
            continue

        # --- regular paragraph ---
        blocks.append({"type": "paragraph", "text": line.strip()})
        i += 1

    return blocks


# ── docx generation ─────────────────────────────────────────────────────────

def render_blocks_to_docx(body: ET.Element, blocks: list[dict]) -> None:
    """Render parsed Markdown blocks into a WordprocessingML body element."""
    in_code = False

    for block in blocks:
        t = block["type"]

        if t == "blank":
            _add_blank_line(body)

        elif t == "heading":
            _add_heading(body, block["text"], block["level"])

        elif t == "list_item":
            indent = block.get("indent", 0)
            prefix = ""
            if block.get("ordered"):
                prefix = f"{block.get('index', 0)}.  "
            else:
                prefix = "•  "
            prefix = "    " * min(indent // 2, 3) + prefix
            _add_paragraph(body, prefix + block["text"], spacing_after=80)

        elif t == "paragraph":
            # Handle inline bold: **text**
            text = block["text"]
            p = _el("p", body)
            ppr = _el("pPr", p)
            _el("spacing", ppr, after="120", line="276", lineRule="auto")
            # Simple bold parsing
            parts = re.split(r"(\*\*[^*]+\*\*)", text)
            for part in parts:
                if part.startswith("**") and part.endswith("**"):
                    _set_text(p, part[2:-2], bold=True)
                else:
                    _set_text(p, part)
            body.append(p)

        elif t == "code":
            # Monospaced block
            for code_line in block["text"].splitlines():
                p = _el("p", body)
                ppr = _el("pPr", p)
                _el("spacing", ppr, after="40", line="240", lineRule="auto")
                _set_text(p, code_line or " ", font_cn="Courier New",
                         font_en="Courier New", size=18)
            _add_blank_line(body)

        elif t == "table":
            # Render as monospaced text
            for tbl_line in block["text"].splitlines():
                p = _el("p", body)
                ppr = _el("pPr", p)
                _el("spacing", ppr, after="40", line="240", lineRule="auto")
                _set_text(p, tbl_line, font_cn="Courier New",
                         font_en="Courier New", size=18)
            _add_blank_line(body)

        elif t in ("code_block_start", "code_block_end"):
            pass  # handled by the code-collection logic


def build_docx(md_path: Path, out_path: Path) -> None:
    """Convert a single Markdown file to .docx."""
    text = md_path.read_text(encoding="utf-8")
    blocks = parse_markdown(text)

    # --- word/document.xml ---
    doc = ET.Element(f"{{{W}}}document",
                     {f"{{{W}}}conformance": "transitional"})
    body = _el("body", doc)
    render_blocks_to_docx(body, blocks)

    # --- [Content_Types].xml ---
    types = ET.Element("Types", xmlns=CT)
    ET.SubElement(types, "Default", Extension="rels",
                  ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, "Default", Extension="xml",
                  ContentType="application/xml")
    ET.SubElement(types, "Override",
                  PartName="/word/document.xml",
                  ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml")

    # --- _rels/.rels ---
    rels = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    ET.SubElement(rels, "Relationship", Id="rId1",
                  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                  Target="word/document.xml")

    # --- word/_rels/document.xml.rels (minimal) ---
    doc_rels = ET.Element("Relationships", xmlns=R)

    # --- assemble ZIP ---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    ET.tostring(types, encoding="unicode", xml_declaration=True))
        zf.writestr("_rels/.rels",
                    ET.tostring(rels, encoding="unicode", xml_declaration=True))
        zf.writestr("word/document.xml",
                    ET.tostring(doc, encoding="unicode", xml_declaration=True))
        zf.writestr("word/_rels/document.xml.rels",
                    ET.tostring(doc_rels, encoding="unicode", xml_declaration=True))


EXPORT_MAP = [
    ("final_selected_questions_with_answers.md",
     "final_selected_questions_with_answers.docx"),
    ("final_teacher_notes.md",
     "final_teacher_notes.docx"),
    ("final_answers_only.md",
     "final_answers_only.docx"),
]


def export_markdown_to_docx(assembled_dir: Path, out_dir: Path) -> list[Path]:
    """Convert all known Markdown files in assembled_dir to docx.

    Returns a list of paths to successfully created .docx files.
    """
    if not assembled_dir.exists():
        raise FileNotFoundError(f"Assembled directory not found: {assembled_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    for md_name, docx_name in EXPORT_MAP:
        md_path = assembled_dir / md_name
        if not md_path.exists():
            print(f"  [skip] {md_name} — not found")
            continue
        out_path = out_dir / docx_name
        build_docx(md_path, out_path)
        created.append(out_path)
        size_kb = out_path.stat().st_size / 1024
        print(f"  [ok]  {md_name} → {docx_name} ({size_kb:.1f} KB)")

    return created


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Markdown files to .docx (stdlib only, no external deps)")
    parser.add_argument("--assembled-dir", default="outputs/gaokao_english/assembled",
                        help="Directory containing assembled .md files")
    parser.add_argument("--out-dir", default="outputs/gaokao_english/docx_exports",
                        help="Output directory for .docx files")
    args = parser.parse_args()

    assembled = Path(args.assembled_dir)
    out = Path(args.out_dir)

    print(f"Exporting from {assembled} → {out}")
    created = export_markdown_to_docx(assembled, out)
    print(f"\nDone. {len(created)} file(s) created.")

    if not created:
        raise SystemExit(f"No .md files found in {assembled}. "
                         "Run --mode assemble first.")


if __name__ == "__main__":
    main()
