"""取消 has to stop the run, not schedule it to stop.

It used to be checked once, between stages. So pressing it during 生成逐题解析 — the stage
that takes minutes, and therefore the only one anybody wants to cancel — did nothing at
all until that stage finished on its own. Worse, a 429 could park a worker inside a 90
second ``time.sleep`` that nothing could wake.

The hard part is that the worker threads are blocked inside a socket read waiting on
DeepSeek. No flag can reach them there; the connection has to be closed underneath them.
That is what these tests pin down — against a real server that never answers, which is
exactly the situation being cancelled out of.
"""

from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gaokao_english_docx_pipeline as pipeline  # noqa: E402


class _NeverAnswers(http.server.BaseHTTPRequestHandler):
    """Accepts the request, then stalls — a model that is still thinking."""

    def do_POST(self):  # noqa: N802
        time.sleep(60)

    def log_message(self, *_args):
        pass


class _Server:
    def __init__(self):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _NeverAnswers)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()


def _call(port: int):
    return pipeline.call_chat_completion_http(
        "hi",
        base_url=f"http://127.0.0.1:{port}",
        api_key="k",
        model="deepseek-v4-flash",
        temperature=0.0,
        reasoning_effort="high",
        thinking="disabled",
        timeout=60,
        max_tokens=10,
        max_retries=3,
    )


def test_cancelling_hangs_up_on_a_request_that_is_still_running():
    pipeline.reset_cancel()
    with _Server() as server:
        outcome: list = []

        def worker():
            try:
                _call(server.port)
                outcome.append("returned")
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        started = time.time()
        thread.start()
        time.sleep(0.4)  # let it get as far as blocking on the response

        pipeline.request_cancel()
        thread.join(timeout=5)

        elapsed = time.time() - started
        assert not thread.is_alive(), "取消 left the worker stuck in the socket read"
        assert elapsed < 5, f"took {elapsed:.1f}s — the server would have stalled for 60"
        assert outcome and isinstance(outcome[0], pipeline.Cancelled), outcome
    pipeline.reset_cancel()


def test_the_retry_backoff_wakes_up_on_cancel():
    # retry_delay_seconds can return up to ~90s after a 429. time.sleep() could not be
    # interrupted, so 取消 pressed during a rate-limit backoff appeared to do nothing.
    pipeline.reset_cancel()
    outcome: list = []

    def worker():
        try:
            pipeline._sleep_or_cancel(60)
            outcome.append("slept the whole way")
        except pipeline.Cancelled:
            outcome.append("woke")

    thread = threading.Thread(target=worker, daemon=True)
    started = time.time()
    thread.start()
    time.sleep(0.2)
    pipeline.request_cancel()
    thread.join(timeout=3)

    assert outcome == ["woke"]
    assert time.time() - started < 3
    pipeline.reset_cancel()


def test_a_cancelled_run_refuses_to_start_new_work():
    pipeline.reset_cancel()
    pipeline.request_cancel()
    try:
        pipeline.raise_if_cancelled()
    except pipeline.Cancelled:
        pass
    else:
        raise AssertionError("every worker checks this before taking the next item")
    pipeline.reset_cancel()
    pipeline.raise_if_cancelled()  # and a fresh run is not poisoned by the last one


def test_the_gui_and_the_pipeline_share_one_flag():
    # The GUI owns a threading.Event for its own stage loop; the pipeline has to check
    # that same object, or the two disagree about whether a cancel happened.
    event = threading.Event()
    pipeline.set_cancel_event(event)
    try:
        event.set()
        try:
            pipeline.raise_if_cancelled()
        except pipeline.Cancelled:
            pass
        else:
            raise AssertionError("the pipeline is watching a different flag than the UI")
    finally:
        pipeline.set_cancel_event(threading.Event())
        pipeline.reset_cancel()
