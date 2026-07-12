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
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

VERSION = "0.3.0"
REPO = "CSUDerrick/gaokao-english-docx-pipeline"

if getattr(sys, "frozen", False):
    BUNDLE = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    HOME = Path.home() / "Documents" / "高三英语试卷整理"
else:
    BUNDLE = Path(__file__).resolve().parents[1]
    HOME = BUNDLE

sys.path.insert(0, str(BUNDLE / "scripts"))

KEYCHAIN_SERVICE = "gaokao-english-docx-pipeline"


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


# --------------------------------------------------------------------------- worker


class Worker(QObject):
    line = Signal(str)
    step = Signal(int, int, str)
    done = Signal(bool, str)

    def __init__(self, input_dir: Path, out_dir: Path, keys: dict[str, str], review: bool):
        super().__init__()
        self.input_dir, self.out_dir, self.keys, self.review = input_dir, out_dir, keys, review
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    class _Tee(io.TextIOBase):
        def __init__(self, emit):
            self.emit = emit

        def write(self, text):  # noqa: D102
            for chunk in str(text).splitlines():
                if chunk.strip():
                    self.emit(chunk)
            return len(text)

    def run(self) -> None:
        for name, value in self.keys.items():
            if value:
                os.environ[name] = value

        stages = ["segment", "score", "select"]
        if self.review:
            stages.append("review-select")
        stages += ["enrich-selected", "assemble", "repair-answers", "export-docx"]
        labels = {
            "segment": "切分题目", "score": "AI 评分", "select": "自动选题",
            "review-select": "AI 复核选题", "enrich-selected": "生成讲解",
            "assemble": "汇总", "repair-answers": "修复答案", "export-docx": "导出 Word",
        }

        import gaokao_english_docx_pipeline as pipeline

        try:
            for i, mode in enumerate(stages):
                if self._stop.is_set():
                    self.done.emit(False, "已取消")
                    return
                self.step.emit(i, len(stages), labels.get(mode, mode))

                argv = [
                    str(self.input_dir), "--out", str(self.out_dir), "--mode", mode,
                    "--client", "http", "--segment-input", "local",
                    "--segment-workers", "16", "--score-workers", "16",
                    "--enrich-workers", "16", "--max-retries", "12",
                ]
                if mode == "segment":
                    argv.append("--init")

                tee = self._Tee(self.line.emit)
                with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                    pipeline.main(argv)

            self.step.emit(len(stages), len(stages), "完成")
            self.done.emit(True, str(self.out_dir / "docx_exports"))
        except SystemExit as exc:
            self.done.emit(False, str(exc) or "流程中止")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- window


