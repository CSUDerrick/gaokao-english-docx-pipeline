#!/usr/bin/env python3
"""Native macOS front end.

The Streamlit GUI shelled out to ``python3 scripts/...``. That cannot work inside
a packaged .app, which has no interpreter on PATH — so the pipeline is imported
and called in-process here, on a worker thread so the window stays responsive.

API keys go to the macOS Keychain rather than a plaintext file under .local/.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QButtonGroup,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "英语试卷整理工具"
VERSION = "0.8.0"
REPO = "CSUDerrick/gaokao-english-docx-pipeline"

# The tool outgrew 高三 — the same pipeline handles junior-high papers — so the app and
# its folder lost that prefix. The Keychain service id deliberately did not: renaming it
# would orphan the API keys already saved under the old name.
LEGACY_DATA_DIR = "高三英语试卷整理"
DATA_DIR = "英语试卷整理"


def _data_home() -> Path:
    """Where a packaged build keeps settings, papers and outputs.

    Renaming the app must not lose the teacher's data, so an existing folder under the
    old name is moved rather than abandoned. If both exist (she has already run the new
    build), the old one is left alone — merging them is not something to guess at.
    """
    documents = Path.home() / "Documents"
    home, legacy = documents / DATA_DIR, documents / LEGACY_DATA_DIR
    if legacy.is_dir() and not home.exists():
        with contextlib.suppress(OSError):
            legacy.rename(home)
    return home


if getattr(sys, "frozen", False):
    BUNDLE = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    HOME = _data_home()
else:
    BUNDLE = Path(__file__).resolve().parents[1]
    HOME = BUNDLE

sys.path.insert(0, str(BUNDLE / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import net_tls  # noqa: E402

# Before anything opens a TLS connection. On a Mac whose network re-signs HTTPS (a school
# proxy, antivirus, a VPN), Python does not see the interception root that macOS already
# trusts, and every call dies with "self-signed certificate in certificate chain" — which
# is what stopped the app dead on a teacher's new machine.
net_tls.install()

from run_timing import Timings, format_duration  # noqa: E402
from settings import (  # noqa: E402
    CUSTOM,
    EFFORT_LABELS,
    PRESET_LABELS,
    QUALITY,
    SPEED,
    STAGES,
    Settings,
    default_segment_model,
    detect_preset,
    efforts_for,
    models_for,
    normalize_effort,
    preset_values,
    pv,
)

KEYCHAIN_SERVICE = "gaokao-english-docx-pipeline"
PAPER_SUFFIXES = {".docx", ".pdf"}

# One Keychain entry per provider, so switching APIs does not make you retype the key you
# already gave — and so two providers' keys never overwrite each other.
def key_account(provider: str) -> str:
    return pv.get(provider).api_key_env


# --------------------------------------------------------------------------- keychain


def keychain_get(account: str) -> str:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def keychain_set(account: str, secret: str) -> None:
    if not secret:
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", secret],
            capture_output=True, timeout=5,
        )


def _elide(text: str, limit: int) -> str:
    """Trim to fit, with an ellipsis. `name[:16]` just lost the tail silently."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_api(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    insecure_ssl: bool = False,
) -> tuple[bool, str]:
    """Send the smallest possible request, so a bad key fails now and not in ten minutes.

    Runs the real client against the real endpoint, in whatever protocol this provider
    speaks: a key that authenticates here is a key the pipeline can use.

    The failure message matters as much as the check. A teacher on a new Mac saw only
    ``<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ... self-signed certificate in
    certificate chain>`` — which is not about her key at all, and gave her nothing to
    act on. Certificate failures are now told apart and explained.
    """
    import gaokao_english_docx_pipeline as pipeline
    import net_tls

    spec = pv.get(provider)
    started = time.time()
    try:
        pipeline.call_chat_completion(
            "hi",
            provider=provider,
            base_url=base_url or spec.base_url,
            api_key=api_key,
            model=model,
            temperature=0.0,
            client_mode="http",
            reasoning_effort="",
            # Thinking off for the probe: we are testing the key, and a reasoning model
            # asked to think would spend its one allowed token doing so.
            thinking="disabled",
            timeout=20,
            max_tokens=1,
            max_retries=1,  # a test must report the failure, not sit there retrying it
            insecure_ssl=insecure_ssl,
        )
    except Exception as exc:  # noqa: BLE001 — every failure mode is a message to show
        if net_tls.is_certificate_error(exc) or "证书" in str(exc):
            return False, net_tls.CERTIFICATE_HELP
        text = str(exc)
        if "401" in text or "403" in text or "Authentication" in text:
            return False, f"{spec.label} 密钥无效（HTTP 401/403）。请核对后重试。"
        if "404" in text:
            return False, f"找不到模型「{model}」或地址不对。可以点「刷新模型列表」看这个 Key 能用哪些模型。"
        return False, text.splitlines()[0][:200]
    return True, f"{spec.label} · {model}，{time.time() - started:.1f}s"


