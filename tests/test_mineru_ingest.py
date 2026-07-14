"""MinerU produces the same blocks PaddleOCR does, so the pipeline cannot tell them apart.

The whole design rests on that: a scanned paper becomes a real .docx and is then
segmented, scored, cloned and exported by exactly the same code as a Word input. If the
two backends disagreed about what a heading is, or about reading order, a MinerU paper
would be laid out differently from a PaddleOCR one and only a human would ever notice.

MinerU is also asynchronous where PaddleOCR is not — submit, poll, download a zip — and
the poll loop has to be interruptible, or 取消 would do nothing for as long as MinerU
takes to parse the paper.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mineru_ingest  # noqa: E402
from pdf_ingest import OcrBlock  # noqa: E402

# One page of a paper, in the shape content_list.json really has.
CONTENT_LIST = [
    {"type": "text", "text": "广东省某中学 2026 届模拟卷", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "第二部分 阅读理解", "text_level": 2, "page_idx": 0},
    {"type": "text", "text": "Scientists have long known that trees cool the air.", "page_idx": 0},
    {"type": "image", "img_path": "images/fig1.jpg", "page_idx": 0},
    {"type": "table", "table_body": "<table><tr><td>A</td><td>B</td></tr></table>", "page_idx": 0},
    {"type": "header", "text": "should be dropped", "page_idx": 0},
    {"type": "page_number", "text": "1", "page_idx": 0},
    {"type": "text", "text": "第二页的正文。", "page_idx": 1},
]
IMAGES = {"images/fig1.jpg": b"\x89PNG-not-really", "fig1.jpg": b"\x89PNG-not-really"}


def test_content_list_becomes_ocr_blocks_in_reading_order():
    result = mineru_ingest.parse_content_list(CONTENT_LIST, IMAGES)

    # Running headers, footers and page numbers are furniture, not content.
    assert not any("should be dropped" in b.text for b in result.blocks)
    assert not any(b.text == "1" for b in result.blocks)

    # text_level marks a heading; its absence marks body text.
    assert result.blocks[0].label == "paragraph_title"
    assert result.blocks[0].text.startswith("广东省某中学")
    assert result.blocks[1].label == "paragraph_title"
    assert result.blocks[1].text == "第二部分 阅读理解"
    assert result.blocks[2].label == "text"

    # Order is preserved across pages, page 0 before page 1.
    assert result.blocks[-1].text == "第二页的正文。"
    assert result.blocks == sorted(result.blocks, key=lambda b: b.order)


def test_a_figure_carries_its_bytes_and_a_table_carries_its_html():
    result = mineru_ingest.parse_content_list(CONTENT_LIST, IMAGES)

    figure = next(b for b in result.blocks if b.label == "image")
    assert figure.image == b"\x89PNG-not-really"
    assert figure.text == ""

    table = next(b for b in result.blocks if b.label == "table")
    # The same HTML shape pdf_ingest._add_table already knows how to parse — the two
    # backends must not need two table parsers.
    assert table.text.startswith("<table>")


def test_a_figure_whose_bytes_are_missing_is_skipped_not_crashed():
    """A single undecodable figure must not sink the whole paper."""
    result = mineru_ingest.parse_content_list(
        [{"type": "image", "img_path": "images/gone.jpg", "page_idx": 0},
         {"type": "text", "text": "正文还在。", "page_idx": 0}],
        {},
    )
    assert [b.text for b in result.blocks] == ["正文还在。"]


def test_blocks_are_the_same_type_paddle_produces():
    """Both backends feed one blocks_to_docx, so they must speak one vocabulary."""
    result = mineru_ingest.parse_content_list(CONTENT_LIST, IMAGES)
    assert all(isinstance(b, OcrBlock) for b in result.blocks)

    import pdf_ingest

    # The labels MinerU emits must be ones pdf_ingest actually acts on, or a MinerU
    # heading would silently come out as body text.
    for block in result.blocks:
        assert block.label in pdf_ingest.HEADINGS | {"text", "table", "image"}, block.label


def test_the_poll_loop_can_be_cancelled():
    """取消 must reach a worker parked in MinerU's poll loop.

    The sleep is injected precisely so it can be routed through the pipeline's cancel
    event; a plain time.sleep here would make 取消 look broken for minutes.
    """
    import gaokao_english_docx_pipeline as pipeline

    calls: list[float] = []

    def sleeping(seconds: float) -> None:
        calls.append(seconds)
        raise pipeline.Cancelled("已取消")

    def never_done(path, token, *, payload=None, timeout=60):
        return {"code": 0, "data": {"extract_result": [{"state": "running"}]}}

    original = mineru_ingest._request
    mineru_ingest._request = never_done
    try:
        raised = False
        try:
            mineru_ingest._wait("batch-1", "tok", sleep=sleeping)
        except pipeline.Cancelled:
            raised = True
        assert raised, "a cancel during polling must propagate, not be swallowed"
        assert calls, "the loop must go through the injected sleep, not time.sleep"
    finally:
        mineru_ingest._request = original


def test_a_failed_parse_says_why():
    def failed(path, token, *, payload=None, timeout=60):
        return {"code": 0, "data": {"extract_result": [{"state": "failed", "err_msg": "文件损坏"}]}}

    original = mineru_ingest._request
    mineru_ingest._request = failed
    try:
        try:
            mineru_ingest._wait("batch-1", "tok", sleep=lambda _s: None)
            raise AssertionError("a failed parse must raise")
        except RuntimeError as exc:
            assert "文件损坏" in str(exc)
    finally:
        mineru_ingest._request = original
