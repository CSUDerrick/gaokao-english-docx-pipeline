"""PDF/OCR ingestion tests.

These drive a recorded PaddleOCR-VL response rather than the live service, so
they run offline and in CI. What they pin down is the contract that matters: OCR
output must become a real .docx that the ordinary pipeline can segment and clone,
so a scanned paper needs no separate code path downstream.
"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docx_blocks as db  # noqa: E402
import docx_splice as ds  # noqa: E402
import pdf_ingest  # noqa: E402


def _png_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (24, 24), (10, 90, 200)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _response() -> dict:
    """Shaped like the documented layout-parsing response."""
    return {
        "errorCode": 0,
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "# 2026 英语试题", "images": {"fig1.png": _png_b64()}},
                    "prunedResult": {
                        "parsing_res_list": [
                            {"block_label": "doc_title", "block_content": "2026届高三英语模拟卷", "block_order": 0},
                            {"block_label": "header", "block_content": "第 1 页", "block_order": 1},
                            {"block_label": "paragraph_title", "block_content": "第二部分 阅读理解", "block_order": 2},
                            {"block_label": "text", "block_content": "A\nUNICEF delivers supplies to children.", "block_order": 3},
                            {"block_label": "image", "block_content": "fig1.png", "block_order": 4},
                            {
                                "block_label": "table",
                                "block_content": "<table><tr><td>21</td><td>A</td></tr><tr><td>22</td><td>B</td></tr></table>",
                                "block_order": 5,
                            },
                        ]
                    },
                }
            ]
        },
    }


def test_parse_response_orders_blocks_and_drops_page_furniture():
    result = pdf_ingest.parse_response(_response())
    labels = [b.label for b in result.blocks]

    assert "header" not in labels, "page headers must not become question text"
    assert labels == ["doc_title", "paragraph_title", "text", "image", "table"]
    assert [b.order for b in result.blocks] == sorted(b.order for b in result.blocks)

    image = [b for b in result.blocks if b.label == "image"][0]
    assert image.image is not None and image.image.startswith(b"\x89PNG")


def test_ocr_blocks_become_a_docx_the_pipeline_can_read():
    result = pdf_ingest.parse_response(_response())
    with tempfile.TemporaryDirectory() as tmp:
        out = pdf_ingest.blocks_to_docx(result, Path(tmp) / "scanned.docx")

        doc = db.read_docx(out)
        assert "2026届高三英语模拟卷" in doc.text
        assert "UNICEF" in doc.text
        assert "21 | A" in doc.text, "the OCR'd table must survive as a real Word table"
        assert ds.media_count(out) == 1, "the figure must be embedded, not dropped"
        assert doc.body_children, "must produce addressable body blocks"


def test_generated_docx_can_be_cloned_like_any_other_paper():
    # The whole point of writing a .docx: a scanned paper flows through the same
    # clone-and-splice export as a Word paper, with no special-casing.
    result = pdf_ingest.parse_response(_response())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = pdf_ingest.blocks_to_docx(result, tmp / "scanned.docx")
        doc = db.read_docx(src)

        out = ds.clone_subset(src, list(range(len(doc.body_children))), tmp / "clone.docx")
        ds.decorate(out, heading="阅读A｜来源：scanned.pdf")

        assert ds.media_count(out) == 1
        assert ds.unresolved_rids(out) == []
        assert "UNICEF" in db.read_docx(out).text


def test_missing_credentials_explain_where_to_get_them():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        try:
            pdf_ingest.ingest_pdf(pdf, Path(tmp), base_url="https://example.com", token="")
            raise AssertionError("should refuse to run without a token")
        except SystemExit as exc:
            assert "accessToken" in str(exc)

        try:
            pdf_ingest.ingest_pdf(pdf, Path(tmp), base_url="", token="tok")
            raise AssertionError("should refuse to run without a base url")
        except SystemExit as exc:
            assert "paddleocr/task" in str(exc)


def test_endpoint_is_not_duplicated_when_already_complete():
    assert pdf_ingest._endpoint("https://x.aistudio-app.com") == "https://x.aistudio-app.com/layout-parsing"
    assert pdf_ingest._endpoint("https://x.aistudio-app.com/layout-parsing") == "https://x.aistudio-app.com/layout-parsing"
    assert pdf_ingest._endpoint("https://x.aistudio-app.com/") == "https://x.aistudio-app.com/layout-parsing"