def list_models(provider: str, api_key: str, base_url: str, insecure_ssl: bool = False) -> list[str]:
    """Ask the endpoint which models this key can actually use.

    The curated table in providers.py is a starting point, not the truth: model ids churn
    (GLM 4.6 and 4.7 were delisted in July 2026), and a teacher on a provider we have not
    verified needs to see real names rather than our guesses. Every OpenAI-compatible
    vendor serves GET /v1/models; Anthropic does too.
    """
    import json as _json
    import urllib.parse

    import gaokao_english_docx_pipeline as pipeline

    spec = pv.get(provider)
    root = (base_url or spec.base_url).rstrip("/")
    url = f"{root}/models" if spec.protocol != pv.ANTHROPIC else f"{root}/v1/models"
    parts = urllib.parse.urlsplit(url)

    connection = pipeline._open_connection(parts, timeout=20, insecure_ssl=insecure_ssl)
    try:
        connection.request("GET", parts.path, headers=pipeline.auth_headers(provider, api_key))
        response = connection.getresponse()
        body = _json.loads(response.read().decode("utf-8"))
    finally:
        pipeline._close_connection(connection)

    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"{spec.label} 没有返回模型列表：{str(body)[:200]}")
    return sorted(str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id"))


class _Check(QObject):
    """Runs one API check off the UI thread."""

    finished = Signal(bool, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        ok, message = self.fn()
        self.finished.emit(ok, message)


class KeysDialog(QDialog):
    """The API keys, in a window of their own.

    They used to live in the main window, and they are the wrong shape for it: a key is
    typed once and then never looked at again, but the two PaddleOCR fields sat there
    for good, and when the group was expanded it needed 395px in a pane that only ever
    got 340 — so the PDF row was cut in half and the panel collided with the tabs below.

    A dialog gives the space back to the things a run is actually driven from, and lets
    the fields be as tall as they like.
    """

    def __init__(self, parent, cfg: Settings):
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.setMinimumWidth(600)
        self.cfg = cfg
        self.insecure_ssl = cfg.insecure_ssl
        self._check: _Check | None = None
        # Edits are held here and only written to the Keychain on 保存, so switching the
        # provider dropdown back and forth cannot lose a key you just typed.
        self._keys: dict[str, str] = {}

        form = QFormLayout()

        self.provider = QComboBox()
        for pid in pv.PROVIDER_ORDER:
            self.provider.addItem(pv.get(pid).label, pid)
        self.provider.setCurrentIndex(max(0, self.provider.findData(cfg.provider)))
        self.provider.activated.connect(self._provider_changed)
        form.addRow("服务商", self.provider)

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.textEdited.connect(self._remember_key)
        self.api_test = QPushButton("测试")
        self.api_test.clicked.connect(self.test_api)
        form.addRow("API Key", self._with_button(self.api_key, self.api_test))

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("留空则用该服务商的默认地址")
        self.refresh = QPushButton("刷新模型列表")
        self.refresh.clicked.connect(self.refresh_models)
        form.addRow("接口地址", self._with_button(self.base_url, self.refresh))

        # PDF input. These used to appear *only* when the input folder already held a
        # PDF — i.e. they were hidden at exactly the moment a teacher would want to set
        # them up in advance.
        self.pdf_backend = QComboBox()
        self.pdf_backend.addItem("PaddleOCR-VL（百度 AI Studio）", "paddle")
        self.pdf_backend.addItem("MinerU（官方云 API）", "mineru")
        self.pdf_backend.setCurrentIndex(max(0, self.pdf_backend.findData(cfg.pdf_backend)))
        self.pdf_backend.activated.connect(self._pdf_backend_changed)
        form.addRow("PDF 识别", self.pdf_backend)

        self.paddle_token = QLineEdit(keychain_get("PADDLEOCR_ACCESS_TOKEN"))
        self.paddle_token.setEchoMode(QLineEdit.Password)
        self.paddle_token.setPlaceholderText("只有要处理 PDF 试卷时才需要")
        self.paddle_url = QLineEdit(keychain_get("PADDLEOCR_BASE_URL"))
        self.paddle_url.setPlaceholderText("服务地址（aistudio.baidu.com/paddleocr/task 查看）")
        self.paddle_test = QPushButton("测试")
        self.paddle_test.clicked.connect(self.test_paddle)
        self.paddle_token_row = self._row("PaddleOCR 令牌", self.paddle_token)
        self.paddle_url_row = self._row("PaddleOCR 地址", self._with_button(self.paddle_url, self.paddle_test))
        form.addRow(*self.paddle_token_row)
        form.addRow(*self.paddle_url_row)

        self.mineru_token = QLineEdit(keychain_get("MINERU_TOKEN"))
        self.mineru_token.setEchoMode(QLineEdit.Password)
        self.mineru_token.setPlaceholderText("mineru.net/apiManage/token 申请")
        self.mineru_test = QPushButton("测试")
        self.mineru_test.clicked.connect(self.test_mineru)
        self.mineru_row = self._row("MinerU 令牌", self._with_button(self.mineru_token, self.mineru_test))
        form.addRow(*self.mineru_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#5570a0")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(QLabel("密钥保存在 macOS 钥匙串，不会写进任何文件。"))
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self._load_provider(cfg.provider)
        self._pdf_backend_changed()

    @staticmethod
    def _with_button(field: QWidget, button: QPushButton) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field, 1)
        row.addWidget(button)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    @staticmethod
    def _row(label: str, widget: QWidget) -> tuple[QLabel, QWidget]:
        return QLabel(label), widget

    # --- provider

    def current_provider(self) -> str:
        return str(self.provider.currentData())

    def _remember_key(self, text: str) -> None:
        self._keys[key_account(self.current_provider())] = text.strip()

    def _load_provider(self, provider: str) -> None:
        """Show the key and URL belonging to this provider."""
        spec = pv.get(provider)
        account = key_account(provider)
        if account not in self._keys:
            self._keys[account] = keychain_get(account)
        self.api_key.setText(self._keys[account])
        self.api_key.setPlaceholderText(f"{account}（{spec.label} 的密钥）")
        self.base_url.setText(
            self.cfg.base_url if provider == self.cfg.provider and self.cfg.base_url else spec.base_url
        )
        self.status.setText(spec.note or "")

    def _provider_changed(self) -> None:
        self._load_provider(self.current_provider())

    def _pdf_backend_changed(self) -> None:
        """Only show the OCR service that is actually selected."""
        mineru = self.pdf_backend.currentData() == "mineru"
        for label, widget in (self.paddle_token_row, self.paddle_url_row):
            label.setVisible(not mineru)
            widget.setVisible(not mineru)
        self.mineru_row[0].setVisible(mineru)
        self.mineru_row[1].setVisible(mineru)

    def keys(self) -> dict[str, str]:
        keys = dict(self._keys)
        keys["PADDLEOCR_ACCESS_TOKEN"] = self.paddle_token.text().strip()
        keys["PADDLEOCR_BASE_URL"] = self.paddle_url.text().strip()
        keys["MINERU_TOKEN"] = self.mineru_token.text().strip()
        return keys

    def accept(self) -> None:
        self._remember_key(self.api_key.text())
        for name, value in self.keys().items():
            keychain_set(name, value)
        super().accept()

    # --- checks. A wrong key, or a network that intercepts HTTPS, used to surface as a
    # --- failed run ten minutes in.

    def _run_check(self, button: QPushButton, fn) -> None:
        button.setEnabled(False)
        self.status.setText("正在测试…")

        thread = QThread(self)
        check = _Check(fn)
        check.moveToThread(thread)
        thread.started.connect(check.run)

        def finished(ok: bool, message: str) -> None:
            self.status.setText(f"✅ 可用（{message}）" if ok else f"❌ {message}")
            button.setEnabled(True)
            thread.quit()

        check.finished.connect(finished)
        thread.finished.connect(thread.deleteLater)
        self._check = check  # keep it alive until the thread finishes
        thread.start()

    def test_api(self) -> None:
        provider, key = self.current_provider(), self.api_key.text().strip()
        url = self.base_url.text().strip()
        if not key:
            self.status.setText(f"❌ 先填 {pv.get(provider).label} 的 API Key")
            return
        # Whatever the preset would run at 质量优先: testing a model nobody will use is
        # a test that can pass while the real run 404s.
        model = preset_values(QUALITY, provider).get("explain_model", "")
        if not model:
            self.status.setText("❌ 这个服务商还没有配模型，请先点「刷新模型列表」并在进阶模式里选一个。")
            return
        self._run_check(self.api_test, lambda: check_api(provider, key, url, model, self.insecure_ssl))

    def refresh_models(self) -> None:
        provider, key = self.current_provider(), self.api_key.text().strip()
        url = self.base_url.text().strip()
        if not key:
            self.status.setText("❌ 先填 API Key，才能问服务器它支持哪些模型")
            return

        def check() -> tuple[bool, str]:
            try:
                models = list_models(provider, key, url, self.insecure_ssl)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc).splitlines()[0][:180]
            if not models:
                return False, "服务器没有返回任何模型"
            return True, f"{len(models)} 个模型：{'、'.join(models[:8])}{' …' if len(models) > 8 else ''}"

        self._run_check(self.refresh, check)

    def test_paddle(self) -> None:
        token, url = self.paddle_token.text().strip(), self.paddle_url.text().strip()
        if not (token and url):
            self.status.setText("❌ PaddleOCR 需要同时填令牌和地址（只有处理 PDF 才用得上）")
            return

        def check() -> tuple[bool, str]:
            import pdf_ingest

            try:
                return pdf_ingest.check_service(url, token)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc).splitlines()[0][:160]

        self._run_check(self.paddle_test, check)

    def test_mineru(self) -> None:
        token = self.mineru_token.text().strip()
        if not token:
            self.status.setText("❌ 先填 MinerU 令牌（只有处理 PDF 才用得上）")
            return

        def check() -> tuple[bool, str]:
            import mineru_ingest

            try:
                return mineru_ingest.check_service(token)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc).splitlines()[0][:160]

        self._run_check(self.mineru_test, check)


def papers_in(folder: Path) -> list[Path]:
    if not folder or not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in PAPER_SUFFIXES and not p.name.startswith("~$")
    )


# --------------------------------------------------------------------------- drop zone


class DropZone(QLabel):
    """Drag papers straight onto the window instead of hunting through a dialog."""

    dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        # Capped, not just floored: this is a hint, and the space it used to take was
        # coming out of the API-key box below it, which got clipped off the screen.
        self.setMinimumHeight(56)
        self.setMaximumHeight(64)
        self._idle()

    def _style(self, border: str, bg: str, fg: str) -> None:
        self.setStyleSheet(
            f"QLabel{{border:2px dashed {border};border-radius:10px;"
            f"background:{bg};color:{fg};font-size:13px;padding:6px}}"
        )

    def _idle(self) -> None:
        self._style("#b6c6e3", "#f6f9ff", "#5570a0")

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._style("#2563eb", "#e8f0ff", "#2563eb")

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._idle()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._idle()
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()


# --------------------------------------------------------------------------- worker


# In 基础模式 the log is a progress report, not a trace: stage boundaries, counts,
# warnings, errors and where the files went. 调试模式 shows everything.
_IMPORTANT = re.compile(
    r"Pipeline (started|finished)|Segmenting|Scoring|Enriching|Extracting|Selected|"
    r"Selection outputs|segmented |wrote |prompt cache|本次|跳过|"
    r"WARN|ERROR|错误|失败|截断|Traceback|Error|Exception",
    re.IGNORECASE,
)
_PROGRESS = re.compile(r"\((\d+)\s*/\s*(\d+)\)")


def is_important(line: str) -> bool:
    return bool(_IMPORTANT.search(line))


def line_fraction(line: str) -> float | None:
    """Read "(3/18)" out of a log line as a 0..1 fraction of the current stage."""
    match = _PROGRESS.search(line)
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    return min(1.0, done / total) if total else None


STAGE_LABELS = {
    "segment": "切分题目", "score": "AI 评分", "select": "自动选题",
    "review-select": "AI 复核选题", "explain": "生成逐题解析",
    "vocab": "提取重难点词汇", "assemble": "汇总",
    "repair-answers": "修复答案", "export-docx": "导出 Word",
}

_TAIL = ["explain", "vocab", "assemble", "repair-answers", "export-docx"]


@dataclass
class Job:
    """One press of a button: which stages run, and which of them is being redone.

    ``force`` names the single stage the teacher asked to regenerate. The stages
    *after* it deliberately do not get ``--force``: their per-item caches then make
    the catch-up nearly free — re-selecting only pays the API for the questions that
    are actually new.

    ``init`` wipes the output directory and so belongs to a full run only. It used to
    be passed on every 开始整理 (the pipeline's `--init` is a `shutil.rmtree`), which is
    exactly why re-running one stage was never possible.
    """

    name: str
    stages: list[str]
    force: str = ""
    init: bool = False
    reselect: bool = False


def full_run(review_select: bool) -> Job:
    stages = ["segment", "score", "select"]
    if review_select:
        stages.append("review-select")
    return Job("整轮", stages + _TAIL, init=True)


def rerun_jobs(review_select: bool) -> dict[str, Job]:
    """The 分步重跑 buttons. Each one carries its downstream along with it.

    Re-selecting used to leave the run in a state that exported silently-broken
    files — the new question had no explanation and the word list was the previous
    selection's — so every job that changes *which* questions are in play must drag
    explain/vocab/export behind it. The pipeline's export gate now refuses the job
    if it doesn't.
    """
    after_select = ["review-select", *_TAIL] if review_select else _TAIL
    resegment = full_run(review_select)
    resegment.name = "重新切分"  # segmentation changes everything below it: it *is* a full run
    return {
        "resegment": resegment,
        "rescore": Job("重新评分", ["score", "select", *after_select], force="score"),
        # The local ranking is deterministic — re-running `select` returns the same
        # questions. Only the AI review can actually choose differently, and only if
        # it is told the current picks were rejected.
        "reselect": Job("重新选题", ["review-select", *_TAIL], reselect=True),
        "reexplain": Job("重生解析", ["explain", "assemble", "export-docx"], force="explain"),
        "revocab": Job("重生词汇", ["vocab", "export-docx"], force="vocab"),
        "reexport": Job("重新导出 Word", ["export-docx"]),
    }


