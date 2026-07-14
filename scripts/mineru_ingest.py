#!/usr/bin/env python3
"""PDF input via MinerU's official cloud API.

The second OCR backend. It produces the same ``OcrBlock`` list PaddleOCR does, so both
land in ``pdf_ingest.blocks_to_docx`` and the rest of the pipeline cannot tell them
apart — a scanned paper becomes a real .docx and is then segmented, scored, cloned and
exported by exactly the same code as a Word input.

**MinerU is asynchronous, which PaddleOCR is not.** PaddleOCR is one blocking POST that
returns the layout. MinerU is four steps:

    POST /api/v4/file-urls/batch   → batch_id + a presigned upload URL per file
    PUT  <presigned url>           → the PDF bytes (and no Content-Type header — S3
                                     signs the request without one, and adding it is a
                                     403)
    GET  /api/v4/extract-results/batch/<batch_id>   → poll until state == done
    GET  <full_zip_url>            → a zip; the layout lives in content_list.json

So this file has a poll loop, and the loop has to be interruptible: without that, 取消
would appear to do nothing for as long as MinerU takes to parse a 12-page paper. The
sleep therefore goes through the pipeline's cancel event, not ``time.sleep``.

Token: https://mineru.net/apiManage/token → ``MINERU_TOKEN``. Unlike PaddleOCR there is
one global URL, so there is no service address to configure.

Limits: 200MB and 200 pages per file, which no exam paper comes near.
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from pdf_ingest import OcrBlock, OcrResult, blocks_to_docx

BASE_URL = "https://mineru.net"
TOKEN_ENV = "MINERU_TOKEN"

# "vlm" is the accurate model; "pipeline" is the fast, lighter one. An exam paper is
# dense two-column text with tables and the occasional figure — accuracy is the point.
MODEL_VERSION = "vlm"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0  # 15 minutes; a 12-page paper takes well under one

# MinerU labels a heading by depth (text_level 1, 2, …) rather than by name.
SKIP_TYPES = {"header", "footer", "page_number", "discarded"}


def _request(path: str, token: str, *, payload: dict | None = None, timeout: int = 60) -> dict:
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",  # PaddleOCR uses `token <t>`; MinerU wants Bearer
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))

    if body.get("code") not in (0, None):
        raise RuntimeError(f"MinerU 返回错误：{body.get('msg') or body.get('message') or body}")
    return body


def check_service(token: str, timeout: int = 20) -> tuple[bool, str]:
    """Is this token usable? Answered without uploading a real PDF.

    Asks to register a zero-byte file: the service authenticates first and only then
    looks at what was asked for, so a 401 and a "that file is no good" are different
    answers — and only the first one is the teacher's problem.
    """
    try:
        _request(
            "/api/v4/file-urls/batch",
            token,
            payload={"files": [{"name": "probe.pdf"}], "model_version": MODEL_VERSION},
            timeout=timeout,
        )
        return True, "令牌可用"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"令牌无效（HTTP {exc.code}）。请到 mineru.net/apiManage/token 核对。"
        if exc.code == 429:
            return False, "调用超限（HTTP 429），请稍后再试。"
        return False, f"MinerU 返回 HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"连不上 MinerU：{exc.reason}"
    except RuntimeError as exc:
        return False, str(exc)[:160]


def _upload(pdf: Path, token: str) -> str:
    """Register the file, PUT the bytes to the presigned URL, return the batch id."""
    body = _request(
        "/api/v4/file-urls/batch",
        token,
        payload={"files": [{"name": pdf.name}], "model_version": MODEL_VERSION},
    )
    data = body.get("data") or {}
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        raise RuntimeError(f"MinerU 没有返回上传地址：{str(body)[:200]}")

    # No Content-Type: the URL is signed without one, and sending it is a 403.
    put = urllib.request.Request(urls[0], data=pdf.read_bytes(), method="PUT")
    with urllib.request.urlopen(put, timeout=300) as response:
        if response.status >= 300:
            raise RuntimeError(f"上传 PDF 到 MinerU 失败（HTTP {response.status}）")
    return str(batch_id)


def _wait(batch_id: str, token: str, *, sleep) -> str:
    """Poll until the parse is done, and return the result zip's URL."""
    deadline = time.time() + POLL_TIMEOUT
    while True:
        body = _request(f"/api/v4/extract-results/batch/{batch_id}", token)
        results = (body.get("data") or {}).get("extract_result") or []
        if results:
            row = results[0]
            state = str(row.get("state") or "")
            if state == "done":
                url = row.get("full_zip_url")
                if not url:
                    raise RuntimeError("MinerU 说解析完成了，却没有给结果地址。")
                return str(url)
            if state == "failed":
                raise RuntimeError(f"MinerU 解析失败：{row.get('err_msg') or '未说明原因'}")

        if time.time() > deadline:
            raise RuntimeError(f"MinerU 解析超时（超过 {POLL_TIMEOUT / 60:.0f} 分钟）。")
        # Interruptible: a plain time.sleep here would make 取消 look broken for as long
        # as MinerU takes to parse the paper.
        sleep(POLL_INTERVAL)


