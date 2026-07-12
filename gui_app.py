#!/usr/bin/env python3
"""Streamlit GUI for the Gaokao English paper pipeline.

The app keeps the command-line pipeline as the source of truth, but presents a
teacher-friendly workflow by default. Advanced and debug controls are available
without making the first screen feel like a terminal.
"""

from __future__ import annotations

import csv
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import streamlit as st
import streamlit.components.v1 as components


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "gaokao_english_docx_pipeline.py"
QUALITY_SCRIPT = ROOT / "scripts" / "check_segment_quality.py"
DEFAULT_INPUT = ROOT / "input_docx"
DEFAULT_OUT = ROOT / "outputs" / "gaokao_english"
CHECK_OUT = ROOT / "outputs" / "gaokao_english_segment_check"
SECRET_PATH = ROOT / ".local" / "gui_secrets.json"


DEFAULTS = {
    "client": "http",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
    "segment_model": "deepseek-v4-flash",
    "score_model": "deepseek-v4-flash",
    "review_model": "deepseek-v4-pro",
    "enrich_model": "deepseek-v4-flash",
    "segment_workers": 16,
    "score_workers": 16,
    "enrich_workers": 16,
    "segment_input": "local",
    "answer_tail_chars": 8000,
    "review_candidates": 6,
    "segment_thinking": "disabled",
    "score_thinking": "disabled",
    "review_thinking": "enabled",
    "enrich_thinking": "disabled",
    "segment_reasoning": "none",
    "score_reasoning": "none",
    "review_reasoning": "medium",
    "enrich_reasoning": "none",
    "score_max_tokens": 1200,
    "review_max_tokens": 2500,
    "enrich_max_tokens": 3500,
    "max_retries": 12,
    "show_output": "preview",
    "show_reasoning": "none",
    "preview_chars": 1200,
    "save_conversations": True,
    "review_select": True,
    "reset_before_full_run": False,
    "force": False,
    "segment_warning_fallback": True,
}


@dataclass
class CommandResult:
    code: int
    elapsed: float
    lines: list[str]


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def line_progress(line: str) -> float | None:
    match = re.search(r"\((\d+)\s*/\s*(\d+)\)", line)
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    return min(1.0, done / total) if total else None


def render_log_view(slot, lines: list[str]) -> None:
    visible = "\n".join(lines[-500:])
    safe = html.escape(visible)
    frame = f"""
    <style>
      body {{ margin:0; background:#0f172a; }}
      textarea {{ box-sizing:border-box; width:100%; height:350px; resize:none;
        border:1px solid #24324a; border-radius:10px; padding:14px;
        background:#0f172a; color:#dbeafe; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    </style>
    <textarea id="pipeline-log" readonly>{safe}</textarea>
    <script>const box=document.getElementById('pipeline-log'); box.scrollTop=box.scrollHeight;</script>
    """
    slot.empty()
    with slot.container():
        components.html(frame, height=365, scrolling=False)


