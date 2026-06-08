#!/usr/bin/env python3
"""Export Markdown files to .docx using Pandoc as the conversion engine.

This replaces the previous ~700-line hand-rolled OOXML builder with a single
``pandoc`` subprocess call.  Pandoc is a battle-tested document converter that
produces standards-compliant OOXML — no more "发现无法读取的内容" recovery prompts.

Dependencies:
    - pandoc (system) — ``brew install pandoc`` / ``apt install pandoc``
    - Python stdlib only (``subprocess`` + ``pathlib``)

Usage:
    python3 scripts/export_markdown_to_docx.py \\
      --assembled-dir outputs/gaokao_english/assembled \\
      --out-dir outputs/gaokao_english/docx_exports
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# ═══════════════════════════════════════════════════════════════════════════════
#  backward-compatible symbols (kept for the test suite)
# ═══════════════════════════════════════════════════════════════════════════════

EXPORT_MAP = [
    ("final_selected_questions_with_answers.md",
     "final_selected_questions_with_answers.docx"),
    ("final_teacher_notes.md",
     "final_teacher_notes.docx"),
    ("final_answers_only.md",
     "final_answers_only.docx"),
]


def _xml_sanitize(text):
    """Strip characters that are illegal in XML 1.0.

    Although pandoc handles encoding correctly, this function remains as a
    pre-processing safety net for input Markdown that may contain embedded
    control characters from AI-generated text.
    """
    if not isinstance(text, str):
        return text
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)
    text = re.sub(r'[\uD800-\uDFFF]', '', text)
    return text


def parse_markdown(text: str) -> list[dict]:
    """Parse a Markdown string into a list of block descriptors.

    Kept for backward compatibility with the test suite.  This parser is no
    longer used by the main conversion path (pandoc handles parsing), but the
    tests exercise it independently.
    """
    blocks: list[dict] = []
    lines = text.splitlines()
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i].rstrip()

        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                blocks.append({"type": "code_block_start"})
            else:
                blocks.append({"type": "code_block_end"})
            i += 1
            continue

        if in_code_block:
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

        if not line.strip():
            blocks.append({"type": "blank"})
            i += 1
            continue

        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            table_lines = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            blocks.append({"type": "table", "text": "\n".join(table_lines)})
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            blocks.append({"type": "heading", "text": m.group(2).strip(),
                          "level": len(m.group(1))})
            i += 1
            continue

        m = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if m:
            blocks.append({"type": "list_item", "text": m.group(2).strip(),
                          "ordered": False, "indent": len(m.group(1))})
            i += 1
            continue

        m = re.match(r"^(\s*)(\d+)[.)]\s+(.+)$", line)
        if m:
            blocks.append({"type": "list_item", "text": m.group(3).strip(),
                          "ordered": True, "index": int(m.group(2)),
                          "indent": len(m.group(1))})
            i += 1
            continue

        blocks.append({"type": "paragraph", "text": line.strip()})
        i += 1

    return blocks


# ═══════════════════════════════════════════════════════════════════════════════
#  post-export validation (kept for CI / regression tests)
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_docx(path: Path) -> None:
    """Open a .docx and parse every XML part — raise on first parse error."""
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not (name.endswith(".xml") or name.endswith(".rels")):
                continue
            raw = zf.read(name)
            try:
                ET.fromstring(raw)
            except ET.ParseError as exc:
                snippet = raw[:500]
                raise RuntimeError(
                    f"XML parse error in {path.name} → {name}: {exc}\n"
                    f"First 500 bytes: {snippet!r}"
                ) from exc


# ═══════════════════════════════════════════════════════════════════════════════
#  pandoc discovery
# ═══════════════════════════════════════════════════════════════════════════════

def _find_pandoc() -> str:
    """Locate the pandoc binary.  Raises RuntimeError if not found."""
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise RuntimeError(
            "Pandoc not found on PATH.\n"
            "Install it:  brew install pandoc   (macOS)\n"
            "             apt install pandoc     (Linux)\n"
            "             winget install pandoc  (Windows)"
        )
    return pandoc


# ═══════════════════════════════════════════════════════════════════════════════
#  core conversion — the only place where Markdown → docx happens
# ═══════════════════════════════════════════════════════════════════════════════

def build_docx(
    md_path: Path,
    out_path: Path,
    *,
    reference_doc: Path | None = None,
    extra_args: list[str] | None = None,
) -> None:
    """Convert a single Markdown file to .docx via pandoc.

    Parameters
    ----------
    md_path : Path
        Input Markdown file.
    out_path : Path
        Output .docx path (parent directories are created if needed).
    reference_doc : Path | None
        Optional reference .docx for styling (passed to ``--reference-doc``).
        When provided, pandoc uses its styles, fonts, and page layout as a
        template.  Create one with:
            pandoc -o reference.docx --print-default-data-file reference.docx
    extra_args : list[str] | None
        Additional pandoc CLI arguments (e.g. ``['--toc', '--toc-depth=3']``).
    """
    pandoc = _find_pandoc()

    # ── pre-sanitize input (belt-and-suspenders) ──
    raw = md_path.read_text(encoding="utf-8")
    clean = _xml_sanitize(raw)

    # Write sanitized copy to a temp file so pandoc reads clean input
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── build pandoc command ──
    cmd = [
        pandoc,
        "-f", "markdown+pipe_tables+grid_tables+footnotes+fenced_code_blocks",
        "-t", "docx",
        "-o", str(out_path),
    ]

    # Optional reference document for custom styling
    if reference_doc is not None:
        if not reference_doc.exists():
            raise FileNotFoundError(f"Reference doc not found: {reference_doc}")
        cmd += ["--reference-doc", str(reference_doc)]

    if extra_args:
        cmd.extend(extra_args)

    # ── run pandoc with stdin ──
    try:
        result = subprocess.run(
            cmd,
            input=clean,
            text=True,
            capture_output=True,
            timeout=120,  # generous timeout for large documents
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Pandoc timed out converting {md_path.name}. "
            f"The file may be too large or pandoc may be stuck."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"Pandoc failed with exit code {result.returncode} "
            f"converting {md_path.name}:\n{result.stderr[:2000]}"
        )

    if result.stderr:
        print(f"  [pandoc] {result.stderr.strip()[:500]}", file=sys.stderr)

    # ── post-export validation ──
    _validate_docx(out_path)


# ═══════════════════════════════════════════════════════════════════════════════
#  batch export (public API — called by the pipeline and CLI)
# ═══════════════════════════════════════════════════════════════════════════════

def export_markdown_to_docx(
    assembled_dir: Path,
    out_dir: Path,
    *,
    reference_doc: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[Path]:
    """Batch-convert assembled Markdown files to .docx via pandoc.

    Parameters
    ----------
    assembled_dir : Path
        Directory containing the 3 assembled .md files.
    out_dir : Path
        Output directory for .docx files.
    reference_doc : Path | None
        Optional reference .docx for styling.
    extra_args : list[str] | None
        Additional pandoc CLI arguments.

    Returns
    -------
    list[Path]
        Paths of the created .docx files.
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
        build_docx(md_path, out_path,
                   reference_doc=reference_doc,
                   extra_args=extra_args)
        created.append(out_path)
        size_kb = out_path.stat().st_size / 1024
        print(f"  [ok]  {md_name} → {docx_name} ({size_kb:.1f} KB)")

    return created


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Markdown files to .docx via Pandoc")
    parser.add_argument(
        "--assembled-dir", default="outputs/gaokao_english/assembled",
        help="Directory containing assembled .md files")
    parser.add_argument(
        "--out-dir", default="outputs/gaokao_english/docx_exports",
        help="Output directory for .docx files")
    parser.add_argument(
        "--reference-doc", type=Path, default=None,
        help="Optional reference .docx for styling (--reference-doc in pandoc)")
    args = parser.parse_args()

    assembled = Path(args.assembled_dir)
    out = Path(args.out_dir)

    print(f"Exporting from {assembled} → {out}")
    created = export_markdown_to_docx(
        assembled, out, reference_doc=args.reference_doc)
    print(f"\nDone. {len(created)} file(s) created.")

    if not created:
        raise SystemExit(
            f"No .md files found in {assembled}. "
            "Run --mode assemble first."
        )


if __name__ == "__main__":
    main()