# Stages that talk to DeepSeek. Cancelling one of these throws away work that was paid
# for, so it asks first; cancelling a local stage just stops.
API_STAGES = {"score", "review-select", "explain", "vocab"}

CANCEL_WARNING = (
    "正在调用 AI。取消会**立刻断开**正在进行的请求。\n\n"
    "· 已经生成好的题目会保留，重跑时不用再生成一遍。\n"
    "· 正在生成的那几道题会作废。\n"
    "· 已经发出去的请求可能仍然计入用量——DeepSeek 没有「停止生成」这个接口，"
    "非流式请求只能靠断开连接来中止。\n\n"
    "确定取消吗？"
).replace("**", "")


def stage_config(cfg: Settings, stage: str) -> tuple[str, str, str]:
    """(model, effort, thinking) for a stage — the key the time estimate is stored under."""
    if stage == "segment":
        return (cfg.segment_model, "none", "disabled")
    if stage == "score":
        return (cfg.score_model, cfg.score_effort, cfg.score_thinking)
    if stage == "review-select":
        return (cfg.review_model, cfg.review_effort, cfg.review_thinking)
    if stage == "explain":
        return (cfg.explain_model, cfg.explain_effort, cfg.explain_thinking)
    if stage == "vocab":
        return (cfg.vocab_model, cfg.vocab_effort, cfg.vocab_thinking)
    return ("local", "none", "disabled")


class Worker(QObject):
    line = Signal(str)
    plan = Signal(list)          # [(mode, label, estimated_seconds)]
    step = Signal(int, float)    # stage index, fraction within it
    stage_done = Signal(int)
    done = Signal(bool, str)

    def __init__(self, input_dir: Path, out_dir: Path, keys: dict[str, str], cfg: Settings, job: Job):
        super().__init__()
        self.input_dir, self.out_dir, self.keys, self.cfg = input_dir, out_dir, keys, cfg
        self.job = job
        self.timings = Timings(HOME / "timings.json")
        self._stop = threading.Event()

    def cancel(self) -> None:
        """Stop now, not at the next stage boundary.

        The flag alone is not enough: the worker threads are parked inside a socket read
        waiting on DeepSeek, and nothing can signal them there. ``request_cancel`` also
        hangs up on every connection in flight, which makes those reads raise at once.
        """
        import gaokao_english_docx_pipeline as pipeline

        self._stop.set()
        pipeline.request_cancel()

    def units(self, stage: str) -> int:
        """How much work a stage faces, read off whatever is already on disk.

        Papers for segmentation, questions for the per-item stages. Read fresh before
        each stage rather than up front, because the counts only exist once the stage
        before has produced them.
        """
        if stage == "segment":
            return max(1, len(papers_in(self.input_dir)))
        if stage == "score":
            index = self.out_dir / "segment_index.jsonl"
            if index.exists():
                return max(1, sum(1 for line in index.read_text(encoding="utf-8").splitlines() if line.strip()))
            return max(1, len(papers_in(self.input_dir)) * 9)  # 9 sections per paper
        if stage == "explain":
            selection = self.out_dir / "selected_items.json"
            if selection.exists():
                try:
                    return max(1, len(json.loads(selection.read_text(encoding="utf-8"))))
                except (json.JSONDecodeError, OSError):
                    pass
            return 18
        if stage == "vocab":
            # The unit of work depends on which handout the teacher asked for: 完整 is one
            # call per question, 困难 is one per paper. Counting the wrong one leaves the
            # progress bar waiting for eighteen units and receiving three.
            selection = self.out_dir / "selected_items.json"
            if selection.exists():
                try:
                    rows = json.loads(selection.read_text(encoding="utf-8"))
                    if self.cfg.vocab_mode == "chunked":
                        return max(1, len(rows))
                    papers = {str(r.get("source_doc", "")) for r in rows if r.get("source_doc")}
                    return max(1, len(papers))
                except (json.JSONDecodeError, OSError):
                    pass
            return 18 if self.cfg.vocab_mode == "chunked" else max(1, len(papers_in(self.input_dir)))
        if stage == "review-select":
            return 9  # one call per section
        return 1

    class _Tee(io.TextIOBase):
        """Route the pipeline's stdout into the log box, at the chosen verbosity.

        The filter lives here rather than in the pipeline because the pipeline has
        a hundred-odd log() calls and only the GUI knows who is reading them.
        """

        def __init__(self, emit, verbose: bool):
            self.emit = emit
            self.verbose = verbose

        def write(self, text):  # noqa: D102
            for chunk in str(text).splitlines():
                if chunk.strip() and (self.verbose or is_important(chunk)):
                    self.emit(chunk)
            return len(text)

    def _argv(self, mode: str) -> list[str]:
        cfg = self.cfg
        argv = [
            str(self.input_dir), "--out", str(self.out_dir), "--mode", mode,
            "--client", "http", "--segment-input", "local",
            # Without --provider the pipeline would default to DeepSeek no matter what
            # the teacher picked, and send DeepSeek's fields to somebody else's API.
            "--provider", cfg.provider,
            "--pdf-backend", cfg.pdf_backend,
            # Without this the teacher's 完整/困难 choice would stay in the UI and the run
            # would quietly use the pipeline default — a switch that does not reach the
            # pipeline is decoration, which is exactly how --*-thinking once failed.
            "--vocab-mode", cfg.vocab_mode,
            "--segment-model", cfg.segment_model,
            "--score-model", cfg.score_model,
            "--enrich-model", cfg.enrich_model,
            "--review-model", cfg.review_model,
            "--explain-model", cfg.explain_model,
            "--vocab-model", cfg.vocab_model,
            "--score-reasoning-effort", cfg.score_effort,
            "--enrich-reasoning-effort", cfg.enrich_effort,
            "--review-reasoning-effort", cfg.review_effort,
            "--explain-reasoning-effort", cfg.explain_effort,
            "--vocab-reasoning-effort", cfg.vocab_effort,
            # Without these the UI's thinking choice never reached the model:
            # score/enrich silently ran with thinking off, review with it on.
            "--score-thinking", cfg.score_thinking,
            "--enrich-thinking", cfg.enrich_thinking,
            "--review-thinking", cfg.review_thinking,
            "--explain-thinking", cfg.explain_thinking,
            "--vocab-thinking", cfg.vocab_thinking,
            "--segment-workers", str(cfg.workers),
            "--score-workers", str(cfg.workers),
            "--enrich-workers", str(cfg.workers),
            "--max-retries", "12",
        ]
        if cfg.base_url:
            argv += ["--base-url", cfg.base_url]
        if cfg.keep_intermediates:
            argv.append("--save-conversations")
        else:
            argv.append("--no-save-conversations")
        if cfg.insecure_ssl:
            argv.append("--insecure-ssl")

        # --init is a shutil.rmtree of the output directory. Only a full run may ask
        # for it; a 分步重跑 that wiped the other stages' work would defeat the point.
        if self.job.init and mode == "segment":
            argv.append("--init")
        if self.job.force == mode:
            argv.append("--force")
        if self.job.reselect and mode == "review-select":
            argv.append("--reselect")
        return argv

    def run(self) -> None:
        for name, value in self.keys.items():
            if value:
                os.environ[name] = value

        import gaokao_english_docx_pipeline as pipeline

        # One flag drives both sides: the loop here between stages, and the pipeline's
        # own checks between papers, questions and turns — and its socket teardown.
        pipeline.set_cancel_event(self._stop)
        pipeline.reset_cancel()

        stages = self.job.stages
        try:
            # Published before anything runs, so the bar can weight each stage by how
            # long it will actually take. Weighting them equally put the sub-second
            # local segmentation and the multi-minute explanation at 1/9 each.
            plan = []
            for mode in stages:
                model, effort, thinking = stage_config(self.cfg, mode)
                plan.append((mode, STAGE_LABELS.get(mode, mode),
                             self.timings.estimate(mode, self.units(mode), model, effort, thinking)))
            self.plan.emit(plan)

            for i, mode in enumerate(stages):
                if self._stop.is_set():
                    self.done.emit(False, "已取消")
                    return
                self.step.emit(i, 0.0)

                def observe(line: str, i=i) -> None:
                    # The stages log "(3/18)" as they go; that is the only handle
                    # on progress *inside* a stage, which is where the minutes are.
                    fraction = line_fraction(line)
                    if fraction is not None:
                        self.step.emit(i, fraction)
                    self.line.emit(line)

                started = time.time()
                tee = self._Tee(observe, self.cfg.verbose)
                with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                    pipeline.main(self._argv(mode))

                model, effort, thinking = stage_config(self.cfg, mode)
                self.timings.record(mode, self.units(mode), model, effort, thinking, time.time() - started)
                self.stage_done.emit(i)

            self.done.emit(True, str(self.out_dir / "docx_exports"))
        except pipeline.Cancelled:
            # Not an error: the teacher asked. Everything already written to disk stays,
            # and the per-item caches mean a re-run does not pay for it twice.
            self.done.emit(False, "已取消（已生成的结果都保留了，重跑时不会重复生成）")
        except SystemExit as exc:
            self.done.emit(False, str(exc) or "流程中止")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, f"{type(exc).__name__}: {exc}")
        finally:
            pipeline.reset_cancel()
            # Saved even when a later stage failed or the run was cancelled: the stages
            # that *did* finish were really measured, and the next run — which is very
            # likely a retry — is exactly when a good estimate is worth the most.
            self.timings.save()


