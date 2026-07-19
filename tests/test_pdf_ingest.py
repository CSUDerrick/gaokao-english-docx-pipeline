"""PDF/OCR ingestion tests.

These drive a recorded PaddleOCR-VL response rather than the live service, so
they run offline and in CI. What they pin down is the contract that matters: OCR
output must become a real .docx that the ordinary pipeline can segment and clone,
so a scanned paper needs no separate code path downstream.

The service is an async job API — submit, poll, download the result JSONL — so the
transport tests replace the single ``_request`` seam, the same way the MinerU ones do.
The recorded page shape below is a real v2 reply: it still carries
``prunedResult.parsing_res_list``, which is what gives the blocks their labels.
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


def test_missing_token_explains_where_to_get_it():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        try:
            pdf_ingest.ingest_pdf(pdf, Path(tmp), base_url="https://example.com", token="")
            raise AssertionError("should refuse to run without a token")
        except SystemExit as exc:
            assert "accessToken" in str(exc)


JOBS = "/api/v2/ocr/jobs"


def test_the_service_address_is_optional_and_an_old_one_still_works():
    # v2 is one global endpoint, so a teacher who configured nothing must still work:
    # the old code refused to start without a per-account URL copied from the console.
    assert pdf_ingest._jobs_url("") == f"https://paddleocr.aistudio-app.com{JOBS}"
    assert pdf_ingest._jobs_url("https://x.aistudio-app.com") == f"https://x.aistudio-app.com{JOBS}"
    assert pdf_ingest._jobs_url("https://x.aistudio-app.com/") == f"https://x.aistudio-app.com{JOBS}"
    # A URL left in the Keychain from the per-account layout-parsing days: keep the
    # host, drop the dead path rather than pasting the new one onto it.
    assert pdf_ingest._jobs_url("https://x.aistudio-app.com/layout-parsing") == f"https://x.aistudio-app.com{JOBS}"
    assert pdf_ingest._jobs_url(f"https://x.aistudio-app.com{JOBS}") == f"https://x.aistudio-app.com{JOBS}"


def test_the_upload_is_multipart_with_the_file_and_the_model():
    # The API's examples use requests' files=; this project has no requests, so the body
    # is hand-built and nothing else checks it.
    ctype, body = pdf_ingest._encode_multipart(
        {"model": "PaddleOCR-VL-1.6", "optionalPayload": '{"a": 1}'}, "file", "x.pdf", b"%PDF-1.4 body"
    )
    boundary = ctype.split("boundary=")[1]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="model"' in body and b"PaddleOCR-VL-1.6" in body
    assert b'name="optionalPayload"' in body
    assert b'name="file"; filename="x.pdf"' in body
    assert b"%PDF-1.4 body" in body
    assert body.endswith(f"--{boundary}--\r\n".encode()), "a body without the closing boundary is a 400"


def test_the_poll_loop_can_be_cancelled():
    # PaddleOCR used to be one blocking POST and so never needed this. Now it waits in a
    # loop, and a wait that ignores the cancel event leaves the teacher pressing 取消
    # until the OCR finishes on its own.
    import gaokao_english_docx_pipeline as pipeline

    original = pdf_ingest._request
    pdf_ingest._request = lambda *a, **k: {
        "code": 0,
        "data": {"state": "running", "extractProgress": {"totalPages": 3, "extractedPages": 1}},
    }
    slept: list[float] = []

    def sleeping(seconds: float) -> None:
        slept.append(seconds)
        raise pipeline.Cancelled("已取消")

    try:
        pdf_ingest._wait("job1", "tok", f"https://x{JOBS}", sleep=sleeping)
        raise AssertionError("cancel must break the poll loop")
    except pipeline.Cancelled:
        pass
    finally:
        pdf_ingest._request = original
    assert slept, "the loop must go through the injected sleep, not time.sleep"


def test_a_failed_job_says_why():
    original = pdf_ingest._request
    pdf_ingest._request = lambda *a, **k: {"code": 0, "data": {"state": "failed", "errorMsg": "文件损坏"}}
    try:
        pdf_ingest._wait("job1", "tok", f"https://x{JOBS}", sleep=lambda s: None)
        raise AssertionError("a failed job must not look like success")
    except RuntimeError as exc:
        assert "文件损坏" in str(exc)
    finally:
        pdf_ingest._request = original


def test_a_done_job_hands_back_the_result_url():
    replies = [
        {"code": 0, "data": {"state": "pending"}},
        {"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": "https://x/r.jsonl"}}},
    ]
    original = pdf_ingest._request
    pdf_ingest._request = lambda *a, **k: replies.pop(0)
    slept: list[float] = []
    try:
        url = pdf_ingest._wait("job1", "tok", f"https://x{JOBS}", sleep=slept.append)
    finally:
        pdf_ingest._request = original
    assert url == "https://x/r.jsonl"
    assert slept == [pdf_ingest.POLL_INTERVAL], "pending must wait once, then re-poll"


def test_a_table_without_a_block_order_stays_where_it_was_found():
    # Real v2 replies give tables `block_order: None`. The old code turned that into 0,
    # which sorted every table to the top of its page — ahead of the paper's own title.
    # Caught on a live run, where the answer grid jumped above "Reading Comprehension".
    body = {"result": {"layoutParsingResults": [{
        "prunedResult": {"parsing_res_list": [
            {"block_label": "doc_title", "block_content": "Reading Comprehension", "block_order": 1},
            {"block_label": "text", "block_content": "Answer Sheet", "block_order": 2},
            {"block_label": "table", "block_content": "<table><tr><td>21</td></tr></table>",
             "block_order": None},
        ]},
        "markdown": {"text": "", "images": {}},
    }]}}
    result = pdf_ingest.parse_response(body)
    assert [b.label for b in result.blocks] == ["doc_title", "text", "table"], \
        "the table must stay last, not jump above the title"


def test_pages_split_across_jsonl_lines_keep_reading_order():
    # A job's pages can come back over several lines. Restarting the page counter per
    # line would interleave page 2 back into page 1.
    import json

    def line(title: str) -> str:
        return json.dumps({"result": {"layoutParsingResults": [{
            "prunedResult": {"parsing_res_list": [
                {"block_label": "doc_title", "block_content": title, "block_order": 1},
                {"block_label": "text", "block_content": f"body of {title}", "block_order": 2},
            ]},
            "markdown": {"text": f"# {title}", "images": {}},
        }]}})

    result = pdf_ingest.parse_jsonl([line("Page One"), line("Page Two")])
    assert [b.text for b in result.blocks] == [
        "Page One", "body of Page One", "Page Two", "body of Page Two",
    ]
