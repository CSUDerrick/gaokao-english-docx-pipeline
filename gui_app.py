#!/usr/bin/env python3
"""Streamlit GUI for the Gaokao English paper pipeline.

The GUI intentionally calls the existing command-line pipeline as a subprocess.
That keeps the TUI and GUI behavior identical and makes terminal debugging
straightforward.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import streamlit as st


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "gaokao_english_docx_pipeline.py"
DEFAULT_INPUT = ROOT / "input_docx"
DEFAULT_OUT = ROOT / "outputs" / "gaokao_english"
SECRET_PATH = ROOT / ".local" / "gui_secrets.json"


TEXT = {
    "zh": {
        "language": "界面语言 / Language",
        "title": "高三英语试卷整理流水线",
        "params": "参数",
        "paths_api": "路径与 API",
        "input_dir": "输入文件夹",
        "input_dir_help": "存放原始 docx 试卷的文件夹。GUI 上传文件也会保存到这里。",
        "out_dir": "输出文件夹",
        "out_dir_help": "保存切割结果、评分结果、最终 Markdown 和 API 对话记录的文件夹。",
        "api_key_env": "API Key 环境变量",
        "api_key_env_help": "脚本会从这个环境变量读取 DeepSeek API Key。默认 DEEPSEEK_API_KEY。",
        "api_key": "API Key（可选，不保存）",
        "api_key_help": "临时输入 API Key。若点击保存，会记忆到本项目 .local/gui_secrets.json；否则只传给本次运行。",
        "api_key_saved": "已保存 API Key。留空也会使用已保存的 Key。",
        "api_key_not_saved": "尚未保存 API Key。",
        "save_api_key": "保存 API Key",
        "clear_api_key": "清除 Key",
        "api_key_saved_ok": "API Key 已保存到本地。",
        "api_key_cleared": "已清除本地保存的 API Key。",
        "client": "调用方式 client",
        "client_help": "http 不需要安装 openai 包；sdk 使用 OpenAI SDK；auto 优先 SDK，失败时使用 HTTP。",
        "base_url_help": "DeepSeek API 根地址，默认 https://api.deepseek.com。",
        "models": "模型",
        "segment_model": "切割模型 segment model",
        "segment_model_help": "负责把整份试卷切成阅读A/B/C/D、七选五、完形、语法填空、写作等题目单元。默认用便宜快速的 deepseek-v4-flash。",
        "score_model": "评分模型 score model",
        "score_model_help": "负责给每道题评分，输出题材新颖度、难度、词汇价值、语法价值等。默认 deepseek-v4-flash。",
        "review_model": "复核模型 review model",
        "review_model_help": "负责在本地初筛候选中做最终选择。默认 deepseek-v4-pro。",
        "enrich_model": "精讲补充模型 enrich model",
        "enrich_model_help": "只对最终入选题目补充重点词汇、词形语法和长难句。默认 deepseek-v4-flash，成本较低。",
        "concurrency": "并发与切割",
        "segment_workers": "切割并发数 segment workers",
        "segment_workers_help": "本地切割时是本地并发；AI 切割时是 API 并发。建议先用 4。",
        "score_workers": "评分并发数 score workers",
        "score_workers_help": "同时评分多少道题。为避免 429，建议先用 4；如果仍限流，降到 2。",
        "enrich_workers": "精讲补充并发数 enrich workers",
        "enrich_workers_help": "同时给多少个最终入选题目补充词汇/语法/长难句。建议先用 2；如果仍限流，降到 1。",
        "segment_input": "切割输入方式 segment input",
        "segment_input_help": "local 表示本地按高考试卷结构切割，不调用 AI，最省 token；rough/full 才会调用 AI 切割。",
        "answer_tail_chars": "答案区字符数 answer tail chars",
        "answer_tail_chars_help": "从试卷末尾提取多少字符作为答案区，附到粗切单元后，帮助 AI 匹配答案。默认 8000。",
        "review_candidates": "复核候选数量 review candidates",
        "review_candidates_help": "每个题型先按评分取前几个候选，再交给 pro 复核。默认 6。",
        "thinking": "思考模式 / 推理强度",
        "thinking_tip": "建议：segment 和 score 显式关闭 thinking；review-select 默认开启；enrich 默认关闭以控制成本。",
        "segment_thinking": "切割思考模式 segment thinking",
        "segment_thinking_help": "控制 AI 切割是否启用模型思考。local 切割不调用模型；disabled 是显式关闭，避免默认开启产生 reasoning token。",
        "score_thinking": "评分思考模式 score thinking",
        "score_thinking_help": "控制评分任务是否启用模型思考。默认 disabled，显式关闭，适合大批量低成本评分。",
        "review_thinking": "复核思考模式 review thinking",
        "review_thinking_help": "控制最终候选复核是否启用模型思考。默认 enabled，提高最终选题质量。",
        "enrich_thinking": "精讲补充思考模式 enrich thinking",
        "enrich_thinking_help": "控制最终入选题目补充词汇/语法时是否启用模型思考。默认 disabled，通常已经足够。",
        "segment_reasoning": "切割推理强度 segment reasoning",
        "segment_reasoning_help": "如果启用 thinking，控制切割任务的推理强度。默认 none。",
        "score_reasoning": "评分推理强度 score reasoning",
        "score_reasoning_help": "如果启用 thinking，控制评分任务的推理强度。默认 none。",
        "review_reasoning": "复核推理强度 review reasoning",
        "review_reasoning_help": "最终复核的推理强度。默认 medium，平衡质量和成本。",
        "enrich_reasoning": "精讲补充推理强度 enrich reasoning",
        "enrich_reasoning_help": "如果启用 enrich thinking，控制补充讲解材料的推理强度。默认 none。",
        "token_caps": "输出长度上限",
        "score_max_tokens": "评分输出上限 score max tokens",
        "score_max_tokens_help": "每个评分调用最多输出多少 token。轻量评分默认 1200，防止模型啰嗦。",
        "review_max_tokens": "复核输出上限 review max tokens",
        "review_max_tokens_help": "每个题型复核最多输出多少 token。默认 2500。",
        "enrich_max_tokens": "精讲输出上限 enrich max tokens",
        "enrich_max_tokens_help": "每个入选题目补充讲解最多输出多少 token。默认 3500。",
        "max_retries": "限流重试次数 max retries",
        "max_retries_help": "遇到 429 Too Many Requests 或临时网络错误时最多重试几次。默认 8，会自动等待后再试。",
        "logs": "日志",
        "show_output": "AI 输出显示 AI output",
        "show_output_help": "控制日志里显示多少 AI 最终输出。preview 只显示预览，full 显示完整，none 不显示。",
        "show_reasoning": "AI 思考内容显示 AI reasoning",
        "show_reasoning_help": "如果 API 返回 reasoning/thinking 内容，控制显示多少。不是所有接口都会返回。",
        "preview_chars": "预览字符数 preview chars",
        "preview_chars_help": "preview 模式下最多显示多少字符。",
        "save_conversations": "保存 API 对话 Markdown",
        "save_conversations_help": "保存每次调用的 prompt、模型输出和 usage，便于复盘和调试。",
        "review_select": "stage1 启用 pro 复核",
        "review_select_help": "一键运行 stage1 时，是否在本地初筛后调用 pro 复核最终选题。",
        "running": "运行中",
        "done": "完成，用时 {elapsed:.1f}s",
        "failed": "失败，退出码 {code}，用时 {elapsed:.1f}s",
        "status": "状态",
        "run": "运行",
        "review": "审核",
        "results": "结果",
        "final_yes": "已生成",
        "final_no": "未生成",
        "root": "项目根目录",
        "input": "输入",
        "output": "输出",
        "run_title": "运行",
        "run_info": "GUI 会调用同一个命令行脚本；TUI 和 GUI 输出保持一致。",
        "upload": "上传 docx 到 input_docx",
        "save_uploads": "保存上传文件",
        "saved_files": "已保存 {count} 个文件。",
        "init_before": "运行前初始化输出目录",
        "init_before_help": "勾选后，本次运行前会清空输出目录，适合重新开始完整流程。",
        "force": "强制重跑已存在结果",
        "force_help": "勾选后，即使某些结果文件已存在，也会重新调用模型生成。",
        "initialize": "初始化 initialize",
        "preflight": "预检 preflight",
        "segment": "切割 segment",
        "score": "评分 score",
        "select": "本地选择 select",
        "review_select_btn": "Pro 复核 review-select",
        "enrich_selected": "补充精讲 enrich",
        "assemble": "组装 assemble",
        "repair_answers_btn": "修复答案 repair-answers",
        "quality_report_btn": "质量报告 quality-report",
        "stage1": "一键运行 stage1",
        "no_data": "暂无数据。",
        "view_detail": "查看 {title} 详情",
        "review_target": "审核对象",
        "segmentation": "切割",
        "scoring": "评分",
        "selected": "入选",
        "review_notes": "复核理由",
        "low_only": "只看低置信度或无答案",
        "low_only_help": "优先检查 confidence 低于 0.75 或 answer_count 为 0 的切割结果。",
        "segment_table": "切割审核表",
        "segment_help": "重点检查：阅读A/B/C/D是否切对；七选五选项是否保留；答案数量是否为0；应用文和读后续写是否分开；confidence 低于 0.75 的项目优先检查。",
        "score_table": "评分审核表",
        "score_help": "重点检查：难度分是否合理；新颖度是否符合你的判断；推荐度是否偏高或偏低；题型是否错位。",
        "selected_table": "入选题目",
        "selected_help": "这里展示最终入选题目。若启用了 review-select，会显示 pro 复核后的结果。",
        "no_review_notes": "暂无 review_select_notes.json。",
        "output_files": "输出文件",
        "download": "下载 {name}",
        "preview_md": "预览 Markdown",
        "final_set_note": "学生训练用：题目在前，答案统一在最后。",
        "teacher_note": "教师讲解用：包含评分、入选理由、重点词汇和语法点。",
        "answers_note": "仅答案汇总。",
    },
    "en": {
        "language": "Interface Language / 界面语言",
        "title": "Gaokao English Paper Pipeline",
        "params": "Settings",
        "paths_api": "Paths & API",
        "input_dir": "Input folder",
        "input_dir_help": "Folder containing the original docx papers. Uploaded files are saved here.",
        "out_dir": "Output folder",
        "out_dir_help": "Folder for segments, scores, final Markdown files, and API conversation logs.",
        "api_key_env": "API key environment variable",
        "api_key_env_help": "The script reads the DeepSeek API key from this environment variable. Default: DEEPSEEK_API_KEY.",
        "api_key": "API key (optional, not saved)",
        "api_key_help": "Temporary key. If you click save, it is stored in this project's .local/gui_secrets.json; otherwise it is used only for this run.",
        "api_key_saved": "API key saved. Leave the input empty to use the saved key.",
        "api_key_not_saved": "No saved API key.",
        "save_api_key": "Save API key",
        "clear_api_key": "Clear key",
        "api_key_saved_ok": "API key saved locally.",
        "api_key_cleared": "Saved API key cleared.",
        "client": "Client",
        "client_help": "http requires no openai package; sdk uses the OpenAI SDK; auto tries SDK first, then HTTP.",
        "base_url_help": "DeepSeek API root URL. Default: https://api.deepseek.com.",
        "models": "Models",
        "segment_model": "Segment model",
        "segment_model_help": "Splits a full paper into Reading A/B/C/D, gap filling, cloze, grammar, and writing items. Default: fast and cheaper deepseek-v4-flash.",
        "score_model": "Score model",
        "score_model_help": "Scores each item for novelty, difficulty, vocabulary value, grammar value, etc. Default: deepseek-v4-flash.",
        "review_model": "Review model",
        "review_model_help": "Reviews local shortlist candidates and makes the final selections. Default: deepseek-v4-pro.",
        "enrich_model": "Enrich model",
        "enrich_model_help": "Adds vocabulary, grammar/word-formation, and long-sentence notes only for final selected items. Default: deepseek-v4-flash.",
        "concurrency": "Concurrency & Segmentation",
        "segment_workers": "Segment workers",
        "segment_workers_help": "Local concurrency for local segmentation; API concurrency for AI segmentation. Start with 4.",
        "score_workers": "Score workers",
        "score_workers_help": "How many items to score concurrently. Start with 4 to avoid 429; reduce to 2 if still rate-limited.",
        "enrich_workers": "Enrich workers",
        "enrich_workers_help": "How many final selected items to enrich concurrently. Start with 2; reduce to 1 if still rate-limited.",
        "segment_input": "Segment input",
        "segment_input_help": "local parses common exam structure without AI and saves the most tokens; rough/full call AI segmentation.",
        "answer_tail_chars": "Answer tail chars",
        "answer_tail_chars_help": "Characters extracted from the final answer area and appended to rough chunks to help AI match answers. Default: 8000.",
        "review_candidates": "Review candidates",
        "review_candidates_help": "Number of local top candidates per section sent to pro review. Default: 6.",
        "thinking": "Thinking / Reasoning Effort",
        "thinking_tip": "Recommendation: segment and score explicitly disable thinking; review-select can keep thinking on; enrich is off by default to control cost.",
        "segment_thinking": "Segment thinking",
        "segment_thinking_help": "Whether AI segmentation uses thinking. local segmentation does not call the model; disabled explicitly avoids default reasoning tokens.",
        "score_thinking": "Score thinking",
        "score_thinking_help": "Whether scoring uses model thinking. Default disabled is best for low-cost batch scoring.",
        "review_thinking": "Review thinking",
        "review_thinking_help": "Whether to enable model thinking for final candidate review. Default enabled improves final selection quality.",
        "enrich_thinking": "Enrich thinking",
        "enrich_thinking_help": "Whether selected-item enrichment uses model thinking. Default disabled is usually enough.",
        "segment_reasoning": "Segment reasoning",
        "segment_reasoning_help": "Reasoning effort for segmentation if thinking is enabled. Default none.",
        "score_reasoning": "Score reasoning",
        "score_reasoning_help": "Reasoning effort for scoring if thinking is enabled. Default none.",
        "review_reasoning": "Review reasoning",
        "review_reasoning_help": "Reasoning effort for final review. Default medium balances quality and cost.",
        "enrich_reasoning": "Enrich reasoning",
        "enrich_reasoning_help": "Reasoning effort for enrichment if thinking is enabled. Default none.",
        "token_caps": "Output token caps",
        "score_max_tokens": "Score max tokens",
        "score_max_tokens_help": "Maximum output tokens per score call. Lightweight score default is 1200.",
        "review_max_tokens": "Review max tokens",
        "review_max_tokens_help": "Maximum output tokens per review call. Default: 2500.",
        "enrich_max_tokens": "Enrich max tokens",
        "enrich_max_tokens_help": "Maximum output tokens per selected-item enrichment call. Default: 3500.",
        "max_retries": "Max retries",
        "max_retries_help": "Maximum retries for 429 Too Many Requests or temporary network errors. Default: 8, with automatic backoff.",
        "logs": "Logs",
        "show_output": "AI output",
        "show_output_help": "How much final AI output to show in logs. preview is partial, full is complete, none hides it.",
        "show_reasoning": "AI reasoning",
        "show_reasoning_help": "How much returned reasoning/thinking content to show. Not all APIs return it.",
        "preview_chars": "Preview chars",
        "preview_chars_help": "Maximum characters shown in preview mode.",
        "save_conversations": "Save API conversations as Markdown",
        "save_conversations_help": "Save each prompt, model output, and usage for review/debugging.",
        "review_select": "Enable pro review in stage1",
        "review_select_help": "When running stage1, call pro review after local shortlist selection.",
        "running": "Running",
        "done": "Done in {elapsed:.1f}s",
        "failed": "Failed with exit code {code} in {elapsed:.1f}s",
        "status": "Status",
        "run": "Run",
        "review": "Review",
        "results": "Results",
        "final_yes": "yes",
        "final_no": "no",
        "root": "Project root",
        "input": "Input",
        "output": "Output",
        "run_title": "Run",
        "run_info": "The GUI calls the same command-line script, so TUI and GUI behavior stay consistent.",
        "upload": "Upload docx to input_docx",
        "save_uploads": "Save uploaded files",
        "saved_files": "Saved {count} file(s).",
        "init_before": "Initialize output before run",
        "init_before_help": "Clear the output folder before this run. Useful when restarting the full pipeline.",
        "force": "Force rerun existing results",
        "force_help": "Regenerate results even when output files already exist.",
        "initialize": "Initialize",
        "preflight": "Preflight",
        "segment": "Segment",
        "score": "Score",
        "select": "Local select",
        "review_select_btn": "Pro review",
        "enrich_selected": "Enrich selected",
        "assemble": "Assemble",
        "repair_answers_btn": "Repair answers",
        "quality_report_btn": "Quality report",
        "stage1": "Run stage1",
        "no_data": "No data yet.",
        "view_detail": "View {title} detail",
        "review_target": "Review target",
        "segmentation": "Segmentation",
        "scoring": "Scoring",
        "selected": "Selected",
        "review_notes": "Review notes",
        "low_only": "Only low confidence or no answer",
        "low_only_help": "Prioritize rows with confidence below 0.75 or answer_count equal to 0.",
        "segment_table": "Segment review table",
        "segment_help": "Check: Reading A/B/C/D boundaries; gap-filling options; missing answers; application writing vs continuation writing; low confidence rows first.",
        "score_table": "Score review table",
        "score_help": "Check whether difficulty, novelty, recommendation scores, and section labels match your judgment.",
        "selected_table": "Selected items",
        "selected_help": "Final selected items. If review-select was enabled, this shows the pro-reviewed result.",
        "no_review_notes": "No review_select_notes.json yet.",
        "output_files": "Output files",
        "download": "Download {name}",
        "preview_md": "Preview Markdown",
        "final_set_note": "Student practice version: questions first, all answers at the end.",
        "teacher_note": "Teacher notes: scores, reasons, vocabulary, and grammar points.",
        "answers_note": "Answers only.",
    },
}


def t(cfg_or_lang: dict | str, key: str, **kwargs) -> str:
    lang = cfg_or_lang.get("lang", "zh") if isinstance(cfg_or_lang, dict) else cfg_or_lang
    text = TEXT.get(lang, TEXT["zh"]).get(key, TEXT["zh"].get(key, key))
    return text.format(**kwargs) if kwargs else text


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
        return text[:limit] + f"\n\n... truncated {len(text) - limit} characters"
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
        return "missing"
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


def run_command(cmd: list[str], cfg: dict) -> int:
    log_box = st.empty()
    status = st.status(t(cfg, "running"), expanded=True)
    env = os.environ.copy()
    if cfg.get("api_key"):
        env[cfg["api_key_env"]] = cfg["api_key"]

    lines: list[str] = []
    start = time.time()
    with status:
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
    for line in proc.stdout:
        lines.append(line.rstrip("\n"))
        log_box.code("\n".join(lines[-250:]), language="text")
    code = proc.wait()
    elapsed = time.time() - start
    if code == 0:
        status.update(label=t(cfg, "done", elapsed=elapsed), state="complete", expanded=False)
    else:
        status.update(label=t(cfg, "failed", code=code, elapsed=elapsed), state="error", expanded=True)
    return code


def save_uploaded_files(files: Iterable, input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for file in files:
        target = input_dir / file.name
        target.write_bytes(file.getbuffer())


def sidebar_config() -> dict:
    current_lang = st.session_state.get("lang", "zh")
    lang_choice = st.sidebar.selectbox(t(current_lang, "language"), ["中文", "English"], index=0 if current_lang == "zh" else 1)
    lang_label = "中文" if lang_choice == "中文" else "English"
    lang = "zh" if lang_label == "中文" else "en"
    st.session_state["lang"] = lang
    st.sidebar.header(t(lang, "params"))
    st.sidebar.subheader(t(lang, "paths_api"))
    input_dir = st.sidebar.text_input(t(lang, "input_dir"), str(DEFAULT_INPUT), help=t(lang, "input_dir_help"))
    out_dir = st.sidebar.text_input(t(lang, "out_dir"), str(DEFAULT_OUT), help=t(lang, "out_dir_help"))
    api_key_env = st.sidebar.text_input(t(lang, "api_key_env"), "DEEPSEEK_API_KEY", help=t(lang, "api_key_env_help"))
    saved_api_key = load_saved_api_key()
    st.sidebar.caption(t(lang, "api_key_saved") if saved_api_key else t(lang, "api_key_not_saved"))
    api_key = st.sidebar.text_input(t(lang, "api_key"), type="password", help=t(lang, "api_key_help"))
    key_cols = st.sidebar.columns(2)
    if key_cols[0].button(t(lang, "save_api_key"), disabled=not bool(api_key)):
        save_api_key(api_key)
        saved_api_key = api_key
        st.sidebar.success(t(lang, "api_key_saved_ok"))
    if key_cols[1].button(t(lang, "clear_api_key"), disabled=not bool(saved_api_key)):
        clear_saved_api_key()
        saved_api_key = ""
        st.sidebar.success(t(lang, "api_key_cleared"))
    client = st.sidebar.selectbox(t(lang, "client"), ["http", "auto", "sdk"], index=0, help=t(lang, "client_help"))
    base_url = st.sidebar.text_input("Base URL", "https://api.deepseek.com", help=t(lang, "base_url_help"))

    st.sidebar.subheader(t(lang, "models"))
    segment_model = st.sidebar.text_input(t(lang, "segment_model"), "deepseek-v4-flash", help=t(lang, "segment_model_help"))
    score_model = st.sidebar.text_input(t(lang, "score_model"), "deepseek-v4-flash", help=t(lang, "score_model_help"))
    review_model = st.sidebar.text_input(t(lang, "review_model"), "deepseek-v4-pro", help=t(lang, "review_model_help"))
    enrich_model = st.sidebar.text_input(t(lang, "enrich_model"), "deepseek-v4-flash", help=t(lang, "enrich_model_help"))

    st.sidebar.subheader(t(lang, "concurrency"))
    segment_workers = st.sidebar.slider(t(lang, "segment_workers"), 1, 32, 4, help=t(lang, "segment_workers_help"))
    score_workers = st.sidebar.slider(t(lang, "score_workers"), 1, 64, 4, help=t(lang, "score_workers_help"))
    enrich_workers = st.sidebar.slider(t(lang, "enrich_workers"), 1, 32, 2, help=t(lang, "enrich_workers_help"))
    segment_input = st.sidebar.selectbox(t(lang, "segment_input"), ["local", "rough", "full"], index=0, help=t(lang, "segment_input_help"))
    answer_tail_chars = st.sidebar.number_input(t(lang, "answer_tail_chars"), 1000, 30000, 8000, step=1000, help=t(lang, "answer_tail_chars_help"))
    review_candidates = st.sidebar.slider(t(lang, "review_candidates"), 2, 12, 6, help=t(lang, "review_candidates_help"))

    st.sidebar.subheader(t(lang, "thinking"))
    st.sidebar.caption(t(lang, "thinking_tip"))
    segment_thinking = st.sidebar.selectbox(t(lang, "segment_thinking"), ["disabled", "enabled", "omit"], index=0, help=t(lang, "segment_thinking_help"))
    score_thinking = st.sidebar.selectbox(t(lang, "score_thinking"), ["disabled", "enabled", "omit"], index=0, help=t(lang, "score_thinking_help"))
    review_thinking = st.sidebar.selectbox(t(lang, "review_thinking"), ["enabled", "omit", "disabled"], index=0, help=t(lang, "review_thinking_help"))
    enrich_thinking = st.sidebar.selectbox(t(lang, "enrich_thinking"), ["disabled", "enabled", "omit"], index=0, help=t(lang, "enrich_thinking_help"))
    segment_reasoning = st.sidebar.selectbox(t(lang, "segment_reasoning"), ["none", "low", "medium", "high"], index=0, help=t(lang, "segment_reasoning_help"))
    score_reasoning = st.sidebar.selectbox(t(lang, "score_reasoning"), ["none", "low", "medium", "high"], index=0, help=t(lang, "score_reasoning_help"))
    review_reasoning = st.sidebar.selectbox(t(lang, "review_reasoning"), ["none", "low", "medium", "high"], index=2, help=t(lang, "review_reasoning_help"))
    enrich_reasoning = st.sidebar.selectbox(t(lang, "enrich_reasoning"), ["none", "low", "medium", "high"], index=0, help=t(lang, "enrich_reasoning_help"))

    st.sidebar.subheader(t(lang, "token_caps"))
    score_max_tokens = st.sidebar.number_input(t(lang, "score_max_tokens"), 400, 4000, 1200, step=100, help=t(lang, "score_max_tokens_help"))
    review_max_tokens = st.sidebar.number_input(t(lang, "review_max_tokens"), 800, 8000, 2500, step=100, help=t(lang, "review_max_tokens_help"))
    enrich_max_tokens = st.sidebar.number_input(t(lang, "enrich_max_tokens"), 1000, 10000, 3500, step=100, help=t(lang, "enrich_max_tokens_help"))
    max_retries = st.sidebar.number_input(t(lang, "max_retries"), 1, 20, 8, step=1, help=t(lang, "max_retries_help"))

    st.sidebar.subheader(t(lang, "logs"))
    show_output = st.sidebar.selectbox(t(lang, "show_output"), ["preview", "none", "full"], index=0, help=t(lang, "show_output_help"))
    show_reasoning = st.sidebar.selectbox(t(lang, "show_reasoning"), ["preview", "none", "full"], index=0, help=t(lang, "show_reasoning_help"))
    preview_chars = st.sidebar.number_input(t(lang, "preview_chars"), 200, 10000, 1200, step=200, help=t(lang, "preview_chars_help"))
    save_conversations = st.sidebar.checkbox(t(lang, "save_conversations"), value=True, help=t(lang, "save_conversations_help"))
    review_select = st.sidebar.checkbox(t(lang, "review_select"), value=True, help=t(lang, "review_select_help"))

    return {
        "lang": lang,
        "input_dir": input_dir,
        "out_dir": out_dir,
        "api_key_env": api_key_env,
        "api_key": api_key or saved_api_key,
        "client": client,
        "base_url": base_url,
        "segment_model": segment_model,
        "score_model": score_model,
        "review_model": review_model,
        "enrich_model": enrich_model,
        "segment_workers": segment_workers,
        "score_workers": score_workers,
        "enrich_workers": enrich_workers,
        "segment_input": segment_input,
        "answer_tail_chars": answer_tail_chars,
        "review_candidates": review_candidates,
        "segment_thinking": segment_thinking,
        "score_thinking": score_thinking,
        "review_thinking": review_thinking,
        "enrich_thinking": enrich_thinking,
        "segment_reasoning": segment_reasoning,
        "score_reasoning": score_reasoning,
        "review_reasoning": review_reasoning,
        "enrich_reasoning": enrich_reasoning,
        "score_max_tokens": score_max_tokens,
        "review_max_tokens": review_max_tokens,
        "enrich_max_tokens": enrich_max_tokens,
        "max_retries": max_retries,
        "show_output": show_output,
        "show_reasoning": show_reasoning,
        "preview_chars": preview_chars,
        "save_conversations": save_conversations,
        "review_select": review_select,
    }


def show_status(cfg: dict) -> None:
    input_dir = Path(cfg["input_dir"])
    out_dir = Path(cfg["out_dir"])
    docx_count = count_files(input_dir, "*.docx")
    segment_rows = read_csv_rows(out_dir / "segment_index.csv")
    score_rows = read_csv_rows(out_dir / "score_index.csv")
    selected_rows = read_csv_rows(out_dir / "selected_items.csv")
    final_path = out_dir / "assembled" / "final_selected_questions_with_answers.md"

    cols = st.columns(5)
    cols[0].metric("docx", docx_count)
    cols[1].metric("segments", len(segment_rows))
    cols[2].metric("scores", len(score_rows))
    cols[3].metric("selected", len(selected_rows))
    cols[4].metric("final", t(cfg, "final_yes") if final_path.exists() else t(cfg, "final_no"))

    st.caption(f"{t(cfg, 'root')}：`{ROOT}`")
    st.caption(f"{t(cfg, 'input')}：`{input_dir}`")
    st.caption(f"{t(cfg, 'output')}：`{out_dir}`")


def run_tab(cfg: dict) -> None:
    st.subheader(t(cfg, "run_title"))
    st.info(t(cfg, "run_info"))

    uploaded = st.file_uploader(t(cfg, "upload"), type=["docx"], accept_multiple_files=True)
    if uploaded and st.button(t(cfg, "save_uploads")):
        save_uploaded_files(uploaded, Path(cfg["input_dir"]))
        st.success(t(cfg, "saved_files", count=len(uploaded)))

    init = st.checkbox(t(cfg, "init_before"), value=False, help=t(cfg, "init_before_help"))
    force = st.checkbox(t(cfg, "force"), value=False, help=t(cfg, "force_help"))

    modes = [
        (t(cfg, "initialize"), "init-only"),
        (t(cfg, "preflight"), "preflight"),
        (t(cfg, "segment"), "segment"),
        (t(cfg, "score"), "score"),
        (t(cfg, "select"), "select"),
        (t(cfg, "review_select_btn"), "review-select"),
        (t(cfg, "enrich_selected"), "enrich-selected"),
        (t(cfg, "assemble"), "assemble"),
        (t(cfg, "repair_answers_btn"), "repair-answers"),
        (t(cfg, "quality_report_btn"), "quality-report"),
        (t(cfg, "stage1"), "stage1"),
    ]
    cols = st.columns(3)
    for i, (label, mode) in enumerate(modes):
        with cols[i % 3]:
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
                    cmd = build_command(mode, cfg, init=init, force=force)
                run_command(cmd, cfg)


def table_with_detail(title: str, rows: list[dict], path_key: str | None = None) -> None:
    st.subheader(title)
    if not rows:
        st.warning(t(st.session_state.get("lang", "zh"), "no_data"))
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if path_key:
        ids = [row.get("item_id", f"row-{i}") for i, row in enumerate(rows)]
        selected_id = st.selectbox(t(st.session_state.get("lang", "zh"), "view_detail", title=title), ids, key=f"detail-{title}")
        row = rows[ids.index(selected_id)]
        path_value = row.get(path_key)
        if path_value:
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
    options = [
        (t(cfg, "segmentation"), "segment"),
        (t(cfg, "scoring"), "score"),
        (t(cfg, "selected"), "selected"),
        (t(cfg, "review_notes"), "notes"),
    ]
    view_label = st.radio(t(cfg, "review_target"), [label for label, _ in options], horizontal=True)
    view = dict(options)[view_label]
    if view == "segment":
        st.info(t(cfg, "segment_help"))
        rows = read_csv_rows(out_dir / "segment_index.csv")
        low_only = st.checkbox(t(cfg, "low_only"), value=False, help=t(cfg, "low_only_help"))
        if low_only:
            rows = [
                row
                for row in rows
                if (float(row.get("confidence") or 0) < 0.75) or str(row.get("answer_count") or "0") == "0"
            ]
        table_with_detail(t(cfg, "segment_table"), rows, "segment_path")
    elif view == "score":
        st.info(t(cfg, "score_help"))
        rows = read_csv_rows(out_dir / "score_index.csv")
        table_with_detail(t(cfg, "score_table"), rows)
    elif view == "selected":
        st.info(t(cfg, "selected_help"))
        rows = read_csv_rows(out_dir / "selected_items.csv")
        table_with_detail(t(cfg, "selected_table"), rows)
    else:
        path = out_dir / "review_select_notes.json"
        if path.exists():
            st.json(read_json_file(path))
        else:
            st.warning(t(cfg, "no_review_notes"))


def results_tab(cfg: dict) -> None:
    out_dir = Path(cfg["out_dir"])
    assembled = out_dir / "assembled"
    files = [
        assembled / "final_selected_questions_with_answers.md",
        assembled / "final_teacher_notes.md",
        assembled / "final_answers_only.md",
        out_dir / "segment_index.csv",
        out_dir / "score_index.csv",
        out_dir / "selected_items.csv",
    ]
    st.subheader(t(cfg, "output_files"))
    notes = {
        "final_selected_questions_with_answers.md": t(cfg, "final_set_note"),
        "final_teacher_notes.md": t(cfg, "teacher_note"),
        "final_answers_only.md": t(cfg, "answers_note"),
    }
    for path in files:
        st.write(f"`{rel(path)}` · {file_size(path)}")
        if path.name in notes:
            st.caption(notes[path.name])
        if path.exists():
            st.download_button(
                t(cfg, "download", name=path.name),
                data=path.read_bytes(),
                file_name=path.name,
                mime="text/markdown" if path.suffix == ".md" else "text/csv",
                key=f"download-{path}",
            )

    md_files = [p for p in files if p.suffix == ".md" and p.exists()]
    if md_files:
        selected = st.selectbox(t(cfg, "preview_md"), md_files, format_func=lambda p: p.name)
        st.markdown(read_text(selected, limit=60000))


def main() -> None:
    st.set_page_config(page_title="高三英语试卷整理", layout="wide")
    cfg = sidebar_config()
    st.title(t(cfg, "title"))

    tabs = st.tabs([t(cfg, "status"), t(cfg, "run"), t(cfg, "review"), t(cfg, "results")])
    with tabs[0]:
        show_status(cfg)
    with tabs[1]:
        run_tab(cfg)
    with tabs[2]:
        review_tab(cfg)
    with tabs[3]:
        results_tab(cfg)


if __name__ == "__main__":
    main()