# --------------------------------------------------------------------------- window


class Window(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{VERSION}")
        # A fixed 940x780 was taller than a laptop screen once the menu bar and the
        # Dock are taken out, so the 「开始整理」 row sat *under* the Dock and the
        # window had to be maximised before the buttons could be clicked.
        available = QGuiApplication.primaryScreen().availableGeometry()
        # 880, not 780: the 分步重跑 bar is another 90px, and at 780 the whole ② API
        # 密钥 group box was pushed off the bottom of its scroll pane.
        self.resize(min(960, available.width() - 40), min(880, available.height() - 40))
        self.setMinimumSize(760, 520)

        self.cfg = Settings.load(HOME / "settings.json")
        self.input_dir = Path(self.cfg.input_dir) if self.cfg.input_dir else HOME / "input_docx"
        self.output_dir = Path(self.cfg.output_dir) if self.cfg.output_dir else HOME / "outputs" / "gaokao_english"
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.started_at: float | None = None
        self.plan: list[tuple[str, str, float]] = []
        self.stage_index = 0
        self.stage_fraction = 0.0

        # The ETA has to keep counting down between log lines: a stage that takes two
        # minutes can go a long while without printing anything, and a frozen bar
        # reads as a hung app.
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)

        # Folders and keys are set once and then ignored, so they are the part that
        # may scroll. The mode tabs, the action bar, progress, cost and the log are
        # what a run is actually driven from — they stay put at any window size.
        config = QVBoxLayout()
        config.setContentsMargins(0, 0, 0, 0)
        config.addWidget(self._folders_box())
        config.addStretch(1)
        config_panel = QWidget()
        config_panel.setLayout(config)

        scroll = QScrollArea()
        scroll.setWidget(config_panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        # Tall enough for BOTH group boxes. At 170 the drop zone was squeezed to its
        # floor and 「已找到 N 份试卷」 was cut off mid-sentence — which is the bug the
        # comment here used to *claim* it had fixed — and ② was scrolled out entirely.
        # Everything below is sized so the sum of the minimums stays under the window:
        # once they exceed it, Qt squeezes whatever it likes and the clipping is back.
        scroll.setMinimumHeight(310)
        self.config_scroll = scroll

        root = QVBoxLayout()
        root.addWidget(scroll, 3)
        root.addWidget(self._modes(), 2)
        root.addLayout(self._actions())
        root.addWidget(self._rerun_bar())

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("就绪")
        root.addWidget(self.progress)

        self.cost = QLabel("")
        self.cost.setStyleSheet("color:#5570a0")
        root.addWidget(self.cost)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        self.log.setMaximumBlockCount(5000)
        self.log.setMinimumHeight(70)
        root.addWidget(self.log, 1)

        footer = QHBoxLayout()
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#888")
        update = QPushButton("检查更新")
        update.clicked.connect(self.check_update)
        footer.addWidget(self.hint, 1)
        footer.addWidget(update)
        root.addLayout(footer)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.refresh_papers()
        if not self.cfg.seen_intro:
            self.show_intro()

    # --- ui pieces

    def _folders_box(self) -> QGroupBox:
        box = QGroupBox("① 试卷与输出")
        col = QVBoxLayout()

        self.drop = DropZone()
        self.drop.dropped.connect(self.on_dropped)
        col.addWidget(self.drop)

        # The list of found papers used to be *inside* the drop zone, which is a
        # fixed-height QLabel with no word wrap: four lines of text in a 64px box
        # inside a starved scroll area, so 「已找到 N 份试卷」 was cut off. It is its
        # own wrapping label now, and the drop zone says only one thing.
        self.papers_label = QLabel("")
        self.papers_label.setWordWrap(True)
        self.papers_label.setStyleSheet("color:#2f6f3e")
        col.addWidget(self.papers_label)

        row = QHBoxLayout()
        self.input_label = QLabel(str(self.input_dir))
        self.input_label.setStyleSheet("color:#555")
        self.input_label.setWordWrap(True)
        pick_in = QPushButton("选择…")
        pick_in.clicked.connect(self.pick_input)
        open_in = QPushButton("打开文件夹")
        open_in.clicked.connect(lambda: self._open(self.input_dir))
        row.addWidget(QLabel("试卷："))
        row.addWidget(self.input_label, 1)
        row.addWidget(pick_in)
        row.addWidget(open_in)
        col.addLayout(row)

        row = QHBoxLayout()
        self.output_label = QLabel(str(self.output_dir))
        self.output_label.setStyleSheet("color:#555")
        self.output_label.setWordWrap(True)
        pick_out = QPushButton("选择…")
        pick_out.clicked.connect(self.pick_output)
        open_out = QPushButton("打开文件夹")
        open_out.clicked.connect(lambda: self._open(self.output_dir))
        row.addWidget(QLabel("输出："))
        row.addWidget(self.output_label, 1)
        row.addWidget(pick_out)
        row.addWidget(open_out)
        col.addLayout(row)

        col.addWidget(self._keys_row())

        box.setLayout(col)
        return box

    def _keys_row(self) -> QWidget:
        """One button. The keys live in a dialog now — see KeysDialog."""
        self.keys_label = QLabel("")
        self.keys_label.setStyleSheet("color:#5570a0")
        button = QPushButton("API 密钥…")
        button.clicked.connect(self.edit_keys)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button)
        row.addWidget(self.keys_label, 1)
        holder = QWidget()
        holder.setLayout(row)
        self._refresh_keys_label()
        return holder

    def _refresh_keys_label(self) -> None:
        label = pv.get(self.cfg.provider).label
        saved = keychain_get(key_account(self.cfg.provider))
        self.keys_label.setText(f"{label} 已配置" if saved else f"还没填 {label} 密钥")

    def edit_keys(self) -> None:
        self.cfg.insecure_ssl = self.w_insecure.isChecked()
        dialog = KeysDialog(self, self.cfg)
        if dialog.exec():
            provider = dialog.current_provider()
            url = dialog.base_url.text().strip()
            backend = str(dialog.pdf_backend.currentData())
            if provider != self.cfg.provider:
                # Switching API re-derives every model name from the new provider, then
                # rebuilds the dropdowns. Keeping the old names would send DeepSeek's
                # models to Claude and 404 the run ten minutes in.
                self.cfg.apply_provider(provider)
                self._rebuild_for_provider()
            # An empty box means "use this provider's default", not "no URL".
            self.cfg.base_url = "" if url == pv.get(provider).base_url else url
            self.cfg.pdf_backend = backend
            self.cfg.save()
            self._refresh_summary()
        self._refresh_keys_label()

    def _rerun_bar(self) -> QWidget:
        """Redo one part without paying for the rest.

        Every button carries its downstream stages with it, because a selection change
        that is not followed through leaves the teacher edition missing a question's
        explanation and the handout listing the previous selection's words. The
        already-generated questions come back from cache, so the catch-up is nearly free.
        """
        box = QGroupBox("分步重跑（只重做你不满意的那一块，其余直接复用已有结果）")
        grid = QHBoxLayout()
        specs = [
            ("resegment", "重新切分", "重新读试卷并从头跑一遍（会清空输出目录）"),
            ("rescore", "重新评分", "重新给每道题打分，然后重新选题、解析、导出"),
            ("reselect", "重新选题", "告诉 AI 这批不满意，换一批题；新题会自动补上解析和词汇"),
            ("reexplain", "重生解析", "改完 prompts/*.md 后常用：只重出逐题解析"),
            ("revocab", "重生词汇", "只重出重难点词汇表"),
            ("reexport", "重新导出 Word", "不调 AI，直接用已有结果重排一遍 Word"),
        ]
        self.rerun_buttons = {}
        for key, label, tip in specs:
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, k=key: self.rerun(k))
            self.rerun_buttons[key] = button
            grid.addWidget(button)
        box.setLayout(grid)
        return box

    def _modes(self) -> QTabWidget:
        tabs = QTabWidget()

        # --- 基础模式: no controls, just an honest read-out of what will run.
        # It used to hard-code "评分与讲解使用 deepseek-v4-flash" while the run
        # actually used whatever 进阶模式 last saved — so the tab could lie.
        basic = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel("默认设置已经过调优，直接点「开始整理」即可。"))

        # The one choice worth putting in front of everyone: a cheap run while you
        # are still iterating, or the good one for the batch you actually print.
        # Everything it changes is still visible (and overridable) in 进阶模式.
        self.w_preset = QComboBox()
        for preset in (SPEED, QUALITY):
            self.w_preset.addItem(PRESET_LABELS[preset], preset)
        self.w_preset.addItem(PRESET_LABELS[CUSTOM], CUSTOM)
        self._show_preset(self.cfg.resolved_preset())
        self.w_preset.activated.connect(self._preset_chosen)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("质量档位"))
        preset_row.addWidget(self.w_preset, 1)
        preset_row.addStretch(1)
        holder = QWidget()
        holder.setLayout(preset_row)
        holder.setMaximumWidth(560)
        lay.addWidget(holder)

        # The other choice only the teacher can make. Neither option is the correct one —
        # they answer different questions, so the tool must not answer for her:
        #   完整 — the words come only from the questions she is handing out.
        #   困难 — the model reads the whole paper and picks the genuinely hard ones.
        self.w_vocab_full = QRadioButton("完整（分块）")
        self.w_vocab_full.setToolTip(
            "逐题提词：词表严格对应学生手上的每一道题。\n"
            "模型一次只看一道题，判断不了这个词在整份卷子里算不算难。"
        )
        self.w_vocab_hard = QRadioButton("困难（整卷）")
        self.w_vocab_hard.setToolTip(
            "通读整卷（自动去掉答案区）后只挑真正的重难点。\n"
            "判断更准，但词表里可能有学生那份卷子没考到的题的词。"
        )
        self.w_vocab_mode = QButtonGroup(self)
        self.w_vocab_mode.addButton(self.w_vocab_full)
        self.w_vocab_mode.addButton(self.w_vocab_hard)
        (self.w_vocab_full if self.cfg.vocab_mode == "chunked" else self.w_vocab_hard).setChecked(True)
        for button in (self.w_vocab_full, self.w_vocab_hard):
            button.toggled.connect(self._refresh_summary)

        vocab_row = QHBoxLayout()
        vocab_row.addWidget(QLabel("词汇表"))
        vocab_row.addWidget(self.w_vocab_full)
        vocab_row.addWidget(self.w_vocab_hard)
        vocab_row.addStretch(1)
        vocab_holder = QWidget()
        vocab_holder.setLayout(vocab_row)
        vocab_holder.setMaximumWidth(560)
        lay.addWidget(vocab_holder)

        self.basic_summary = QLabel("")
        self.basic_summary.setStyleSheet("color:#5570a0")
        self.basic_summary.setWordWrap(True)
        lay.addWidget(self.basic_summary)
        lay.addStretch(1)
        basic.setLayout(lay)
        tabs.addTab(self._page(basic), "基础模式")

        # --- 进阶模式. Two columns: stacked one per row, the four stages plus the two
        # extra controls ran past the bottom of the tab and had to be scrolled, even
        # though half the width was empty.
        # The dropdowns are built from whichever provider is selected, so switching API
        # re-populates them rather than offering DeepSeek's model names to Claude.
        provider = self.cfg.provider
        self.w_segment_model = self._model_combo(provider, self.cfg.segment_model)
        self.w_score_model = self._model_combo(provider, self.cfg.score_model)
        self.w_explain_model = self._model_combo(provider, self.cfg.explain_model)
        self.w_enrich_model = self._model_combo(provider, self.cfg.enrich_model)
        self.w_review_model = self._model_combo(provider, self.cfg.review_model)
        self.w_vocab_model = self._model_combo(provider, self.cfg.vocab_model)
        self.w_score_effort = self._effort(provider, self.cfg.score_model, self.cfg.score_effort)
        self.w_explain_effort = self._effort(provider, self.cfg.explain_model, self.cfg.explain_effort)
        self.w_enrich_effort = self._effort(provider, self.cfg.enrich_model, self.cfg.enrich_effort)
        self.w_review_effort = self._effort(provider, self.cfg.review_model, self.cfg.review_effort)
        self.w_vocab_effort = self._effort(provider, self.cfg.vocab_model, self.cfg.vocab_effort)
        self.w_score_thinking = QCheckBox("思考")
        self.w_explain_thinking = QCheckBox("思考")
        self.w_enrich_thinking = QCheckBox("思考")
        self.w_review_thinking = QCheckBox("思考")
        self.w_vocab_thinking = QCheckBox("思考")
        for box, value in (
            (self.w_score_thinking, self.cfg.score_thinking),
            (self.w_explain_thinking, self.cfg.explain_thinking),
            (self.w_enrich_thinking, self.cfg.enrich_thinking),
            (self.w_review_thinking, self.cfg.review_thinking),
            (self.w_vocab_thinking, self.cfg.vocab_thinking),
        ):
            box.setChecked(value == "enabled")

        self.w_workers = QSlider(Qt.Horizontal)
        self.w_workers.setRange(1, 32)
        self.w_workers.setValue(self.cfg.workers)
        self.w_workers_label = QLabel(str(self.cfg.workers))
        self.w_workers.setMaximumHeight(24)
        self.w_workers.setMinimumWidth(110)
        self.w_workers.valueChanged.connect(lambda v: self.w_workers_label.setText(str(v)))
        workers_row = QHBoxLayout()
        workers_row.setContentsMargins(0, 0, 0, 0)
        workers_row.addWidget(self.w_workers, 1)
        workers_row.addWidget(self.w_workers_label)
        workers = QWidget()
        workers.setLayout(workers_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        def cell(row: int, col: int, label: str, widget: QWidget) -> None:
            name = QLabel(label)
            name.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(name, row, col * 2)
            grid.addWidget(widget, row, col * 2 + 1)

        self.w_segment_model.setToolTip("切分在你自己的电脑上完成。只有结构异常、规则读不懂的试卷才会回退到这个模型。")
        cell(0, 0, "切分（回退用）", self.w_segment_model)
        workers.setToolTip("同时发出的请求数。被限流（429）就调低。")
        cell(0, 1, "并发数", workers)
        cell(1, 0, "评分", self._stage_row(self.w_score_model, self.w_score_effort, self.w_score_thinking))
        cell(1, 1, "逐题解析", self._stage_row(self.w_explain_model, self.w_explain_effort, self.w_explain_thinking))
        # 词汇表 is its own row now. It used to have no row at all — it silently rode on
        # 备课笔记's model with its strength hardcoded, so a teacher who set 质量优先 got a
        # word list that had quietly ignored her.
        self.w_vocab_model.setToolTip("重难点词汇表读的是整卷正文（不含答案区），一份卷子一次。")
        cell(2, 0, "词汇表", self._stage_row(self.w_vocab_model, self.w_vocab_effort, self.w_vocab_thinking))
        cell(2, 1, "复核", self._stage_row(self.w_review_model, self.w_review_effort, self.w_review_thinking))
        cell(3, 0, "备课笔记", self._stage_row(self.w_enrich_model, self.w_enrich_effort, self.w_enrich_thinking))

        self.w_review = QCheckBox("启用 AI 复核选题（选得更准，会慢一些）")
        self.w_review.setChecked(self.cfg.review_select)
        grid.addWidget(self.w_review, 4, 1, 1, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        fields = QWidget()
        fields.setLayout(grid)
        # Wide enough to use the window, capped so a full-screen display does not fling
        # each label to one edge and its control to the other.
        fields.setMaximumWidth(1080)
        adv_row = QHBoxLayout()
        adv_row.addWidget(fields)
        adv_row.addStretch(1)
        adv = QWidget()
        adv.setLayout(adv_row)
        tabs.addTab(self._page(adv), "进阶模式")

        # --- 调试模式
        dbg = QWidget()
        lay = QVBoxLayout()
        self.w_verbose = QCheckBox("输出详细日志")
        self.w_verbose.setChecked(self.cfg.verbose)
        lay.addWidget(self.w_verbose)
        self.w_keep = QCheckBox("保留中间产物（segments / scores / api_conversations）")
        self.w_keep.setChecked(self.cfg.keep_intermediates)
        lay.addWidget(self.w_keep)

        # The escape hatch for a network whose interception root is not in the Keychain.
        # truststore handles the normal case; when it cannot, this is the only way
        # through — the same trade `curl -k` makes, and it says so.
        self.w_insecure = QCheckBox("跳过 HTTPS 证书校验（仅当你的网络会拦截证书时才勾）")
        self.w_insecure.setChecked(self.cfg.insecure_ssl)
        self.w_insecure.setToolTip(
            "只有在「测试」提示证书校验失败、且重启程序仍无效时才需要。\n"
            "勾上之后本程序不再验证服务器身份，安全性会下降。"
        )
        lay.addWidget(self.w_insecure)
        open_out = QPushButton("打开中间产物文件夹")
        open_out.clicked.connect(lambda: self._open(self.output_dir))
        lay.addWidget(open_out)
        lay.addStretch(1)
        dbg.setLayout(lay)
        tabs.addTab(self._page(dbg), "调试模式")

        tabs.setCurrentIndex({"basic": 0, "advanced": 1, "debug": 2}.get(self.cfg.mode, 0))
        # Bounded: tall enough for the longest page, but never so greedy that it
        # squeezes the folder/key panel above it down to nothing.
        tabs.setMinimumHeight(230)
        tabs.setMaximumHeight(470)
        self.tabs = tabs
        # 基础模式 must always show what 进阶模式 last saved, so refresh on any change.
        for widget in (self.w_score_model, self.w_explain_model, self.w_enrich_model,
                       self.w_review_model, self.w_vocab_model, self.w_segment_model):
            widget.currentTextChanged.connect(self._refresh_summary)
        for effort in (self.w_score_effort, self.w_explain_effort, self.w_enrich_effort,
                       self.w_review_effort, self.w_vocab_effort):
            effort.currentIndexChanged.connect(self._refresh_summary)
        for box in (self.w_score_thinking, self.w_explain_thinking, self.w_enrich_thinking,
                    self.w_review_thinking, self.w_vocab_thinking, self.w_review):
            box.toggled.connect(self._refresh_summary)
        self._refresh_summary()
        return tabs

    def _stages_widgets(self) -> dict[str, tuple]:
        return {
            "score": (self.w_score_model, self.w_score_effort, self.w_score_thinking),
            "explain": (self.w_explain_model, self.w_explain_effort, self.w_explain_thinking),
            "enrich": (self.w_enrich_model, self.w_enrich_effort, self.w_enrich_thinking),
            "review": (self.w_review_model, self.w_review_effort, self.w_review_thinking),
            "vocab": (self.w_vocab_model, self.w_vocab_effort, self.w_vocab_thinking),
        }

    def _stage_widgets(self) -> dict[str, str]:
        """The per-stage settings as they currently stand in 进阶模式."""
        values: dict[str, str] = {}
        for stage, (model, effort, thinking) in self._stages_widgets().items():
            values[f"{stage}_model"] = model.currentText()
            values[f"{stage}_effort"] = effort.currentData() or ""
            values[f"{stage}_thinking"] = "enabled" if thinking.isChecked() else "disabled"
        return values

    def _show_preset(self, preset: str) -> None:
        index = self.w_preset.findData(preset)
        if index >= 0:
            self.w_preset.setCurrentIndex(index)

    def _preset_chosen(self, _index: int) -> None:
        """Push the chosen preset onto every stage widget in 进阶模式."""
        preset = self.w_preset.currentData()
        if preset == CUSTOM:
            # 自定义 is a state you land in by editing 进阶模式, not one you pick —
            # it has no values of its own. Snap the picker back to what will
            # actually run, rather than leaving it claiming 自定义 while the speed
            # preset's models are still loaded.
            self._refresh_summary()
            return
        widgets = self._stages_widgets()
        for key, value in preset_values(preset, self.cfg.provider).items():
            stage, _, kind = key.partition("_")
            model, effort, thinking = widgets[stage]
            if kind == "model":
                model.setCurrentText(value)
            elif kind == "effort":
                effort.setCurrentIndex(max(0, effort.findData(value)))
            else:
                thinking.setChecked(value == "enabled")
        self._refresh_summary()

    def _rebuild_for_provider(self) -> None:
        """Re-populate every model/effort dropdown after the API was switched.

        A model name only means something to the provider it came from, so the lists have
        to be rebuilt rather than kept: leaving ``deepseek-v4-pro`` selected after a
        switch to Claude would send Anthropic a model it has never heard of, and the run
        would die ten minutes later with a 404.
        """
        provider = self.cfg.provider
        for stage, (model, effort, _thinking) in self._stages_widgets().items():
            self._fill_model_combo(model, provider, getattr(self.cfg, f"{stage}_model"))
            self._fill_effort_combo(
                effort, provider, getattr(self.cfg, f"{stage}_model"), getattr(self.cfg, f"{stage}_effort")
            )
        self._fill_model_combo(self.w_segment_model, provider, self.cfg.segment_model)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        """Restate, in 基础模式, exactly what the next run will do."""

        def thinking(box: QCheckBox) -> str:
            return "思考开" if box.isChecked() else "思考关"

        def depth(combo: QComboBox) -> str:
            return EFFORT_LABELS.get(combo.currentData() or "", combo.currentData() or "不可调")

        # Hand-editing a dropdown in 进阶模式 has to move the badge to 自定义 —
        # a picker that still says 速度优先 while a pro model runs is a lie.
        self._show_preset(detect_preset(self._stage_widgets(), self.cfg.provider))

        spec = pv.get(self.cfg.provider)
        note = f"\n· {spec.note}" if spec.note else ""
        how = (
            "完整（分块）：逐题提词，词表对应学生手上的每一道题"
            if self.vocab_mode() == "chunked"
            else "困难（整卷）：通读整卷（去掉答案区），只挑真正的重难点"
        )
        self.basic_summary.setText(
            "本次将使用（进阶模式保存的设置即为这里的默认值）：\n"
            f"· 服务商：{spec.label}\n"
            f"· 切分：在你自己的电脑上完成（异常试卷才回退到 {self.w_segment_model.currentText()}）\n"
            f"· 评分：{self.w_score_model.currentText()}，{thinking(self.w_score_thinking)}，"
            f"{depth(self.w_score_effort)}\n"
            f"· 逐题解析：{self.w_explain_model.currentText()}，{thinking(self.w_explain_thinking)}，"
            f"{depth(self.w_explain_effort)}\n"
            f"· 词汇表：{self.w_vocab_model.currentText()}，{thinking(self.w_vocab_thinking)}，"
            f"{depth(self.w_vocab_effort)}\n"
            f"    {how}\n"
            f"· AI 复核选题：{'开启' if self.w_review.isChecked() else '关闭'}\n"
            "· 输出：学生版、教师讲解版（原题＋官方解析＋AI 逐题解析）、答案汇总版、重难点词汇表"
            f"{note}"
        )

    def vocab_mode(self) -> str:
        """完整 = chunked, 困难 = whole."""
        return "chunked" if self.w_vocab_full.isChecked() else "whole"

    def _page(self, inner: QWidget) -> QScrollArea:
        """Wrap a tab page so it scrolls itself.

        Without this the QTabWidget takes the height of its tallest page and can
        never shrink, which pushed the window's minimum height past the screen —
        the very thing that hid the buttons behind the Dock.
        """
        page = QScrollArea()
        page.setWidget(inner)
        page.setWidgetResizable(True)
        page.setFrameShape(QScrollArea.NoFrame)
        return page

    def _stage_row(self, model: QComboBox, effort: QWidget, thinking: QCheckBox) -> QWidget:
        """Model, thinking strength and thinking toggle for one stage, on one line."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        model.setMinimumWidth(126)
        row.addWidget(model)
        row.addWidget(effort, 1)
        row.addWidget(thinking)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    def _model_combo(self, provider: str, current: str) -> QComboBox:
        combo = QComboBox()
        # Editable: a provider we have not verified (or one the teacher added herself)
        # may serve a model that is not in our table. 刷新模型列表 tells her the real
        # names; this lets her type one in.
        combo.setEditable(True)
        self._fill_model_combo(combo, provider, current)
        return combo

    @staticmethod
    def _fill_model_combo(combo: QComboBox, provider: str, current: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        for spec in models_for(provider):
            combo.addItem(spec.id)
            combo.setItemData(combo.count() - 1, f"{spec.label} · 上下文 {spec.context_window:,}", Qt.ToolTipRole)
        combo.setCurrentText(current or "")
        combo.blockSignals(False)

    def _effort(self, provider: str, model: str, current: str) -> QComboBox:
        """Thinking depth — however many levels this particular model really has.

        The levels are the provider's, not ours: DeepSeek has two (its docs say low and
        medium are both mapped onto high, so a four-notch slider sent an identical
        request three times over — 决策 26), OpenAI has six, and GLM has none at all.
        A model with no dial gets a disabled box that says so, rather than a switch that
        looks like a cost/quality trade and changes nothing.
        """
        combo = QComboBox()
        self._fill_effort_combo(combo, provider, model, current)
        return combo

    @staticmethod
    def _fill_effort_combo(combo: QComboBox, provider: str, model: str, current: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        levels = efforts_for(provider, model)
        if not levels:
            combo.addItem("不可调", "")
            combo.setEnabled(False)
            combo.setToolTip("这个模型没有强度档位，只有「思考开/关」。")
        else:
            for effort in levels:
                combo.addItem(EFFORT_LABELS.get(effort, effort), effort)
            combo.setEnabled(True)
            combo.setToolTip("")
            combo.setCurrentIndex(max(0, combo.findData(normalize_effort(current, provider, model))))
        combo.blockSignals(False)

    def _actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.run_btn = QPushButton("开始整理")
        self.run_btn.setMinimumHeight(44)
        self.run_btn.setStyleSheet(
            "QPushButton{background:#2563eb;color:white;border-radius:8px;font-size:16px;font-weight:600}"
            "QPushButton:disabled{background:#9db8ea}"
        )
        self.run_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setMinimumHeight(44)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = QPushButton("打开结果文件夹")
        self.open_btn.setMinimumHeight(44)
        self.open_btn.clicked.connect(lambda: self._open(self.output_dir / "docx_exports"))
        row.addWidget(self.run_btn, 2)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.open_btn)
        return row

    # --- behaviour

    def show_intro(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("欢迎使用")
        box.setTextFormat(Qt.RichText)
        box.setText(
            "<b>三步就能用：</b><br><br>"
            "<b>1.</b> 把试卷（.docx 或 .pdf）拖到上面的虚线框里，或点「选择试卷文件夹」。<br>"
            "<b>2.</b> 填入 DeepSeek API Key（<a href='https://platform.deepseek.com/'>点此申请</a>）。<br>"
            "<b>3.</b> 点「开始整理」。<br><br>"
            "完成后会生成三份 Word：<b>学生版 / 教师讲解版 / 答案汇总版</b>。<br>"
            "题目原文直接从原卷里搬运，<b>格式与原卷一致，不需要您再调</b>。<br><br>"
            "<span style='color:#888'>设置会自动记住，下次打开直接点「开始整理」即可。</span>"
        )
        box.exec()
        self.cfg.seen_intro = True
        self.cfg.save()

    def on_dropped(self, paths: list[Path]) -> None:
        folders = [p for p in paths if p.is_dir()]
        files = [p for p in paths if p.is_file() and p.suffix.lower() in PAPER_SUFFIXES]

        if folders:
            self.set_input(folders[0])
            return
        if not files:
            QMessageBox.warning(self, "格式不支持", "只能处理 .docx 和 .pdf 试卷。")
            return

        # Copy dropped papers into the working folder rather than reading them in
        # place: the pipeline must never write anywhere near the originals.
        target = HOME / "input_docx"
        target.mkdir(parents=True, exist_ok=True)
        import shutil

        for f in files:
            with contextlib.suppress(OSError):
                shutil.copy2(f, target / f.name)
        self.set_input(target)
        self.log.appendPlainText(f"已添加 {len(files)} 份试卷到 {target}")

    def set_input(self, folder: Path) -> None:
        self.input_dir = folder
        self.input_label.setText(str(folder))
        self.refresh_papers()

    def refresh_papers(self) -> None:
        # The drop zone keeps its one line; the papers get their own wrapping label,
        # so a long filename elides instead of shoving the count off the screen.
        self.drop.setText("把试卷拖到这里（.docx / .pdf，也可以直接拖文件夹）")

        found = papers_in(self.input_dir)
        if found:
            names = "、".join(_elide(p.name, 22) for p in found[:3])
            more = f" 等 {len(found)} 份" if len(found) > 3 else ""
            self.papers_label.setText(f"✅ 已找到 {len(found)} 份试卷：{names}{more}")
        else:
            self.papers_label.setText("")
        self.hint.setText(f"v{VERSION}")

    def pick_input(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择试卷文件夹", str(self.input_dir))
        if chosen:
            self.set_input(Path(chosen))

    def pick_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择输出文件夹", str(self.output_dir.parent))
        if chosen:
            self.output_dir = Path(chosen)
            self.output_label.setText(chosen)

    def _open(self, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(target)])

    def _collect(self) -> Settings:
        cfg = self.cfg
        cfg.input_dir = str(self.input_dir)
        cfg.output_dir = str(self.output_dir)
        cfg.mode = ["basic", "advanced", "debug"][self.tabs.currentIndex()]
        cfg.segment_model = self.w_segment_model.currentText()
        for key, value in self._stage_widgets().items():
            setattr(cfg, key, value)
        cfg.preset = detect_preset(self._stage_widgets(), cfg.provider)
        cfg.vocab_mode = self.vocab_mode()
        cfg.workers = self.w_workers.value()
        cfg.review_select = self.w_review.isChecked()
        cfg.verbose = self.w_verbose.isChecked()
        cfg.keep_intermediates = self.w_keep.isChecked()
        cfg.insecure_ssl = self.w_insecure.isChecked()
        cfg.save()
        return cfg

    def start(self) -> None:
        self._launch(full_run(self.w_review.isChecked()))

    def rerun(self, key: str) -> None:
        """A 分步重跑 button: redo one stage and everything it invalidates."""
        job = rerun_jobs(self.w_review.isChecked())[key]
        if not (self.output_dir / "selected_items.json").exists() and key != "resegment":
            QMessageBox.warning(self, "还没有结果", "请先点「开始整理」跑一轮，再单独重跑某一块。")
            return
        self._launch(job)

    def _launch(self, job: Job) -> None:
        papers = papers_in(self.input_dir)
        if not papers:
            QMessageBox.warning(self, "没有试卷", f"{self.input_dir} 里没有 .docx 或 .pdf。\n把试卷拖到虚线框里即可。")
            return

        # The keys live in the Keychain now, written by KeysDialog — the main window no
        # longer holds the fields, so it reads them back from where they were saved. The
        # model key is looked up under the *current provider's* env var, so switching API
        # picks up that API's key rather than DeepSeek's.
        spec = pv.get(self.cfg.provider)
        account = key_account(self.cfg.provider)
        keys = {name: keychain_get(name) for name in
                (account, "PADDLEOCR_ACCESS_TOKEN", "PADDLEOCR_BASE_URL", "MINERU_TOKEN")}
        if not keys[account]:
            QMessageBox.warning(self, "缺少密钥", f"请先点「API 设置…」填写 {spec.label} 的 API Key。")
            return

        if any(p.suffix.lower() == ".pdf" for p in papers):
            if self.cfg.pdf_backend == "mineru" and not keys["MINERU_TOKEN"]:
                QMessageBox.warning(self, "缺少 MinerU 令牌", "文件夹里有 PDF，需要在「API 设置…」里填写 MinerU 令牌。")
                return
            if self.cfg.pdf_backend == "paddle" and not (
                keys["PADDLEOCR_ACCESS_TOKEN"] and keys["PADDLEOCR_BASE_URL"]
            ):
                QMessageBox.warning(self, "缺少 PaddleOCR 配置", "文件夹里有 PDF，需要在「API 设置…」里填写 PaddleOCR 令牌和服务地址。")
                return

        cfg = self._collect()
        self.log.clear()
        self.log.appendPlainText(f"【{job.name}】{len(papers)} 份试卷 · 将执行：{' → '.join(job.stages)}\n")
        self._set_running(True)

        self.thread = QThread()
        self.worker = Worker(self.input_dir, self.output_dir, keys, cfg, job)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.plan.connect(self.on_plan)
        self.worker.step.connect(self.on_step)
        self.worker.stage_done.connect(self.on_stage_done)
        self.worker.done.connect(self.on_done)

        self.started_at = time.time()
        self.plan = []
        self.stage_index = 0
        self.stage_fraction = 0.0
        self.ticker.start(1000)  # the user asked for a 1-second heartbeat
        self.thread.start()

    def cancel(self) -> None:
        if not self.worker:
            return

        # Cancelling a local stage costs nothing, so it just stops. Cancelling an API
        # stage throws away work that was paid for, and the request already in flight may
        # be billed whatever we do — so say so and ask, rather than surprising her.
        stage = self.plan[self.stage_index][0] if self.plan else ""
        if stage in API_STAGES:
            answer = QMessageBox.question(
                self, "取消？", CANCEL_WARNING,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.cancel_btn.setEnabled(False)
        self.log.appendPlainText("\n正在取消：已断开进行中的请求…")
        self.worker.cancel()

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for button in self.rerun_buttons.values():
            button.setEnabled(not running)

    # --- progress. Stages are weighted by how long they are expected to take:
    # --- weighting them equally put the sub-second local segmentation and the
    # --- multi-minute explanation at 1/9 of the bar each, so it read 11% instantly
    # --- and then sat still.

    def on_plan(self, plan: list) -> None:
        self.plan = [(mode, label, float(seconds)) for mode, label, seconds in plan]
        total = sum(seconds for _, _, seconds in self.plan)
        self.log.appendPlainText(f"预计耗时约 {format_duration(total)}。\n")
        self._tick()

    def on_step(self, index: int, fraction: float) -> None:
        self.stage_index, self.stage_fraction = index, fraction
        self._tick()

    def on_stage_done(self, index: int) -> None:
        self.stage_index, self.stage_fraction = index, 1.0
        self._tick()

    def _tick(self) -> None:
        """Repaint the bar. Called on every event and once a second regardless."""
        if not self.plan or self.started_at is None:
            return
        weights = [seconds for _, _, seconds in self.plan]
        total = sum(weights) or 1.0

        done = sum(weights[: self.stage_index]) + weights[self.stage_index] * self.stage_fraction
        fraction = min(1.0, done / total)
        remaining = max(0.0, total - done)

        label = self.plan[self.stage_index][1]
        elapsed = time.time() - self.started_at
        self.progress.setMaximum(1000)
        self.progress.setValue(int(fraction * 1000))
        self.progress.setFormat(
            f"{label} · {fraction * 100:.0f}% · 已用 {format_duration(elapsed)}"
            f" · 预计剩余 {format_duration(remaining)}"
        )

    def on_done(self, ok: bool, message: str) -> None:
        self.ticker.stop()
        self._set_running(False)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self._show_cost()
        if ok:
            self.progress.setFormat(
                f"完成 · 用时 {format_duration(time.time() - (self.started_at or time.time()))}"
            )
            self.progress.setValue(self.progress.maximum())
            self.log.appendPlainText(f"\n✅ 全部完成，Word 文件在：{message}")
            if QMessageBox.question(self, "完成", "整理完成，现在打开结果文件夹？") == QMessageBox.Yes:
                self._open(self.output_dir / "docx_exports")
        else:
            self.progress.setFormat("失败")
            self.log.appendPlainText(f"\n❌ {message}")
            QMessageBox.critical(self, "出错了", message)

    def _show_cost(self) -> None:
        """Token usage was being written to disk every run and never shown."""
        try:
            import usage_report

            self.cost.setText(usage_report.summary_line(self.output_dir))
        except Exception:  # noqa: BLE001 — a missing cost line must never fail a run
            self.cost.setText("")

    def check_update(self) -> None:
        try:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "gaokao-app"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                latest = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # No release published yet — that is not an error the teacher
                # needs to see as a failure.
                QMessageBox.information(self, "暂无更新", f"当前 v{VERSION} 已是最新版本（尚未发布更新）。")
                return
            QMessageBox.warning(self, "检查更新失败", f"HTTP {exc.code}")
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "检查更新失败", f"无法连接 GitHub：{exc}")
            return

        tag = str(latest.get("tag_name", "")).lstrip("v")
        if tag and tag != VERSION:
            if QMessageBox.question(
                self, "有新版本", f"发现新版本 v{tag}（当前 v{VERSION}），前往下载？"
            ) == QMessageBox.Yes:
                subprocess.Popen(["open", latest.get("html_url", f"https://github.com/{REPO}/releases")])
        else:
            QMessageBox.information(self, "已是最新", f"当前已是最新版本 v{VERSION}。")


def selftest() -> int:
    """Prove the frozen bundle can actually reach the pipeline.

    A PyInstaller build that launches fine can still die the moment it imports
    the pipeline, so the build script runs this rather than trusting the window.
    """
    import gaokao_english_docx_pipeline as pipeline  # noqa: F401
    import docx_splice  # noqa: F401
    import export_docx_splice
    import mineru_ingest
    import pdf_ingest
    import providers
    from settings import Settings

    assert pipeline.parse_args(["x"]).segment_workers == 16
    assert callable(mineru_ingest.ingest_pdf) and callable(mineru_ingest.check_service)

    # Check the templates through the *same* path the exporter uses, not through a
    # path recomputed here. PyInstaller flattens scripts/ onto the top level, so
    # the exporter's own `__file__`-relative lookup pointed outside the bundle and
    # it silently fell back to python-docx's US Letter default.
    for name in ("answers_reference.docx", "student_reference.docx"):
        found = export_docx_splice.TEMPLATE_DIR / name
        assert found.exists(), f"exporter cannot find its template: {found}"
    assert pdf_ingest.TEMPLATE.exists(), f"pdf ingest cannot find its template: {pdf_ingest.TEMPLATE}"

    import answer_explanation  # noqa: F401
    import export_vocab_docx  # noqa: F401
    import usage_report

    # The explanation prompts are data, not code, so PyInstaller only ships them if
    # the build passes --add-data. Load one through the pipeline's own lookup — the
    # same mistake with the Word templates shipped an app that wrote US Letter.
    for section in pipeline.EXPLAIN_PROMPT_FILES:
        rendered = pipeline.load_explain_prompt(section)
        assert "先给答案" in rendered, f"explanation prompt for {section} is missing its style rules"

    # Same story for DeepSeek's 7.5 MB vocabulary. It is a soft dependency — the cost
    # line degrades to a character estimate without it rather than crashing — which is
    # exactly why a build that silently dropped it would go unnoticed. Fail here instead.
    import deepseek_tokens

    assert deepseek_tokens.is_exact(), f"tokenizer not bundled: {deepseek_tokens.tokenizer_path()}"
    assert deepseek_tokens.count("这是一段中文") > 0

    # The whole app was dead on a new Mac because Python could not verify HTTPS behind a
    # network that re-signs it. truststore is what fixes that, and a build that dropped it
    # would fail silently — the fallback still "works", right up until it does not.
    import ssl

    assert net_tls.mode() == net_tls.SYSTEM, f"trust store is {net_tls.mode()}, not the macOS Keychain"
    assert net_tls.context().verify_mode == ssl.CERT_REQUIRED
    assert net_tls.context(insecure=True).verify_mode == ssl.CERT_NONE

    # Cancelling has to reach the pipeline, not just the stage loop in here.
    assert callable(pipeline.request_cancel) and callable(pipeline.set_cancel_event)
    assert issubclass(pipeline.Cancelled, Exception)

    # Every choice the UI offers must be one the pipeline accepts — for every provider,
    # not just DeepSeek. A payload builder that raises, or a preset that resolves to a
    # model the provider does not list, is a run that dies on the teacher's machine.
    for pid in providers.PROVIDER_ORDER:
        spec = providers.get(pid)
        for preset in (SPEED, QUALITY):
            values = preset_values(preset, pid)
            if not values.get("explain_model"):
                continue  # a 预留 provider with no models yet; nothing to resolve
            args = pipeline.parse_args(["x", "--provider", pid, "--preset", preset])
            assert args.base_url, f"{pid} has no base_url"
            assert args.api_key_env, f"{pid} has no api key env var"
            # The payload must build without raising, in whichever protocol it speaks.
            if spec.protocol == providers.ANTHROPIC:
                body = pipeline.anthropic_payload(
                    "hi", provider=pid, model=args.explain_model, temperature=0.2,
                    reasoning_effort=args.explain_reasoning_effort, thinking="enabled",
                    max_tokens=args.explain_max_tokens,
                )
                assert "max_tokens" in body, "Anthropic requires max_tokens"
                assert "temperature" not in body, "Opus 4.7+ rejects temperature"
            else:
                body, extras = pipeline.chat_payload(
                    "hi", provider=pid, model=args.explain_model, temperature=0.2,
                    reasoning_effort=args.explain_reasoning_effort, thinking="enabled",
                    max_tokens=args.explain_max_tokens,
                )
                assert body["model"] == args.explain_model
            # An effort level must never be sent to a model that has no dial: GLM would
            # take reasoning_effort as an unknown field.
            if not providers.model_spec(pid, args.explain_model).efforts:
                assert not args.explain_reasoning_effort, f"{pid} has no effort dial but one was set"

    # DeepSeek's own two levels, spelled out — the reference implementation.
    for effort in providers.model_spec("deepseek", "deepseek-v4-pro").efforts:
        parsed = pipeline.parse_args(["x", "--score-reasoning-effort", effort])
        assert parsed.score_reasoning_effort == effort
    assert Settings().workers == 16

    # A control that does not reach the pipeline is decoration. Build the argv the
    # way the Worker does and read it back through the pipeline's own parser, so a
    # setting that never gets passed (as --*-thinking never was) fails here.
    cfg = Settings()
    cfg.score_model, cfg.explain_model = "deepseek-v4-pro", "deepseek-v4-flash"
    cfg.score_effort, cfg.explain_effort = "high", "max"
    cfg.score_thinking, cfg.explain_thinking = "disabled", "enabled"
    cfg.workers = 7
    worker = Worker(Path("in"), Path("out"), {}, cfg, full_run(review_select=False))
    parsed = pipeline.parse_args(worker._argv("score"))
    assert parsed.score_model == "deepseek-v4-pro", parsed.score_model
    assert parsed.explain_model == "deepseek-v4-flash"
    assert parsed.score_reasoning_effort == "high"
    assert parsed.explain_reasoning_effort == "max"
    assert parsed.score_thinking == "disabled"
    assert parsed.explain_thinking == "enabled"
    assert parsed.score_workers == 7 and parsed.enrich_workers == 7

    # The teacher's 完整/困难 choice must survive the trip into the pipeline. A switch that
    # only exists in the UI is decoration — which is precisely how --*-thinking once
    # failed, silently running score with thinking off while the box said it was on.
    for mode in pipeline.VOCAB_MODES:
        cfg.vocab_mode = mode
        argv = Worker(Path("in"), Path("out"), {}, cfg, full_run(review_select=False))._argv("vocab")
        assert pipeline.parse_args(argv).vocab_mode == mode, f"GUI 的 {mode} 没传到流水线"

    # Both handouts must be buildable, and neither may carry the answer key.
    segment = {"item_id": "x", "question_text": "Trees cool the air around them."}
    item_prompt = pipeline.build_vocab_item_prompt(segment)
    paper_prompt = pipeline.build_vocab_paper_prompt("卷.docx", "Some paper text.")
    for prompt in (item_prompt, paper_prompt):
        # 决策 27: saying only "no English quotes inside strings" made flash apply the rule
        # to the JSON syntax itself and emit {“word”: “x”}. Both halves, or neither.
        assert "语法符号" in prompt and "错误示例" in prompt, "vocab prompt lost its quote rules"

    # Every stage of every job must be a mode the pipeline can dispatch. Built from
    # the jobs themselves rather than a copy of the list, so a renamed stage
    # (enrich-selected -> explain) cannot pass by being renamed in only one place.
    jobs = [full_run(True), full_run(False), *rerun_jobs(True).values(), *rerun_jobs(False).values()]
    for job in jobs:
        assert job.stages, job.name
        for stage in job.stages:
            assert pipeline.parse_args(["x", "--mode", stage]).mode == stage

    # --init is a rmtree of the output folder. Only the full run may ever ask for it,
    # or a 分步重跑 would delete the very work it is meant to preserve.
    for key, job in rerun_jobs(False).items():
        if key == "resegment":
            continue
        assert not job.init, f"{job.name} must not wipe the output directory"
        for stage in job.stages:
            assert "--init" not in Worker(Path("in"), Path("out"), {}, cfg, job)._argv(stage)

    # Prove the frozen bundle can actually emit a file Word will open. Twice now a
    # document that looked fine here made Word demand a repair (an empty
    # cp:lastPrinted; w:tblBorders written after w:tblLook), so the build gate
    # runs the real builders and the real validator rather than trusting imports.
    import tempfile

    import docx_splice as ds

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.docx"
        doc = ds.blank_template(export_docx_splice.TEMPLATE_DIR / "answers_reference.docx")
        ds.ensure_note_styles(doc)
        doc.add_paragraph("自检", style=ds.NOTE_HEADING)
        table = doc.add_table(rows=2, cols=2)
        ds.set_table_borders(table)
        doc.save(str(probe))
        ds.scrub_metadata(probe, "自检")
        ds.validate(probe)
        assert ds.empty_typed_core_properties(probe) == []

    assert callable(usage_report.summary_line)
    # The models we ship as a *preset* default must be priced — those are the ones a
    # teacher will actually run, and a cost line that silently omits them is 决策 20
    # again. A model she types in herself may legitimately be unpriced; that case is
    # reported as 未配价格 rather than billed at somebody else's rate.
    for pid in ("deepseek", "openai", "anthropic"):
        for role in (providers.FLASH, providers.PRO):
            model = providers.get(pid).role_model(role)
            assert usage_report.rate(model, pid) is not None, f"{pid}/{model} has no price"
    # And a model nobody has priced must come back as unknown, not as DeepSeek's rate.
    assert usage_report.rate("some-model-nobody-priced", "custom") is None
    assert usage_report.price(1000, 1000, 1000, "some-model-nobody-priced", "custom") is None
    assert line_fraction("  scored abc (3/18)") == 3 / 18
    assert line_fraction("no counter here") is None
    assert is_important("[10:00:00] Scoring 27 segment(s)")
    assert not is_important("[10:00:00]   scored 江苏__reading_a__01 (3/18)")

    print("selftest ok: pipeline, splice, ocr, templates, settings, cost and argv all reachable")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    # Also set at runtime, not just baked into the bundle: without this the Dock shows
    # the generic Python rocket when the app is run from source.
    icon = BUNDLE / "assets" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "input_docx").mkdir(exist_ok=True)
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
