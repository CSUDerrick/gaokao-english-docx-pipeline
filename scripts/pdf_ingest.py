#!/usr/bin/env python3
"""PDF input via PaddleOCR-VL.

A scanned paper has no OOXML to clone, so it has to be typeset. Rather than add a
second code path through the whole pipeline, OCR output is written out as a real
``.docx`` first — after that it is just another source paper: it gets segmented,
scored, cloned and exported by exactly the same code as a Word input.

    paper.pdf --[PaddleOCR-VL]--> paper.docx --> (the normal pipeline)

The generated .docx is what ``source_blocks`` then points at, so the "clone the
original" export works for scanned papers too; the layout it clones is the one
reconstructed here.

Endpoint: the AI Studio layout-parsing service is deployed **per account**, so the
base URL has to come from the user's own console
(https://aistudio.baidu.com/paddleocr/task) — there is no global URL to hardcode.
The token comes from https://aistudio.baidu.com/account/accessToken and is read
from ``PADDLEOCR_ACCESS_TOKEN``.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.shared import Inches

import docx_splice as ds
from bundle_paths import template_dir

MODEL = "PaddleOCR-VL-1.6"
TOKEN_ENV = "PADDLEOCR_ACCESS_TOKEN"
BASE_URL_ENV = "PADDLEOCR_BASE_URL"

# PaddleOCR block_label -> what it should look like in Word
HEADINGS = {"doc_title", "paragraph_title", "title"}
SKIP = {"header", "footer", "page_number", "seal"}


@dataclass
class OcrBlock:
    label: str
    text: str
    order: int
    image: bytes | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class OcrResult:
    blocks: list[OcrBlock] = field(default_factory=list)
    markdown: str = ""


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/layout-parsing") else f"{base}/layout-parsing"


def call_api(pdf: Path, *, base_url: str, token: str, timeout: int = 600) -> dict:
    payload = {
        "file": base64.b64encode(pdf.read_bytes()).decode("ascii"),
        "fileType": 0,  # 0 = PDF, 1 = image
        "model": MODEL,
        "useChartRecognition": True,
        "prettifyMarkdown": True,
    }
    request = urllib.request.Request(
        _endpoint(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 429:
            raise RuntimeError("PaddleOCR 免费额度已用完（HTTP 429），请稍后再试或在控制台查看配额。") from exc
        raise RuntimeError(f"PaddleOCR API 调用失败 (HTTP {exc.code}): {detail}") from exc

    if body.get("errorCode", 0) != 0:
        raise RuntimeError(f"PaddleOCR API 返回错误: {body.get('errorMsg')}")
    return body


def parse_response(body: dict) -> OcrResult:
    """Flatten the API response into reading-ordered blocks.

    The response nests one entry per page; ``parsing_res_list`` inside each holds
    the layout blocks with their reading order, which is what decides the order
    paragraphs land in the Word file.
    """
    result = OcrResult()
    pages = (body.get("result") or {}).get("layoutParsingResults") or []

    for page_no, page in enumerate(pages):
        md = page.get("markdown") or {}
        if isinstance(md, dict) and md.get("text"):
            result.markdown += md["text"] + "\n"

        images = {k: v for k, v in (md.get("images") or {}).items()} if isinstance(md, dict) else {}
        pruned = page.get("prunedResult") or {}

        for item in pruned.get("parsing_res_list") or []:
            label = str(item.get("block_label") or "text")
            if label in SKIP:
                continue
            order = item.get("block_order")
            order = page_no * 10_000 + (int(order) if order is not None else 0)

            content = item.get("block_content")
            blob = None
            if label in {"image", "figure", "chart"} and isinstance(content, str) and content in images:
                try:
                    blob = base64.b64decode(images[content])
                except Exception:
                    blob = None

            result.blocks.append(
                OcrBlock(
                    label=label,
                    text="" if blob else str(content or ""),
                    order=order,
                    image=blob,
                    bbox=tuple(item["block_bbox"]) if item.get("block_bbox") else None,
                )
            )

    result.blocks.sort(key=lambda b: b.order)
    return result


TEMPLATE = template_dir() / "student_reference.docx"


def blocks_to_docx(result: OcrResult, out: Path) -> Path:
    """Typeset OCR blocks into a Word file the rest of the pipeline can consume."""
    doc = Document(str(TEMPLATE)) if TEMPLATE.exists() else Document()
    for para in list(doc.paragraphs):
        para._element.getparent().remove(para._element)
    ds.ensure_note_styles(doc)

    import io

    for block in result.blocks:
        if block.image:
            try:
                doc.add_picture(io.BytesIO(block.image), width=Inches(4.5))
                continue
            except Exception:
                continue  # an undecodable figure must not sink the whole paper

        text = ds.sanitize(block.text).strip()
        if not text:
            continue
        if block.label == "table" and "<" in text:
            _add_table(doc, text)
            continue
        style = ds.NOTE_HEADING if block.label in HEADINGS else ds.NOTE_BODY
        for line in text.split("\n"):
            if line.strip():
                doc.add_paragraph(line, style=style)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def _add_table(doc, html: str) -> None:
    """Render the HTML table PaddleOCR returns for table blocks."""
    from html.parser import HTMLParser

    class Rows(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self.cell: str | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.rows.append([])
            elif tag in ("td", "th"):
                self.cell = ""

        def handle_data(self, data):
            if self.cell is not None:
                self.cell += data

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.cell is not None and self.rows:
                self.rows[-1].append(self.cell.strip())
                self.cell = None

    parser = Rows()
    parser.feed(html)
    rows = [r for r in parser.rows if r]
    if not rows:
        return

    table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
    # Borders are set directly rather than via a "Table Grid" style: the style is
    # not guaranteed to exist in the template, and referencing a style that
    # styles.xml never declares is what made Word report unreadable content.
    _set_borders(table)
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            table.cell(i, j).text = ds.sanitize(cell)


def _set_borders(table) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        line = OxmlElement(f"w:{edge}")
        line.set(qn("w:val"), "single")
        line.set(qn("w:sz"), "4")
        line.set(qn("w:color"), "000000")
        borders.append(line)
    table._tbl.tblPr.append(borders)


def ingest_pdf(pdf: Path, out_dir: Path, *, base_url: str = "", token: str = "") -> Path:
    """OCR a PDF and write a .docx next to the other input papers."""
    token = token or os.environ.get(TOKEN_ENV, "")
    base_url = base_url or os.environ.get(BASE_URL_ENV, "")
    if not token:
        raise SystemExit(
            f"缺少 PaddleOCR access token。请在 https://aistudio.baidu.com/account/accessToken "
            f"获取后设置环境变量 {TOKEN_ENV}。"
        )
    if not base_url:
        raise SystemExit(
            f"缺少 PaddleOCR 服务地址。该服务按账号部署，请在 https://aistudio.baidu.com/paddleocr/task "
            f"查看你的专属地址后设置环境变量 {BASE_URL_ENV}。"
        )

    result = parse_response(call_api(pdf, base_url=base_url, token=token))
    if not result.blocks:
        raise SystemExit(f"{pdf.name}: PaddleOCR 未识别出任何内容。")
    return blocks_to_docx(result, out_dir / f"{pdf.stem}.docx")
