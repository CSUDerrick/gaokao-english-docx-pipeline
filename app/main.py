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
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QStackedWidget,
    QTabWidget,
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import EFFORTS, MODELS, Settings  # noqa: E402

KEYCHAIN_SERVICE = "gaokao-english-docx-pipeline"
PAPER_SUFFIXES = {".docx", ".pdf"}


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
        self.setMinimumHeight(96)
        self._idle()

    def _style(self, border: str, bg: str, fg: str) -> None:
        self.setStyleSheet(
            f"QLabel{{border:2px dashed {border};border-radius:10px;"
            f"background:{bg};color:{fg};font-size:14px;padding:12px}}"
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


class Worker(QObject):
    line = Signal(str)
    step = Signal(int, int, str)
    done = Signal(bool, str)

    def __init__(self, input_dir: Path, out_dir: Path, keys: dict[str, str], cfg: Settings):
        super().__init__()
        self.input_dir, self.out_dir, self.keys, self.cfg = input_dir, out_dir, keys, cfg
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

    def _argv(self, mode: str) -> list[str]:
        cfg = self.cfg
        argv = [
            str(self.input_dir), "--out", str(self.out_dir), "--mode", mode,
            "--client", "http", "--segment-input", "local",
            "--segment-model", cfg.segment_model,
            "--score-model", cfg.score_model,
            "--enrich-model", cfg.enrich_model,
            "--review-model", cfg.review_model,
            "--score-reasoning-effort", cfg.score_effort,
            "--enrich-reasoning-effort", cfg.enrich_effort,
            "--review-reasoning-effort", cfg.review_effort,
            "--segment-workers", str(cfg.workers),
            "--score-workers", str(cfg.workers),
            "--enrich-workers", str(cfg.workers),
            "--max-retries", "12",
        ]
        if mode == "segment":
            argv.append("--init")
        return argv

    def run(self) -> None:
        for name, value in self.keys.items():
            if value:
                os.environ[name] = value

        stages = ["segment", "score", "select"]
        if self.cfg.review_select:
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
                tee = self._Tee(self.line.emit)
                with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                    pipeline.main(self._argv(mode))

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
        self.resize(940, 780)

        self.cfg = Settings.load(HOME / "settings.json")
        self.input_dir = Path(self.cfg.input_dir) if self.cfg.input_dir else HOME / "input_docx"
        self.output_dir = Path(self.cfg.output_dir) if self.cfg.output_dir else HOME / "outputs" / "gaokao_english"
        self.thread: QThread | None = None
        self.worker: Worker | None = None

        root = QVBoxLayout()
        root.addWidget(self._folders_box())
        root.addWidget(self._keys_box())
        root.addWidget(self._modes())
        root.addLayout(self._actions())

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("就绪")
        root.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        self.log.setMaximumBlockCount(5000)
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

        row = QHBoxLayout()
        self.input_label = QLabel(str(self.input_dir))
        self.input_label.setStyleSheet("color:#555")
        pick_in = QPushButton("选择试卷文件夹…")
        pick_in.clicked.connect(self.pick_input)
        row.addWidget(QLabel("试卷："))
        row.addWidget(self.input_label, 1)
        row.addWidget(pick_in)
        col.addLayout(row)

        row = QHBoxLayout()
        self.output_label = QLabel(str(self.output_dir))
        self.output_label.setStyleSheet("color:#555")
        pick_out = QPushButton("选择输出文件夹…")
        pick_out.clicked.connect(self.pick_output)
        row.addWidget(QLabel("输出："))
        row.addWidget(self.output_label, 1)
        row.addWidget(pick_out)
        col.addLayout(row)

        box.setLayout(col)
        return box

    def _keys_box(self) -> QGroupBox:
        box = QGroupBox("② API 密钥（保存在 macOS 钥匙串，不写入文件）")
        col = QVBoxLayout()
        self.deepseek = QLineEdit(keychain_get("DEEPSEEK_API_KEY"))
        self.deepseek.setEchoMode(QLineEdit.Password)
        self.deepseek.setPlaceholderText("DeepSeek API Key（评分与讲解需要）")
        col.addWidget(self.deepseek)

        self.paddle_token = QLineEdit(keychain_get("PADDLEOCR_ACCESS_TOKEN"))
        self.paddle_token.setEchoMode(QLineEdit.Password)
        self.paddle_token.setPlaceholderText("PaddleOCR 令牌（仅处理 PDF 时需要）")
        col.addWidget(self.paddle_token)

        self.paddle_url = QLineEdit(keychain_get("PADDLEOCR_BASE_URL"))
        self.paddle_url.setPlaceholderText("PaddleOCR 服务地址（aistudio.baidu.com/paddleocr/task 查看）")
        col.addWidget(self.paddle_url)
        box.setLayout(col)
        return box

    def _modes(self) -> QTabWidget:
        tabs = QTabWidget()

        basic = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel(
            "默认设置已经过调优，直接点「开始整理」即可。\n"
            "· 切分在本地完成，不花钱\n"
            "· 评分与讲解使用 deepseek-v4-flash（更快更便宜）"
        ))
        lay.addStretch(1)
        basic.setLayout(lay)
        tabs.addTab(basic, "基础模式")

        adv = QWidget()
        form = QFormLayout()
        self.w_segment_model = self._combo(MODELS, self.cfg.segment_model)
        self.w_score_model = self._combo(MODELS, self.cfg.score_model)
        self.w_enrich_model = self._combo(MODELS, self.cfg.enrich_model)
        self.w_review_model = self._combo(MODELS, self.cfg.review_model)
        form.addRow("切分模型（仅异常试卷回退时用）", self.w_segment_model)
        form.addRow("评分模型", self.w_score_model)
        form.addRow("讲解模型", self.w_enrich_model)
        form.addRow("复核模型", self.w_review_model)

        self.w_score_effort = self._effort(self.cfg.score_effort)
        self.w_enrich_effort = self._effort(self.cfg.enrich_effort)
        self.w_review_effort = self._effort(self.cfg.review_effort)
        form.addRow("评分思考强度", self.w_score_effort[0])
        form.addRow("讲解思考强度", self.w_enrich_effort[0])
        form.addRow("复核思考强度", self.w_review_effort[0])

        self.w_workers = QSlider(Qt.Horizontal)
        self.w_workers.setRange(1, 32)
        self.w_workers.setValue(self.cfg.workers)
        self.w_workers_label = QLabel(str(self.cfg.workers))
        self.w_workers.valueChanged.connect(lambda v: self.w_workers_label.setText(str(v)))
        row = QHBoxLayout()
        row.addWidget(self.w_workers, 1)
        row.addWidget(self.w_workers_label)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("并发数（限流就调低）", holder)

        self.w_review = QCheckBox("启用 AI 复核选题（更准，但更慢更贵）")
        self.w_review.setChecked(self.cfg.review_select)
        form.addRow("", self.w_review)

        adv.setLayout(form)
        tabs.addTab(adv, "进阶模式")

        dbg = QWidget()
        lay = QVBoxLayout()
        self.w_verbose = QCheckBox("输出详细日志")
        self.w_verbose.setChecked(self.cfg.verbose)
        lay.addWidget(self.w_verbose)
        open_out = QPushButton("打开中间产物文件夹（segments / scores / api_conversations）")
        open_out.clicked.connect(lambda: self._open(self.output_dir))
        lay.addWidget(open_out)
        lay.addStretch(1)
        dbg.setLayout(lay)
        tabs.addTab(dbg, "调试模式")

        tabs.setCurrentIndex({"basic": 0, "advanced": 1, "debug": 2}.get(self.cfg.mode, 0))
        self.tabs = tabs
        return tabs

    def _combo(self, options: list[str], current: str) -> QComboBox:
        combo = QComboBox()
        combo.addItems(options)
        if current in options:
            combo.setCurrentText(current)
        return combo

    def _effort(self, current: str) -> tuple[QWidget, QSlider]:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, len(EFFORTS) - 1)
        slider.setValue(EFFORTS.index(current) if current in EFFORTS else 0)
        slider.setTickPosition(QSlider.TicksBelow)
        slider.setTickInterval(1)
        label = QLabel(EFFORTS[slider.value()])
        label.setMinimumWidth(60)
        slider.valueChanged.connect(lambda v: label.setText(EFFORTS[v]))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(label)
        holder = QWidget()
        holder.setLayout(row)
        return holder, slider

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
        found = papers_in(self.input_dir)
        if found:
            names = "、".join(p.name[:16] for p in found[:3])
            more = f" 等 {len(found)} 份" if len(found) > 3 else ""
            self.drop.setText(f"✅ 已找到 {len(found)} 份试卷\n{names}{more}\n\n（拖入新文件可继续添加）")
        else:
            self.drop.setText("把试卷拖到这里\n支持 .docx 和 .pdf，也可以直接拖文件夹")
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
        cfg.score_model = self.w_score_model.currentText()
        cfg.enrich_model = self.w_enrich_model.currentText()
        cfg.review_model = self.w_review_model.currentText()
        cfg.score_effort = EFFORTS[self.w_score_effort[1].value()]
        cfg.enrich_effort = EFFORTS[self.w_enrich_effort[1].value()]
        cfg.review_effort = EFFORTS[self.w_review_effort[1].value()]
        cfg.workers = self.w_workers.value()
        cfg.review_select = self.w_review.isChecked()
        cfg.verbose = self.w_verbose.isChecked()
        cfg.save()
        return cfg

    def start(self) -> None:
        papers = papers_in(self.input_dir)
        if not papers:
            QMessageBox.warning(self, "没有试卷", f"{self.input_dir} 里没有 .docx 或 .pdf。\n把试卷拖到虚线框里即可。")
            return
        if not self.deepseek.text().strip():
            QMessageBox.warning(self, "缺少密钥", "请先填写 DeepSeek API Key。")
            return
        if any(p.suffix.lower() == ".pdf" for p in papers) and not (
            self.paddle_token.text().strip() and self.paddle_url.text().strip()
        ):
            QMessageBox.warning(self, "缺少 PaddleOCR 配置", "文件夹里有 PDF，需要填写 PaddleOCR 令牌和服务地址。")
            return

        keys = {
            "DEEPSEEK_API_KEY": self.deepseek.text().strip(),
            "PADDLEOCR_ACCESS_TOKEN": self.paddle_token.text().strip(),
            "PADDLEOCR_BASE_URL": self.paddle_url.text().strip(),
        }
        for name, value in keys.items():
            keychain_set(name, value)

        cfg = self._collect()
        self.log.clear()
        self.log.appendPlainText(f"找到 {len(papers)} 份试卷，开始处理…\n")
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.thread = QThread()
        self.worker = Worker(self.input_dir, self.output_dir, keys, cfg)
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
                self._open(self.output_dir / "docx_exports")
        else:
            self.progress.setFormat("失败")
            self.log.appendPlainText(f"\n❌ {message}")
            QMessageBox.critical(self, "出错了", message)

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
    import pdf_ingest
    from settings import EFFORTS, MODELS, Settings

    assert pipeline.parse_args(["x"]).segment_workers == 16

    # Check the templates through the *same* path the exporter uses, not through a
    # path recomputed here. PyInstaller flattens scripts/ onto the top level, so
    # the exporter's own `__file__`-relative lookup pointed outside the bundle and
    # it silently fell back to python-docx's US Letter default.
    for name in ("answers_reference.docx", "student_reference.docx"):
        found = export_docx_splice.TEMPLATE_DIR / name
        assert found.exists(), f"exporter cannot find its template: {found}"
    assert pdf_ingest.TEMPLATE.exists(), f"pdf ingest cannot find its template: {pdf_ingest.TEMPLATE}"

    # Every choice the UI offers must be one the pipeline accepts.
    for model in MODELS:
        assert pipeline.parse_args(["x", "--score-model", model]).score_model == model
    for effort in EFFORTS:
        assert pipeline.parse_args(["x", "--score-reasoning-effort", effort]).score_reasoning_effort == effort
    assert Settings().workers == 16

    print("selftest ok: pipeline, splice, ocr, templates and settings all reachable")
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