def _download_content_list(zip_url: str) -> tuple[list, dict[str, bytes]]:
    """Fetch the result zip and pull out the layout plus any figures."""
    with urllib.request.urlopen(zip_url, timeout=300) as response:
        blob = response.read()

    images: dict[str, bytes] = {}
    content_list: list = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            lower = name.lower()
            if lower.endswith("content_list.json"):
                content_list = json.loads(archive.read(name).decode("utf-8"))
            elif lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                images[name] = archive.read(name)
                images[Path(name).name] = images[name]  # referenced either way

    if not content_list:
        raise RuntimeError("MinerU 的结果里没有 content_list.json，无法还原版面。")
    return content_list, images


def parse_content_list(content_list: list, images: dict[str, bytes]) -> OcrResult:
    """Flatten MinerU's content list into reading-ordered blocks.

    ``content_list.json`` is already in reading order, one entry per block, so the order
    is the index. A heading is marked by ``text_level`` rather than by a label — the
    presence of the key at all is what makes it a heading.
    """
    result = OcrResult()
    for index, item in enumerate(content_list):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "text")
        if kind in SKIP_TYPES:
            continue

        page = int(item.get("page_idx") or 0)
        order = page * 10_000 + index

        if kind in {"image", "figure"}:
            path = str(item.get("img_path") or "")
            blob = images.get(path) or images.get(Path(path).name) if path else None
            if blob:
                result.blocks.append(OcrBlock(label="image", text="", order=order, image=blob))
            continue

        if kind == "table":
            # An HTML table, exactly the shape pdf_ingest._add_table already parses.
            html = str(item.get("table_body") or "")
            if html.strip():
                result.blocks.append(OcrBlock(label="table", text=html, order=order))
            continue

        if kind == "equation":
            text = str(item.get("text") or "")
        else:
            text = str(item.get("text") or "")
        if not text.strip():
            continue

        label = "paragraph_title" if item.get("text_level") else "text"
        result.blocks.append(OcrBlock(label=label, text=text, order=order))
        result.markdown += text + "\n"

    result.blocks.sort(key=lambda b: b.order)
    return result


def ingest_pdf(pdf: Path, out_dir: Path, *, token: str = "", sleep=time.sleep) -> Path:
    """OCR a PDF with MinerU and write a .docx next to the other input papers."""
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SystemExit(
            f"缺少 MinerU 令牌。请在 https://mineru.net/apiManage/token 申请后设置环境变量 {TOKEN_ENV}。"
        )

    batch_id = _upload(pdf, token)
    zip_url = _wait(batch_id, token, sleep=sleep)
    content_list, images = _download_content_list(zip_url)
    result = parse_content_list(content_list, images)
    if not result.blocks:
        raise SystemExit(f"{pdf.name}: MinerU 未识别出任何内容。")
    return blocks_to_docx(result, out_dir / f"{pdf.stem}.docx")