class Window(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"高三英语试卷整理工具 v{VERSION}")
        self.resize(880, 680)

        self.input_dir = HOME / "input_docx"
        self.out_dir = HOME / "outputs" / "gaokao_english"
        self.thread: QThread | None = None
        self.worker: Worker | None = None

        root = QVBoxLayout()

        # --- input
        box = QGroupBox("① 选择试卷文件夹（支持 .docx 和 .pdf）")
        row = QHBoxLayout()
        self.input_label = QLabel(str(self.input_dir))
        self.input_label.setStyleSheet("color:#555")
        pick = QPushButton("选择文件夹…")
        pick.clicked.connect(self.pick_input)
        row.addWidget(self.input_label, 1)
        row.addWidget(pick)
        box.setLayout(row)
        root.addWidget(box)

        # --- keys
        box = QGroupBox("② API 密钥（保存在 macOS 钥匙串，不写入文件）")
        col = QVBoxLayout()
        self.deepseek = QLineEdit(keychain_get("DEEPSEEK_API_KEY"))
        self.deepseek.setEchoMode(QLineEdit.Password)
        self.deepseek.setPlaceholderText("DeepSeek API Key（评分与讲解需要）")
        col.addWidget(self.deepseek)

        self.paddle_token = QLineEdit(keychain_get("PADDLEOCR_ACCESS_TOKEN"))
        self.paddle_token.setEchoMode(QLineEdit.Password)
        self.paddle_token.setPlaceholderText("PaddleOCR 令牌（仅处理 PDF 时需要，可留空）")
        col.addWidget(self.paddle_token)

        self.paddle_url = QLineEdit(keychain_get("PADDLEOCR_BASE_URL"))
        self.paddle_url.setPlaceholderText("PaddleOCR 服务地址（在 aistudio.baidu.com/paddleocr/task 查看，可留空）")
        col.addWidget(self.paddle_url)

        self.review = QCheckBox("启用 AI 复核选题（更准，但更慢更贵）")
        col.addWidget(self.review)
        box.setLayout(col)
        root.addWidget(box)

        # --- run
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
        self.open_btn.clicked.connect(self.open_out)
        row.addWidget(self.run_btn, 2)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.open_btn)
        root.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("就绪")
        root.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        self.log.setMaximumBlockCount(3000)
        root.addWidget(self.log, 1)

        footer = QHBoxLayout()
        update = QPushButton("检查更新")
        update.clicked.connect(self.check_update)
        footer.addStretch(1)
        footer.addWidget(update)
        root.addLayout(footer)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

    # --- actions

    def pick_input(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择试卷文件夹", str(self.input_dir))
        if chosen:
            self.input_dir = Path(chosen)
            self.input_label.setText(chosen)

    def open_out(self) -> None:
        target = self.out_dir / "docx_exports"
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(target)])

    def start(self) -> None:
        papers = list(self.input_dir.glob("*.docx")) + list(self.input_dir.glob("*.pdf"))
        papers = [p for p in papers if not p.name.startswith("~$")]
        if not papers:
            QMessageBox.warning(self, "没有试卷", f"{self.input_dir} 里没有找到 .docx 或 .pdf 文件。")
            return
        if not self.deepseek.text().strip():
            QMessageBox.warning(self, "缺少密钥", "请先填写 DeepSeek API Key。")
            return
        if any(p.suffix.lower() == ".pdf" for p in papers) and not self.paddle_token.text().strip():
            QMessageBox.warning(self, "缺少 PaddleOCR 令牌", "文件夹里有 PDF，需要填写 PaddleOCR 令牌和服务地址。")
            return

        keys = {
            "DEEPSEEK_API_KEY": self.deepseek.text().strip(),
            "PADDLEOCR_ACCESS_TOKEN": self.paddle_token.text().strip(),
            "PADDLEOCR_BASE_URL": self.paddle_url.text().strip(),
        }
        for name, value in keys.items():
            keychain_set(name, value)

        self.log.clear()
        self.log.appendPlainText(f"找到 {len(papers)} 份试卷，开始处理…\n")
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.thread = QThread()
        self.worker = Worker(self.input_dir, self.out_dir, keys, self.review.isChecked())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.step.connect(self.on_step)
        self.worker.done.connect(self.on_done)
        self.thread.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.log.appendPlainText("\n正在取消，当前步骤结束后停止…")

    def on_step(self, i: int, total: int, label: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(i)
        self.progress.setFormat(f"{label}  ({i}/{total})")

    def on_done(self, ok: bool, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        if ok:
            self.progress.setFormat("完成")
            self.log.appendPlainText(f"\n✅ 全部完成，Word 文件在：{message}")
            if QMessageBox.question(self, "完成", "整理完成，现在打开结果文件夹？") == QMessageBox.Yes:
                self.open_out()
        else:
            self.progress.setFormat("失败")
            self.log.appendPlainText(f"\n❌ {message}")
            QMessageBox.critical(self, "出错了", message)

    def check_update(self) -> None:
        try:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                latest = json.loads(response.read())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "检查更新失败", str(exc))
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
    import export_docx_splice  # noqa: F401
    import pdf_ingest  # noqa: F401

    assert pipeline.parse_args(["x"]).segment_workers == 16
    template = BUNDLE / "assets" / "word_templates" / "answers_reference.docx"
    assert template.exists(), f"missing bundled template: {template}"
    print("selftest ok: pipeline, splice, ocr and templates all reachable")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    app = QApplication(sys.argv)
    app.setApplicationName("高三英语试卷整理工具")
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "input_docx").mkdir(exist_ok=True)
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