def update_workflow_progress(state: dict, *, stage_index: int, stage_label: str, within_stage: float) -> None:
    total = state["total"]
    fraction = min(1.0, (stage_index + within_stage) / total)
    elapsed = time.time() - state["started"]
    eta = None
    if fraction >= 0.04 and elapsed >= 2:
        eta = max(0.0, elapsed / fraction - elapsed)
    state["bar"].progress(fraction, text=f"{stage_label} · {fraction * 100:.0f}%")
    state["detail"].markdown(
        f"**当前阶段：{stage_label}**  ·  已用 {format_duration(elapsed)}  ·  "
        + (f"预计剩余 {format_duration(eta)}" if eta is not None else "预计时间计算中")
    )


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --app-primary: #174f9e;
            --app-primary-dark: #113b78;
            --app-accent: #3b82f6;
            --app-border: #dbe5f2;
            --app-muted: #64748b;
            --app-surface: #f4f7fb;
            --app-card: #ffffff;
        }
        .stApp { background: linear-gradient(180deg, #eef4fb 0, #f8fafc 260px, #f8fafc 100%); }
        .block-container {
            padding-top: 1.3rem;
            padding-bottom: 4rem;
            max-width: 1240px;
        }
        [data-testid="stSidebar"] {
            background: #f7f9fc;
            border-right: 1px solid var(--app-border);
        }
        [data-testid="stSidebar"] h2 { color: var(--app-primary-dark); }
        .app-hero {
            color: white;
            background: linear-gradient(125deg, #113b78 0%, #1756aa 58%, #3182ce 100%);
            padding: 1.55rem 1.8rem;
            border-radius: 18px;
            box-shadow: 0 16px 35px rgba(17, 59, 120, 0.18);
            margin-bottom: 1.1rem;
        }
        .app-hero h1 { font-size: 1.7rem; margin: 0 0 .35rem 0; color: white; }
        .app-hero p { margin: 0; opacity: .88; font-size: .96rem; }
        .workflow-strip {
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: .55rem;
            margin: .7rem 0 1.3rem;
        }
        .workflow-step {
            background: rgba(255,255,255,.92);
            border: 1px solid var(--app-border);
            border-radius: 12px;
            padding: .68rem .7rem;
            color: #334155;
            font-size: .82rem;
            text-align: center;
            box-shadow: 0 4px 12px rgba(15, 23, 42, .04);
        }
        .workflow-step b { display:block; color: var(--app-primary); margin-bottom:.12rem; }
        div[data-testid="stMetric"] {
            background: var(--app-card);
            border: 1px solid var(--app-border);
            border-radius: 14px;
            padding: 0.9rem 1rem;
            box-shadow: 0 5px 16px rgba(15,23,42,.05);
        }
        div[data-testid="stMetric"] label {
            color: var(--app-muted);
            font-size: 0.82rem;
        }
        div.stButton > button {
            border-radius: 10px;
            min-height: 2.7rem;
            font-weight: 650;
            border-color: var(--app-border);
        }
        div.stButton > button[kind="primary"] {
            background: linear-gradient(90deg, var(--app-primary-dark), var(--app-primary));
            border: 0;
            box-shadow: 0 8px 18px rgba(23,79,158,.2);
        }
        div[data-testid="stAlert"] {
            border-radius: 12px;
        }
        .app-section {
            padding: 1rem 0 0.5rem 0;
        }
        .app-muted {
            color: var(--app-muted);
            font-size: 0.92rem;
        }
        .app-file-row {
            border-bottom: 1px solid var(--app-border);
            padding: 0.55rem 0;
        }
        .result-card {
            background: white;
            border: 1px solid var(--app-border);
            border-radius: 14px;
            padding: .9rem 1rem;
            margin: .35rem 0;
            box-shadow: 0 5px 16px rgba(15,23,42,.045);
        }
        .result-card strong { color: var(--app-primary-dark); }
        div[data-testid="stCode"] { max-height: 420px; overflow-y: auto; border-radius: 12px; }
        @media (max-width: 900px) {
            .workflow-strip { grid-template-columns: repeat(3, 1fr); }
        }
        @media (max-width: 560px) {
            .workflow-strip { grid-template-columns: repeat(2, 1fr); }
            .app-hero { padding: 1.2rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_json_file(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path, limit: int | None = None) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if limit and len(text) > limit:
        return text[:limit] + f"\n\n... 已省略 {len(text) - limit} 个字符"
    return text


def load_saved_api_key() -> str:
    if not SECRET_PATH.exists():
        return ""
    try:
        data = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    key = data.get("DEEPSEEK_API_KEY", "")
    return key if isinstance(key, str) else ""


def save_api_key(api_key: str) -> None:
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_PATH.write_text(json.dumps({"DEEPSEEK_API_KEY": api_key}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass


def clear_saved_api_key() -> None:
    if SECRET_PATH.exists():
        SECRET_PATH.unlink()


def file_size(path: Path) -> str:
    if not path.exists():
        return "未生成"
    size = path.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return len(list(path.rglob(pattern)))


def directory_entry_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.rglob("*"))


def validate_clear_target(path: Path) -> Path:
    raw = path.expanduser()
    if raw.is_symlink():
        raise ValueError("拒绝清空符号链接目录。")
    resolved = raw.resolve()
    protected = {
        ROOT.resolve(),
        (ROOT / "scripts").resolve(),
        (ROOT / "config").resolve(),
        (ROOT / "assets").resolve(),
        (ROOT / ".venv").resolve(),
    }
    if resolved in protected or ROOT.resolve() not in resolved.parents:
        raise ValueError(f"拒绝清空受保护或项目外目录：{resolved}")
    return resolved


def safe_clear_directory(path: Path) -> int:
    target = validate_clear_target(path)
    count = directory_entry_count(target)
    if target.exists():
        for child in target.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
    target.mkdir(parents=True, exist_ok=True)
    return count


@st.dialog("确认清空目录")
def clear_directory_dialog(label: str, path_text: str) -> None:
    path = Path(path_text)
    st.warning(f"即将清空{label}，该操作不能撤销。")
    st.code(str(path.resolve()), language="text")
    st.write(f"当前包含约 **{directory_entry_count(path)}** 个文件或目录项。")
    confirm = st.checkbox(f"我确认清空{label}", key=f"confirm-clear-{label}")
    if st.button(f"确认清空{label}", type="primary", disabled=not confirm, width="stretch"):
        try:
            removed = safe_clear_directory(path)
        except (OSError, ValueError) as exc:
            st.error(str(exc))
            return
        st.session_state["clear_message"] = f"{label}已清空，共移除约 {removed} 个目录项。"
        st.rerun()


def open_output_folder(path: Path) -> tuple[bool, str]:
    path.mkdir(parents=True, exist_ok=True)
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)])
        else:
            return False, "当前环境没有可用的文件管理器，请使用下载按钮。"
    except OSError as exc:
        return False, f"无法打开文件夹：{exc}"
    return True, "已打开输出文件夹。"


def command_env(cfg: dict, *, include_api: bool) -> dict[str, str]:
    env = os.environ.copy()
    api_env = cfg.get("api_key_env") or "DEEPSEEK_API_KEY"
    for key in {api_env, "DEEPSEEK_API_KEY", "OPENAI_API_KEY"}:
        env.pop(key, None)
    if include_api and cfg.get("api_key"):
        env[api_env] = cfg["api_key"]
    return env


def build_command(mode: str, cfg: dict, *, init: bool = False, force: bool = False) -> list[str]:
    cmd = [
        sys.executable,
        str(SCRIPT),
        cfg["input_dir"],
        "--out",
        cfg["out_dir"],
        "--mode",
        mode,
        "--client",
        cfg["client"],
        "--base-url",
        cfg["base_url"],
        "--api-key-env",
        cfg["api_key_env"],
        "--segment-model",
        cfg["segment_model"],
        "--score-model",
        cfg["score_model"],
        "--review-model",
        cfg["review_model"],
        "--enrich-model",
        cfg["enrich_model"],
        "--segment-workers",
        str(cfg["segment_workers"]),
        "--score-workers",
        str(cfg["score_workers"]),
        "--enrich-workers",
        str(cfg["enrich_workers"]),
        "--segment-input",
        cfg["segment_input"],
        "--answer-tail-chars",
        str(cfg["answer_tail_chars"]),
        "--review-candidates",
        str(cfg["review_candidates"]),
        "--segment-thinking",
        cfg["segment_thinking"],
        "--score-thinking",
        cfg["score_thinking"],
        "--review-thinking",
        cfg["review_thinking"],
        "--enrich-thinking",
        cfg["enrich_thinking"],
        "--segment-reasoning-effort",
        cfg["segment_reasoning"],
        "--score-reasoning-effort",
        cfg["score_reasoning"],
        "--review-reasoning-effort",
        cfg["review_reasoning"],
        "--enrich-reasoning-effort",
        cfg["enrich_reasoning"],
        "--score-max-tokens",
        str(cfg["score_max_tokens"]),
        "--review-max-tokens",
        str(cfg["review_max_tokens"]),
        "--enrich-max-tokens",
        str(cfg["enrich_max_tokens"]),
        "--max-retries",
        str(cfg["max_retries"]),
        "--show-output",
        cfg["show_output"],
        "--show-reasoning",
        cfg["show_reasoning"],
        "--preview-chars",
        str(cfg["preview_chars"]),
    ]
    if cfg["review_select"] and mode == "stage1":
        cmd.append("--review-select")
    if cfg.get("segment_warning_fallback", True):
        cmd.append("--segment-warning-fallback")
    else:
        cmd.append("--no-segment-warning-fallback")
    if not cfg["save_conversations"]:
        cmd.append("--no-save-conversations")
    if init:
        cmd.append("--init")
    if force:
        cmd.append("--force")
    return cmd


def important_lines(lines: list[str]) -> list[str]:
    needles = (
        "Pipeline finished",
        "Step ",
        "Quality report",
        "Segment quality report",
        "wrote ",
        "exported",
        "FAIL",
        "WARN",
        "PASS",
        "segments",
        "selected",
        "enriched",
    )
    picked = [line for line in lines if any(token in line for token in needles)]
    return picked[-10:] if picked else lines[-8:]


def run_command(
    cmd: list[str],
    cfg: dict,
    *,
    label: str,
    include_api: bool,
    timeout: int | None = None,
    workflow_state: dict | None = None,
    stage_index: int = 0,
) -> CommandResult:
    show_logs = cfg.get("ui_mode") != "基础模式"
    status = None if workflow_state else st.status(label, expanded=False)
    log_box = workflow_state.get("log") if workflow_state else st.empty()
    start = time.time()
    lines: list[str] = []
    env = command_env(cfg, include_api=include_api)

    if workflow_state:
        update_workflow_progress(workflow_state, stage_index=stage_index, stage_label=label, within_stage=0.0)
    elif cfg["debug"]:
        st.code(" ".join(cmd), language="bash")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            partial = line_progress(line)
            if workflow_state and partial is not None:
                update_workflow_progress(
                    workflow_state,
                    stage_index=stage_index,
                    stage_label=label,
                    within_stage=partial,
                )
            if show_logs:
                render_log_view(log_box, lines)
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = 124
        lines.append(f"Timed out after {timeout} seconds")

    elapsed = time.time() - start
    if code == 0:
        if workflow_state:
            update_workflow_progress(workflow_state, stage_index=stage_index, stage_label=f"{label}完成", within_stage=1.0)
        elif status:
            status.update(label=f"{label}完成，用时 {elapsed:.1f}s", state="complete", expanded=False)
    else:
        if status:
            status.update(label=f"{label}失败，退出码 {code}", state="error", expanded=True)
        if cfg.get("ui_mode") == "基础模式":
            st.error("运行已停止：" + (lines[-1] if lines else f"退出码 {code}"))
        else:
            render_log_view(log_box, lines)
    return CommandResult(code=code, elapsed=elapsed, lines=lines)


def save_uploaded_files(files: Iterable, input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for file in files:
        target = input_dir / file.name
        target.write_bytes(file.getbuffer())


def sidebar_config() -> dict:
    st.sidebar.header("工作模式")
    ui_mode = st.sidebar.radio(
        "模式",
        ["基础模式", "进阶模式", "调试模式"],
        index=0,
        horizontal=False,
        help="基础模式只保留日常使用入口；进阶模式可调模型和并发；调试模式显示单步命令和完整日志。",
    )
    debug = ui_mode == "调试模式"

    saved_api_key = load_saved_api_key()
    api_key = st.sidebar.text_input(
        "DeepSeek API key",
        type="password",
        help="基础模式下，粘贴 key 后点击“开始完整整理”即可。",
    )
    key_cols = st.sidebar.columns(2)
    if key_cols[0].button("保存 key", disabled=not bool(api_key), width="stretch"):
        save_api_key(api_key)
        saved_api_key = api_key
        st.sidebar.success("已保存到本项目 .local 目录")
    if key_cols[1].button("清除", disabled=not bool(saved_api_key), width="stretch"):
        clear_saved_api_key()
        saved_api_key = ""
        st.sidebar.success("已清除本地保存的 key")
    st.sidebar.caption("已保存 key" if saved_api_key else "未保存 key")

    cfg = dict(DEFAULTS)
    cfg.update(
        {
            "ui_mode": ui_mode,
            "debug": debug,
            "api_key": api_key or saved_api_key,
            "input_dir": str(DEFAULT_INPUT),
            "out_dir": str(DEFAULT_OUT),
        }
    )

    with st.sidebar.expander("文件夹", expanded=ui_mode != "基础模式"):
        cfg["input_dir"] = st.text_input("试卷文件夹", cfg["input_dir"])
        cfg["out_dir"] = st.text_input("输出文件夹", cfg["out_dir"])

    if ui_mode in {"进阶模式", "调试模式"}:
        with st.sidebar.expander("模型与接口", expanded=ui_mode == "进阶模式"):
            cfg["api_key_env"] = st.text_input("API Key 环境变量", cfg["api_key_env"])
            cfg["client"] = st.selectbox("调用方式", ["http", "auto", "sdk"], index=["http", "auto", "sdk"].index(cfg["client"]))
            cfg["base_url"] = st.text_input("Base URL", cfg["base_url"])
            cfg["segment_model"] = st.text_input("切分模型", cfg["segment_model"])
            cfg["score_model"] = st.text_input("评分模型", cfg["score_model"])
            cfg["review_model"] = st.text_input("复核模型", cfg["review_model"])
            cfg["enrich_model"] = st.text_input("讲解模型", cfg["enrich_model"])

        with st.sidebar.expander("并发与限流", expanded=True):
            cfg["segment_workers"] = st.slider("切分并发", 1, 32, cfg["segment_workers"])
            cfg["score_workers"] = st.slider("评分并发", 1, 32, cfg["score_workers"])
            cfg["enrich_workers"] = st.slider("讲解并发", 1, 32, cfg["enrich_workers"])
            cfg["max_retries"] = st.number_input("限流重试次数", 1, 30, cfg["max_retries"], step=1)
            cfg["force"] = st.checkbox("强制重跑已有结果", value=cfg["force"])
            cfg["reset_before_full_run"] = st.checkbox("完整整理前清空旧输出", value=cfg["reset_before_full_run"])

        with st.sidebar.expander("切分、复核与输出", expanded=False):
            cfg["segment_input"] = st.selectbox("切分方式", ["local", "rough", "full"], index=["local", "rough", "full"].index(cfg["segment_input"]))
            cfg["segment_warning_fallback"] = st.checkbox(
                "结构 WARN/FAIL 自动模型重切",
                value=cfg["segment_warning_fallback"],
                help="只重切异常试卷；PASS 和轻微 PASS* 不调用模型。",
            )
            cfg["answer_tail_chars"] = st.number_input("答案区字符数", 1000, 30000, cfg["answer_tail_chars"], step=1000)
            cfg["review_candidates"] = st.slider("复核候选数", 2, 12, cfg["review_candidates"])
            cfg["review_select"] = st.checkbox("启用 Pro 复核", value=cfg["review_select"])
            cfg["save_conversations"] = st.checkbox("保存 API 对话", value=cfg["save_conversations"])

        with st.sidebar.expander("Thinking 与 token", expanded=False):
            cfg["segment_thinking"] = st.selectbox("切分 thinking", ["disabled", "enabled", "omit"], index=0)
            cfg["score_thinking"] = st.selectbox("评分 thinking", ["disabled", "enabled", "omit"], index=0)
            cfg["review_thinking"] = st.selectbox("复核 thinking", ["enabled", "disabled", "omit"], index=0)
            cfg["enrich_thinking"] = st.selectbox("讲解 thinking", ["disabled", "enabled", "omit"], index=0)
            cfg["segment_reasoning"] = st.selectbox("切分推理强度", ["none", "low", "medium", "high"], index=0)
            cfg["score_reasoning"] = st.selectbox("评分推理强度", ["none", "low", "medium", "high"], index=0)
            cfg["review_reasoning"] = st.selectbox("复核推理强度", ["none", "low", "medium", "high"], index=2)
            cfg["enrich_reasoning"] = st.selectbox("讲解推理强度", ["none", "low", "medium", "high"], index=0)
            cfg["score_max_tokens"] = st.number_input("评分输出上限", 400, 4000, cfg["score_max_tokens"], step=100)
            cfg["review_max_tokens"] = st.number_input("复核输出上限", 800, 8000, cfg["review_max_tokens"], step=100)
            cfg["enrich_max_tokens"] = st.number_input("讲解输出上限", 1000, 10000, cfg["enrich_max_tokens"], step=100)
            cfg["show_output"] = st.selectbox("AI 输出显示", ["preview", "none", "full"], index=0)
            cfg["show_reasoning"] = st.selectbox("AI 思考显示", ["none", "preview", "full"], index=0)
            cfg["preview_chars"] = st.number_input("预览字符数", 200, 10000, cfg["preview_chars"], step=200)

    if ui_mode == "调试模式":
        cfg["show_output"] = "preview"
        cfg["show_reasoning"] = "preview"

    with st.sidebar.expander("数据管理", expanded=False):
        st.caption("输入和输出互不影响；清空前会再次确认。")
        if st.button("清空输入目录", key="clear-input", width="stretch"):
            clear_directory_dialog("输入目录", cfg["input_dir"])
        if st.button("清空输出目录", key="clear-output", width="stretch"):
            clear_directory_dialog("输出目录", cfg["out_dir"])
        if st.session_state.get("clear_message"):
            st.success(st.session_state.pop("clear_message"))

    return cfg


def output_paths(out_dir: Path) -> dict[str, Path]:
    assembled = out_dir / "assembled"
    return {
        "学生版": assembled / "final_selected_questions_with_answers.md",
        "教师版": assembled / "final_teacher_notes.md",
        "答案版": assembled / "final_answers_only.md",
        "质量报告": out_dir / "run_quality_report.md",
    }


def word_output_paths(out_dir: Path) -> dict[str, Path]:
    folder = out_dir / "docx_exports"
    return {
        "学生训练版": folder / "高三英语精选试题_学生版.docx",
        "教师讲解版": folder / "高三英语精选试题_教师讲解版.docx",
        "答案汇总版": folder / "高三英语精选试题_答案汇总版.docx",
    }


def show_completion_actions(cfg: dict) -> None:
    out_dir = Path(cfg["out_dir"])
    files = word_output_paths(out_dir)
    for label, path in files.items():
        state = "已生成" if path.exists() else "未生成"
        st.markdown(
            f"<div class='result-card'><strong>{label}</strong><br>"
            f"<span class='app-muted'>{html.escape(path.name)} · {file_size(path)} · {state}</span></div>",
            unsafe_allow_html=True,
        )
    if st.button("打开输出文件夹", type="primary", width="stretch", key="open-output-complete"):
        ok, message = open_output_folder(out_dir / "docx_exports")
        (st.success if ok else st.warning)(message)


def usage_totals(out_dir: Path) -> dict[str, int]:
    result = {"score_calls": 0, "score_tokens": 0, "enrich_calls": 0, "enrich_tokens": 0}
    for folder, prefix in [("scores", "score"), ("enrichments", "enrich")]:
        path = out_dir / folder
        if not path.exists():
            continue
        for file in path.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            usage = data.get("usage") or {}
            result[f"{prefix}_calls"] += 1
            result[f"{prefix}_tokens"] += int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
    return result


def show_status(cfg: dict) -> None:
    input_dir = Path(cfg["input_dir"])
    out_dir = Path(cfg["out_dir"])
    docx_count = count_files(input_dir, "*.docx")
    segment_rows = read_csv_rows(out_dir / "segment_index.csv")
    score_rows = read_csv_rows(out_dir / "score_index.csv")
    selected_rows = read_csv_rows(out_dir / "selected_items.csv")
    docx_exports = sum(path.exists() for path in word_output_paths(out_dir).values())
    final_ready = all(path.exists() for path in word_output_paths(out_dir).values())

    cols = st.columns(6)
    cols[0].metric("Word 试卷", docx_count)
    cols[1].metric("切分题型", len(segment_rows))
    cols[2].metric("AI 评分", len(score_rows))
    cols[3].metric("入选题", len(selected_rows))
    cols[4].metric("Word 输出", docx_exports)
    cols[5].metric("成品", "已生成" if final_ready else "未生成")

    st.caption(f"输入：`{input_dir}`")
    st.caption(f"输出：`{out_dir}`")


def upload_panel(cfg: dict) -> None:
    uploaded = st.file_uploader("添加 .docx 试卷", type=["docx"], accept_multiple_files=True)
    if uploaded and st.button("保存到 input_docx", width="stretch"):
        save_uploaded_files(uploaded, Path(cfg["input_dir"]))
        st.success(f"已保存 {len(uploaded)} 个文件")


def show_workflow_strip() -> None:
    steps = [
        ("01", "预检与切分"),
        ("02", "异常自动重切"),
        ("03", "评分与筛选"),
        ("04", "复核与讲解"),
        ("05", "修复与报告"),
        ("06", "导出 Word"),
    ]
    cards = "".join(f"<div class='workflow-step'><b>{num}</b>{label}</div>" for num, label in steps)
    st.markdown(f"<div class='workflow-strip'>{cards}</div>", unsafe_allow_html=True)


def run_sequence(cfg: dict, steps: list[tuple[str, str, bool]], *, init_first: bool = False) -> bool:
    ok = True
    total_start = time.time()
    st.markdown("#### 运行进度")
    workflow_state = {
        "total": len(steps),
        "started": total_start,
        "bar": st.progress(0.0, text="准备开始"),
        "detail": st.empty(),
        "log": st.empty() if cfg.get("ui_mode") != "基础模式" else None,
    }
    for index, (label, mode, include_api) in enumerate(steps):
        cmd = build_command(
            mode,
            cfg,
            init=init_first and index == 0,
            force=cfg["force"] and include_api,
        )
        result = run_command(
            cmd,
            cfg,
            label=label,
            include_api=include_api,
            workflow_state=workflow_state,
            stage_index=index,
        )
        if result.code != 0:
            ok = False
            break
    if ok:
        workflow_state["bar"].progress(1.0, text="全部完成 · 100%")
        workflow_state["detail"].markdown(f"**全部完成** · 总用时 {format_duration(time.time() - total_start)}")
        st.success("整理完成，三份 Word 成品已经生成。")
        show_completion_actions(cfg)
    return ok


def run_full_workflow(cfg: dict) -> None:
    if not cfg.get("api_key"):
        st.error("请先在左侧粘贴或保存 DeepSeek API key。")
        return
    raw_steps = [
        ("输入预检", "preflight", False),
        ("切分与质量门", "segment", True),
        ("AI 评分", "score", True),
        ("本地筛选", "select", False),
    ]
    if cfg["review_select"]:
        raw_steps.append(("Pro 复核", "review-select", True))
    raw_steps.extend([
        ("教师讲解", "enrich-selected", True),
        ("修复答案并组装", "repair-answers", False),
        ("生成质量报告", "quality-report", False),
        ("导出 Word", "export-docx", False),
    ])
    total = len(raw_steps)
    steps = [(f"{index}/{total} {label}", mode, include_api) for index, (label, mode, include_api) in enumerate(raw_steps, 1)]
    run_sequence(cfg, steps, init_first=cfg["reset_before_full_run"])


def run_local_finish(cfg: dict) -> None:
    steps = [
        ("本地修复答案", "repair-answers", False),
        ("本地生成质量报告", "quality-report", False),
        ("本地导出 Word", "export-docx", False),
    ]
    run_sequence(cfg, steps)


def run_acceptance(cfg: dict) -> None:
    steps: list[tuple[str, list[str]]] = [
        ("完整回归测试", [sys.executable, str(ROOT / "tests" / "run_tests.py")]),
        (
            "语法检查",
            [
                sys.executable,
                "-m",
                "py_compile",
                str(SCRIPT),
                str(ROOT / "gui_app.py"),
                str(QUALITY_SCRIPT),
            ],
        ),
        (
            "本地切分",
            [
                sys.executable,
                str(SCRIPT),
                cfg["input_dir"],
                "--out",
                str(CHECK_OUT),
                "--mode",
                "segment",
                "--init",
                "--segment-input",
                "local",
                "--no-segment-warning-fallback",
            ],
        ),
        ("切分质量检查", [sys.executable, str(QUALITY_SCRIPT), "--out", str(CHECK_OUT)]),
    ]

    local_cfg = dict(cfg)
    local_cfg["debug"] = cfg["debug"]
    ok = True
    start = time.time()
    for label, cmd in steps:
        result = run_command(cmd, local_cfg, label=label, include_api=False, timeout=300)
        if result.code != 0:
            ok = False
            break

    if ok:
        report = CHECK_OUT / "segment_quality_report.md"
        st.success(f"本地验收完成，用时 {time.time() - start:.1f}s")
        if report.exists():
            with st.expander("验收报告", expanded=True):
                st.caption(f"`{report}`")
                st.markdown(read_text(report, limit=60000))


def workbench_tab(cfg: dict) -> None:
    st.subheader("工作台")
    show_workflow_strip()
    show_status(cfg)
    upload_panel(cfg)

    st.markdown('<div class="app-section"></div>', unsafe_allow_html=True)
    if cfg["ui_mode"] == "基础模式":
        st.button(
            "一键开始完整整理",
            type="primary",
            width="stretch",
            on_click=lambda: st.session_state.update(run_full_requested=True),
        )
        st.caption("将自动完成预检、切分、评分、筛选、讲解、修复和 Word 导出。")
    else:
        cols = st.columns([2, 1])
        with cols[0]:
            st.button(
                "运行完整流程",
                type="primary",
                width="stretch",
                on_click=lambda: st.session_state.update(run_full_requested=True),
            )
        with cols[1]:
            st.button(
                "修复并重新导出",
                width="stretch",
                on_click=lambda: st.session_state.update(local_finish_requested=True),
            )

    if st.session_state.pop("run_full_requested", False):
        run_full_workflow(cfg)
    if st.session_state.pop("local_finish_requested", False):
        run_local_finish(cfg)

    if cfg["ui_mode"] != "基础模式":
        st.info("修复答案、质量报告和 Word 导出均为本地步骤，不会向模型发送 API key。")


def results_tab(cfg: dict) -> None:
    out_dir = Path(cfg["out_dir"])
    paths = output_paths(out_dir)
    st.subheader("Word 成品")
    word_paths = word_output_paths(out_dir)
    for label, path in word_paths.items():
        cols = st.columns([2.4, .8, .9])
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)) if path.exists() else "尚未生成"
        cols[0].markdown(
            f"<div class='result-card'><strong>{label}</strong><br>"
            f"<span class='app-muted'>{html.escape(path.name)} · {modified}</span></div>",
            unsafe_allow_html=True,
        )
        cols[1].write(file_size(path))
        if path.exists():
            cols[2].download_button(
                "下载 Word",
                data=path.read_bytes(),
                file_name=path.name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"download-docx-{path}",
                width="stretch",
            )
        else:
            cols[2].write("—")
    if st.button("打开 Word 输出文件夹", type="primary", width="stretch", key="open-output-results"):
        ok, message = open_output_folder(out_dir / "docx_exports")
        (st.success if ok else st.warning)(message)

    st.subheader("Markdown 与质量报告")
    for label, path in paths.items():
        cols = st.columns([2.4, .8, .9])
        cols[0].markdown(f"<div class='app-file-row'><strong>{label}</strong><br><span class='app-muted'>{rel(path)}</span></div>", unsafe_allow_html=True)
        cols[1].write(file_size(path))
        if path.exists():
            cols[2].download_button("下载", data=path.read_bytes(), file_name=path.name, mime="text/markdown", key=f"download-{label}-{path}", width="stretch")
        else:
            cols[2].write("—")

    previews = [path for path in paths.values() if path.suffix == ".md" and path.exists()]
    if previews:
        selected = st.selectbox("预览", previews, format_func=lambda p: p.name)
        with st.container(height=520, border=True):
            st.markdown(read_text(selected, limit=60000))


def cost_tab(cfg: dict) -> None:
    out_dir = Path(cfg["out_dir"])
    usage = usage_totals(out_dir)
    score_rows = read_csv_rows(out_dir / "score_index.csv")
    selected_rows = read_csv_rows(out_dir / "selected_items.csv")
    has_review = (out_dir / "review_select_notes.json").exists()
    review_calls = 9 if has_review else 0
    total_calls = usage["score_calls"] + usage["enrich_calls"] + review_calls
    total_tokens = usage["score_tokens"] + usage["enrich_tokens"]

    st.subheader("成本统计")
    cols = st.columns(4)
    cols[0].metric("总调用", total_calls)
    cols[1].metric("总 token", f"{total_tokens:,}")
    cols[2].metric("已评分", len(score_rows))
    cols[3].metric("已入选", len(selected_rows))

    data = [
        {"阶段": "segment", "调用": 0, "token": "0", "说明": "本地切分，结构异常时模型回退"},
        {"阶段": "score", "调用": usage["score_calls"], "token": str(usage["score_tokens"]), "说明": "AI 轻量评分"},
        {"阶段": "review-select", "调用": review_calls, "token": "—", "说明": "AI 复核，token 从质量报告复盘"},
        {"阶段": "enrich-selected", "调用": usage["enrich_calls"], "token": str(usage["enrich_tokens"]), "说明": "AI 教师讲解"},
        {"阶段": "repair-answers", "调用": 0, "token": "0", "说明": "本地"},
        {"阶段": "quality-report", "调用": 0, "token": "0", "说明": "本地"},
        {"阶段": "export-docx", "调用": 0, "token": "0", "说明": "本地"},
    ]
    st.dataframe(data, width="stretch", hide_index=True)

    report = out_dir / "run_quality_report.md"
    if report.exists():
        with st.expander("质量报告中的 API 用量段落", expanded=False):
            text = read_text(report)
            start = text.find("## 6. API 用量估算")
            if start >= 0:
                end = text.find("## ", start + 10)
                st.markdown(text[start : end if end >= 0 else len(text)])
            else:
                st.markdown(read_text(report, limit=30000))


def table_with_detail(title: str, rows: list[dict], path_key: str | None = None) -> None:
    st.subheader(title)
    if not rows:
        st.warning("暂无数据")
        return
    st.dataframe(rows, width="stretch", hide_index=True)
    if not path_key:
        return
    ids = [row.get("item_id", f"row-{i}") for i, row in enumerate(rows)]
    selected_id = st.selectbox("查看详情", ids, key=f"detail-{title}")
    row = rows[ids.index(selected_id)]
    path_value = row.get(path_key)
    if not path_value:
        return
    detail_path = Path(path_value)
    if not detail_path.is_absolute():
        detail_path = ROOT / detail_path
    st.caption(f"`{detail_path}`")
    if detail_path.suffix == ".json":
        st.json(read_json_file(detail_path))
    else:
        st.code(read_text(detail_path, limit=12000), language="text")


def review_tab(cfg: dict) -> None:
    out_dir = Path(cfg["out_dir"])
    view = st.radio("审核对象", ["切分", "评分", "入选", "复核理由"], horizontal=True)
    if view == "切分":
        rows = read_csv_rows(out_dir / "segment_index.csv")
        low_only = st.checkbox("只看低置信度或无答案", value=False)
        if low_only:
            rows = [
                row
                for row in rows
                if (float(row.get("confidence") or 0) < 0.75) or str(row.get("answer_count") or "0") == "0"
            ]
        table_with_detail("切分结果", rows, "segment_path")
    elif view == "评分":
        table_with_detail("评分结果", read_csv_rows(out_dir / "score_index.csv"))
    elif view == "入选":
        table_with_detail("入选题目", read_csv_rows(out_dir / "selected_items.csv"))
    else:
        notes = out_dir / "review_select_notes.json"
        if notes.exists():
            st.json(read_json_file(notes))
        else:
            st.warning("暂无复核理由")


def debug_tab(cfg: dict) -> None:
    st.subheader("调试")
    st.caption("单步按钮会显示命令和完整日志；本地按钮不会带入 API key。")
    out_dir = Path(cfg["out_dir"])
    has_input = count_files(Path(cfg["input_dir"]), "*.docx") > 0
    modes = [
        ("1. 输入预检", "preflight", False, has_input),
        ("2. 切分与质量门", "segment", True, has_input),
        ("3. AI 评分", "score", True, (out_dir / "segment_index.jsonl").exists()),
        ("4. 本地选择", "select", False, (out_dir / "score_index.jsonl").exists()),
        ("5. Pro 复核", "review-select", True, (out_dir / "selected_items.json").exists()),
        ("6. 补充讲解", "enrich-selected", True, (out_dir / "selected_items.json").exists()),
        ("7. 组装 Markdown", "assemble", False, (out_dir / "selected_items.json").exists()),
        ("8. 修复答案", "repair-answers", False, (out_dir / "segment_index.jsonl").exists()),
        ("9. 质量报告", "quality-report", False, (out_dir / "segment_index.csv").exists()),
        ("10. 导出 Word", "export-docx", False, (out_dir / "assembled").exists()),
    ]
    cols = st.columns(3)
    for index, (label, mode, include_api, ready) in enumerate(modes):
        with cols[index % 3]:
            if st.button(label, width="stretch", disabled=not ready):
                cmd = build_command(mode, cfg, force=cfg["force"] and include_api)
                run_command(cmd, cfg, label=label, include_api=include_api)


def acceptance_tab(cfg: dict) -> None:
    st.subheader("本地验收")
    st.caption(f"输出目录：`{CHECK_OUT}`")
    if st.button("运行本地验收", type="primary", width="stretch"):
        run_acceptance(cfg)


def main() -> None:
    st.set_page_config(page_title="高三英语模拟题自动整理", layout="wide")
    inject_css()
    cfg = sidebar_config()

    st.markdown(
        """
        <div class="app-hero">
          <h1>高三英语模拟题自动整理</h1>
          <p>从多份 Word 试卷到学生训练、教师讲解与答案汇总，一次完成。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("默认配置：本地优先切分 · 结构异常自动模型重切 · 切分/评分/讲解并发均为 16")

    if cfg["ui_mode"] == "基础模式":
        tabs = st.tabs(["运行", "结果", "本地验收"])
        with tabs[0]:
            workbench_tab(cfg)
        with tabs[1]:
            results_tab(cfg)
        with tabs[2]:
            acceptance_tab(cfg)
    elif cfg["ui_mode"] == "进阶模式":
        tabs = st.tabs(["运行", "结果", "成本统计", "本地验收"])
        with tabs[0]:
            workbench_tab(cfg)
        with tabs[1]:
            results_tab(cfg)
        with tabs[2]:
            cost_tab(cfg)
        with tabs[3]:
            acceptance_tab(cfg)
    else:
        tabs = st.tabs(["运行", "结果", "审核数据", "成本统计", "本地验收", "调试"])
        with tabs[0]:
            workbench_tab(cfg)
        with tabs[1]:
            results_tab(cfg)
        with tabs[2]:
            review_tab(cfg)
        with tabs[3]:
            cost_tab(cfg)
        with tabs[4]:
            acceptance_tab(cfg)
        with tabs[5]:
            debug_tab(cfg)


if __name__ == "__main__":
    main()
