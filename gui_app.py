#!/usr/bin/env python3
"""Streamlit GUI for the Gaokao English paper pipeline.

The app keeps the command-line pipeline as the source of truth, but presents a
teacher-friendly workflow by default. Advanced and debug controls are available
without making the first screen feel like a terminal.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import streamlit as st


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
    "segment_workers": 4,
    "score_workers": 2,
    "enrich_workers": 1,
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
}


@dataclass
class CommandResult:
    code: int
    elapsed: float
    lines: list[str]


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --app-border: rgba(15, 23, 42, 0.12);
            --app-muted: #64748b;
            --app-surface: #f8fafc;
        }
        .block-container {
            padding-top: 1.6rem;
            padding-bottom: 3rem;
            max-width: 1180px;
        }
        [data-testid="stSidebar"] {
            background: #f8fafc;
            border-right: 1px solid var(--app-border);
        }
        div[data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid var(--app-border);
            border-radius: 8px;
            padding: 0.8rem 0.9rem;
        }
        div[data-testid="stMetric"] label {
            color: var(--app-muted);
            font-size: 0.82rem;
        }
        div.stButton > button {
            border-radius: 8px;
            min-height: 2.7rem;
            font-weight: 650;
        }
        div[data-testid="stAlert"] {
            border-radius: 8px;
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
) -> CommandResult:
    status = st.status(label, expanded=cfg["debug"])
    log_box = st.empty()
    start = time.time()
    lines: list[str] = []
    env = command_env(cfg, include_api=include_api)

    with status:
        if cfg["debug"]:
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
                if cfg["debug"]:
                    log_box.code("\n".join(lines[-220:]), language="text")
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = 124
            lines.append(f"Timed out after {timeout} seconds")

    elapsed = time.time() - start
    if code == 0:
        status.update(label=f"{label}完成，用时 {elapsed:.1f}s", state="complete", expanded=False)
        if not cfg["debug"]:
            summary = important_lines(lines)
            if summary:
                st.code("\n".join(summary), language="text")
    else:
        status.update(label=f"{label}失败，退出码 {code}", state="error", expanded=True)
        st.code("\n".join(lines[-80:]) or "(no output)", language="text")
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
    if key_cols[0].button("保存 key", disabled=not bool(api_key), use_container_width=True):
        save_api_key(api_key)
        saved_api_key = api_key
        st.sidebar.success("已保存到本项目 .local 目录")
    if key_cols[1].button("清除", disabled=not bool(saved_api_key), use_container_width=True):
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
            cfg["enrich_workers"] = st.slider("讲解并发", 1, 16, cfg["enrich_workers"])
            cfg["max_retries"] = st.number_input("限流重试次数", 1, 30, cfg["max_retries"], step=1)
            cfg["force"] = st.checkbox("强制重跑已有结果", value=cfg["force"])
            cfg["reset_before_full_run"] = st.checkbox("完整整理前清空旧输出", value=cfg["reset_before_full_run"])

        with st.sidebar.expander("切分、复核与输出", expanded=False):
            cfg["segment_input"] = st.selectbox("切分方式", ["local", "rough", "full"], index=["local", "rough", "full"].index(cfg["segment_input"]))
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

    return cfg


def output_paths(out_dir: Path) -> dict[str, Path]:
    assembled = out_dir / "assembled"
    return {
        "学生版": assembled / "final_selected_questions_with_answers.md",
        "教师版": assembled / "final_teacher_notes.md",
        "答案版": assembled / "final_answers_only.md",
        "质量报告": out_dir / "run_quality_report.md",
    }


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
    docx_exports = count_files(out_dir / "docx_exports", "*.docx")
    final_ready = output_paths(out_dir)["学生版"].exists()

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
    if uploaded and st.button("保存到 input_docx", use_container_width=True):
        save_uploaded_files(uploaded, Path(cfg["input_dir"]))
        st.success(f"已保存 {len(uploaded)} 个文件")


def run_sequence(cfg: dict, steps: list[tuple[str, str, bool]], *, init_first: bool = False) -> bool:
    ok = True
    total_start = time.time()
    for index, (label, mode, include_api) in enumerate(steps):
        cmd = build_command(
            mode,
            cfg,
            init=init_first and index == 0,
            force=cfg["force"] and include_api,
        )
        result = run_command(cmd, cfg, label=label, include_api=include_api)
        if result.code != 0:
            ok = False
            break
    if ok:
        st.success(f"全部完成，用时 {time.time() - total_start:.1f}s")
    return ok


def run_full_workflow(cfg: dict) -> None:
    if not cfg.get("api_key"):
        st.error("请先在左侧粘贴或保存 DeepSeek API key。")
        return
    steps = [
        ("AI 整理", "stage1", True),
        ("本地修复答案", "repair-answers", False),
        ("本地生成质量报告", "quality-report", False),
        ("本地导出 Word", "export-docx", False),
    ]
    run_sequence(cfg, steps, init_first=cfg["reset_before_full_run"])


def run_local_finish(cfg: dict) -> None:
    steps = [
        ("本地修复答案", "repair-answers", False),
        ("本地生成质量报告", "quality-report", False),
        ("本地导出 Word", "export-docx", False),
    ]
    run_sequence(cfg, steps)


def run_acceptance(cfg: dict) -> None:
    tests = [
        ROOT / "tests" / "test_segment_tail_trim.py",
        ROOT / "tests" / "test_answer_extraction.py",
        ROOT / "tests" / "test_export_markdown_to_docx.py",
    ]
    steps: list[tuple[str, list[str]]] = [
        ("回归测试：读后续写尾部", [sys.executable, str(tests[0])]),
        ("回归测试：答案提取", [sys.executable, str(tests[1])]),
        ("回归测试：Word 导出", [sys.executable, str(tests[2])]),
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
    show_status(cfg)
    upload_panel(cfg)

    st.markdown('<div class="app-section"></div>', unsafe_allow_html=True)
    cols = st.columns([2, 1])
    with cols[0]:
        st.button(
            "开始完整整理",
            type="primary",
            use_container_width=True,
            on_click=lambda: st.session_state.update(run_full_requested=True),
        )
    with cols[1]:
        st.button(
            "只做本地收尾",
            use_container_width=True,
            on_click=lambda: st.session_state.update(local_finish_requested=True),
        )

    if st.session_state.pop("run_full_requested", False):
        run_full_workflow(cfg)
    if st.session_state.pop("local_finish_requested", False):
        run_local_finish(cfg)

    st.info("本地验收、答案修复、质量报告和 Word 导出会移除 API key 环境变量后运行。")


def results_tab(cfg: dict) -> None:
    out_dir = Path(cfg["out_dir"])
    paths = output_paths(out_dir)
    st.subheader("输出文件")

    for label, path in paths.items():
        cols = st.columns([2.2, 1, 1])
        cols[0].markdown(f"<div class='app-file-row'><strong>{label}</strong><br><span class='app-muted'>{rel(path)}</span></div>", unsafe_allow_html=True)
        cols[1].write(file_size(path))
        if path.exists():
            cols[2].download_button(
                "下载",
                data=path.read_bytes(),
                file_name=path.name,
                mime="text/markdown",
                key=f"download-{label}-{path}",
                use_container_width=True,
            )
        else:
            cols[2].write("—")

    docx_dir = out_dir / "docx_exports"
    docx_files = sorted(docx_dir.glob("*.docx")) if docx_dir.exists() else []
    if docx_files:
        st.subheader("Word 文档")
        for path in docx_files:
            cols = st.columns([2.2, 1, 1])
            cols[0].markdown(f"<div class='app-file-row'><strong>{path.name}</strong><br><span class='app-muted'>{rel(path)}</span></div>", unsafe_allow_html=True)
            cols[1].write(file_size(path))
            cols[2].download_button(
                "下载",
                data=path.read_bytes(),
                file_name=path.name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"download-docx-{path}",
                use_container_width=True,
            )

    previews = [path for path in paths.values() if path.suffix == ".md" and path.exists()]
    if previews:
        selected = st.selectbox("预览", previews, format_func=lambda p: p.name)
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
        {"阶段": "segment", "调用": 0, "token": 0, "说明": "本地切分"},
        {"阶段": "score", "调用": usage["score_calls"], "token": usage["score_tokens"], "说明": "AI 轻量评分"},
        {"阶段": "review-select", "调用": review_calls, "token": "—", "说明": "AI 复核，token 从质量报告复盘"},
        {"阶段": "enrich-selected", "调用": usage["enrich_calls"], "token": usage["enrich_tokens"], "说明": "AI 教师讲解"},
        {"阶段": "repair-answers", "调用": 0, "token": 0, "说明": "本地"},
        {"阶段": "quality-report", "调用": 0, "token": 0, "说明": "本地"},
        {"阶段": "export-docx", "调用": 0, "token": 0, "说明": "本地"},
    ]
    st.dataframe(data, use_container_width=True, hide_index=True)

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
    st.dataframe(rows, use_container_width=True, hide_index=True)
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
    modes = [
        ("初始化输出目录", "init-only", False),
        ("预检", "preflight", False),
        ("切分", "segment", False),
        ("评分", "score", True),
        ("本地选择", "select", False),
        ("Pro 复核", "review-select", True),
        ("补充讲解", "enrich-selected", True),
        ("组装 Markdown", "assemble", False),
        ("修复答案", "repair-answers", False),
        ("质量报告", "quality-report", False),
        ("导出 Word", "export-docx", False),
    ]
    cols = st.columns(3)
    for index, (label, mode, include_api) in enumerate(modes):
        with cols[index % 3]:
            if st.button(label, use_container_width=True):
                if mode == "init-only":
                    cmd = [
                        sys.executable,
                        str(SCRIPT),
                        cfg["input_dir"],
                        "--out",
                        cfg["out_dir"],
                        "--init-only",
                    ]
                else:
                    cmd = build_command(mode, cfg, force=cfg["force"] and include_api)
                run_command(cmd, cfg, label=label, include_api=include_api)


def acceptance_tab(cfg: dict) -> None:
    st.subheader("本地验收")
    st.caption(f"输出目录：`{CHECK_OUT}`")
    if st.button("运行本地验收", type="primary", use_container_width=True):
        run_acceptance(cfg)


def main() -> None:
    st.set_page_config(page_title="高三英语模拟题自动整理", layout="wide")
    inject_css()
    cfg = sidebar_config()

    st.title("高三英语模拟题自动整理工具")
    st.caption("当前默认：本地切分，评分并发 2，讲解并发 1，限流重试 12。")

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
