# Gaokao English Docx Pipeline

> 高三英语模拟题 docx 自动整理流水线 · Automated exam-paper processing for Chinese high-school English teachers.

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![DeepSeek](https://img.shields.io/badge/AI-DeepSeek%20V4-purple)](https://deepseek.com)
[![Streamlit](https://img.shields.io/badge/GUI-Streamlit-red)](https://streamlit.io)

---

## About

**Gaokao English Docx Pipeline** transforms a folder of raw `.docx` mock-exam papers into three polished, classroom-ready Markdown files:

- **Student practice set** — curated questions with answers collected at the end.
- **Teacher notes** — scores, selection rationale, vocabulary highlights, grammar points, and long-sentence breakdowns.
- **Answers only** — a compact answer key for quick reference.

The pipeline does NOT simply dump paper content. It **segments** each paper into standard Gaokao question types, checks structural quality, retries only abnormal papers with rough model segmentation, **scores** every item, **ranks and selects** the best candidates, **enriches** only the winners, and assembles three classroom-ready Word documents.

### Why this exists

Manual exam-paper curation is slow, error-prone, and doesn't scale. A teacher processing five papers by hand would spend hours copying text, matching answers, comparing difficulty, and writing vocabulary notes. This pipeline automates every mechanical step while keeping the teacher in the loop through reviewable CSV indices at each stage. The AI handles the heavy lifting — scoring, selecting, enriching — but every decision is auditable and every output is plain Markdown that can be further edited by hand.

### Key Features

| Feature | Description |
|---|---|
| **DOCX parsing** | Extracts text from `.docx` without external dependencies (pure stdlib `zipfile` + `xml.etree`). |
| **Local-first segmentation** | Splits papers locally; only structural WARN/FAIL papers are retried with rough model segmentation. |
| **Multi-format answer extraction** | Handles `-` `—` `~` `--` range separators, concatenated grammar answers, table-format keys, and inline-embedded answers. |
| **Lightweight AI scoring** | Each item is scored on novelty, difficulty, vocabulary value, grammar value, and exam relevance — output is < 200 tokens per item. |
| **Programmatic selection** | A local ranking formula picks top candidates per section; no AI tokens spent on selection. |
| **Optional pro review** | A stronger model (`deepseek-v4-pro`) re-evaluates the shortlist with detailed accept/reject reasoning. |
| **Targeted enrichment** | Only the 18 final selections receive vocabulary lists, grammar/word-formation notes, and long-sentence analysis. |
| **Three polished Word outputs** | Separate A4 templates for student practice, teacher notes, and compact answers, with CJK fonts, headers, and page numbers. |
| **Streamlit GUI** | Education-blue UI, one-click Basic mode, progress/ETA, fixed auto-scrolling logs, and safe input/output cleanup. |
| **Quality report** | Generates `run_quality_report.md` summarising coverage, scores, selections, and token usage — no AI calls. |
| **Answer repair mode** | `--mode repair-answers` rescans extracted text for answers using every known format; fixes segment JSONs without touching AI-generated scores. |
| **Resumable & retry-safe** | Each stage writes checkpoint files. `Ctrl+C` is safe. `429` rate-limit errors are retried with exponential backoff. |
| **API conversation archive** | Every prompt and response is saved as Markdown for debugging and cost tracking. |

### Use Cases

- **High-school English teachers** preparing curated practice sets from mock exams
- **Tutoring centres** batch-processing papers to create differentiated worksheets
- **Exam researchers** analysing topic distribution, difficulty trends, and question quality across papers
- **Content creators** building structured question banks from raw exam DOCX files
- **Self-study students** who want a distilled set of the most valuable questions

### Tech Stack

| Layer | Technology |
|---|---|
| **Runtime** | Python 3.10+ (stdlib-first; zero mandatory external dependencies) |
| **DOCX parsing** | `zipfile` + `xml.etree.ElementTree` (no `python-docx`) |
| **AI models** | DeepSeek V4 Flash (scoring, enrichment) · DeepSeek V4 Pro (review) |
| **API client** | HTTP mode (stdlib `urllib`) or OpenAI SDK (optional) |
| **GUI** | Streamlit |
| **Concurrency** | `concurrent.futures.ThreadPoolExecutor` |
| **Output formats** | JSON, JSONL, CSV (UTF-8 BOM), Markdown, styled DOCX |

### Design Philosophy

- **AI as assistant, not black box.** Every decision is traceable through CSV indices and JSON checkpoints. The teacher remains the final authority.
- **Token economy by default.** Segmentation is local. Scoring is lightweight. Enrichment is targeted. Only the review step uses a "thinking" model.
- **Resumable and auditable.** Each stage writes independent checkpoints. Interruptions are safe. Every intermediate table is human-readable.
- **Stdlib-first, optionally SDK.** The core pipeline runs with zero `pip install`. The OpenAI SDK is an optional upgrade for better retry handling.

---

## v0.2 at a Glance

| Stage | AI? | What it does |
|---|---|---|
| `preflight` | ❌ local | Count docx, estimate API calls, warn about stale outputs |
| `segment` | local-first | Local split plus rough-model fallback for structural WARN/FAIL |
| `score` | ✅ flash | Lightweight scoring (novelty, difficulty, vocab, grammar) |
| `select` | ❌ local | Rank and pick top 2 candidates per section |
| `review-select` | ✅ pro | Re-evaluate shortlist with detailed reasoning |
| `enrich-selected` | ✅ flash | Vocabulary, grammar, sentences for winners only |
| `repair-answers` | ❌ local | Full-text answer rescan, fix segment JSONs |
| `assemble` | ❌ local | Compose 3 Markdown files from segments + scores |
| `quality-report` | ❌ local | Generate `run_quality_report.md` |
| `export-docx` | ❌ local | Convert Markdown to three template-styled A4 Word files via Pandoc |
| `check_segment_quality` | ❌ local | Per-paper diagnostics with PASS/WARN/FAIL |
| GUI Acceptance Check | ❌ local | Tests + segment + quality report in one click |
| GUI Cost Summary | ❌ local | Token usage and API call counts from existing files |

**Current regression result** — 61 tests passed; all three Word outputs validate as styled A4 OOXML. The historical 25-paper/225-segment benchmark remains documented in `docs/test_results.md`.

---

## Quick Start

### 1. Prerequisites

```bash
# Python 3.10+ required. No mandatory pip packages for CLI mode.
python3 --version

# Optional: OpenAI SDK for better API retry handling
pip3 install openai

# Optional: GUI
pip3 install -r requirements-gui.txt
```

### 2. Place your papers

```text
input_docx/
├── 2026北京一模.docx
├── 2026杭州二模.docx
└── 2026南京三模.docx
```

### 3. Preflight check (no API calls)

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode preflight
```

### 4. Run the full pipeline

```bash
export DEEPSEEK_API_KEY="your-key"

python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode stage1 \
  --init \
  --client http \
  --review-select \
  --segment-workers 16 \
  --score-workers 16 \
  --enrich-workers 16 \
  --max-retries 12
```

If rate-limited (429), use the conservative version:

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode stage1 \
  --init \
  --client http \
  --review-select \
  --segment-workers 4 \
  --score-workers 4 \
  --enrich-workers 4 \
  --max-retries 12
```

### 5. Or run step by step (for review)

```bash
# Segment papers into 9 question-type units
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode segment --init --client http
# → review outputs/gaokao_english/segment_index.csv

# Score each segment
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode score --client http --score-workers 16
# → review outputs/gaokao_english/score_index.csv

# Select top candidates locally
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode select
# → review outputs/gaokao_english/selected_items.csv

# Optional: pro model review
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode review-select --client http

# Enrich only selected items with vocabulary/grammar notes
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode enrich-selected --client http --enrich-workers 16

# Assemble final Markdown
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode assemble
```

### 6. Repair answers if needed (no AI calls)

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode repair-answers
```

### 7. Generate quality report (no AI calls)

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode quality-report
# → outputs/gaokao_english/run_quality_report.md
```

---

## Pipeline Architecture

```text
preflight  →  local pre-check (docx count, expected API calls, stale-output warnings)
segment    →  local split; structural WARN/FAIL papers use rough model fallback
score      →  deepseek-v4-flash lightweight scoring (no vocab lists)
select     →  local ranking formula picks top-2 per section
review-select →  optional deepseek-v4-pro re-evaluation with detailed reasoning
enrich-selected →  targeted vocabulary/grammar/sentence notes for winners only
assemble   →  local Markdown composition (questions + answers + teacher notes)
```

### Model assignments (defaults)

```text
segment  →  local first; deepseek-v4-flash only for structural fallback
score    →  deepseek-v4-flash  (thinking: disabled)
review   →  deepseek-v4-pro    (thinking: enabled, reasoning_effort: medium)
enrich   →  deepseek-v4-flash  (thinking: disabled)
```

### Concurrency & rate limits

```text
segment-workers = 16
score-workers   = 16
enrich-workers  = 16   (reduce all three if the provider returns 429)
max-retries     = 12   (exponential backoff + Retry-After header honoured)
```

For persistent `429 Too Many Requests`:

```bash
--segment-workers 4 --score-workers 4 --enrich-workers 4 --max-retries 12
```

### Thinking mode notes

`omit` does NOT disable thinking — it simply omits the parameter, letting the server use its default (which may be `enabled`). To control cost, use `disabled` explicitly:

```text
segment_thinking = disabled
score_thinking   = disabled
review_thinking  = enabled
enrich_thinking  = disabled
```

### Segment input modes

```text
--segment-input local   (default: local parsing, zero API tokens)
--segment-input rough   (local rough-chunk + AI refinement)
--segment-input full    (AI splits the entire paper)
```

---

## Pipeline Outputs

```text
outputs/gaokao_english/
├── extracted_text/                                  plain text for each docx
├── segments/                                        segmented question JSONs
├── rough_segments/                                  local rough-chunk results
├── segment_index.csv                                segmentation audit table
├── scores/                                          per-item score JSONs
├── score_index.csv                                  scoring audit table
├── selected_items.csv                               final selected items
├── review_select_notes.json                         pro review decisions (if run)
├── enrichments/                                     vocabulary/grammar/sentence JSONs
├── assembled/
│   ├── final_selected_questions_with_answers.md     student practice set
│   ├── final_teacher_notes.md                       teacher reference
│   └── final_answers_only.md                        answer key
├── api_conversations/                               archived prompts & responses (Markdown)
└── run_quality_report.md                            auto-generated quality report
```

---

## Export to Word (docx)

Convert the assembled Markdown files to `.docx` for printing and distribution. Pandoc applies three committed reference DOCX templates; runtime Python code remains stdlib-only.

### CLI

```bash
# Export all three Markdown files
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode export-docx
```

Outputs:

```text
outputs/gaokao_english/docx_exports/
├── 高三英语精选试题_学生版.docx
├── 高三英语精选试题_教师讲解版.docx
└── 高三英语精选试题_答案汇总版.docx
```

Or use the standalone script:

```bash
python3 scripts/export_markdown_to_docx.py \
  --assembled-dir outputs/gaokao_english/assembled \
  --out-dir outputs/gaokao_english/docx_exports
```

### GUI

Run the complete workflow or click **10. 导出 Word** in Debug mode. This local step does not call the AI.

### Supported syntax

The templates enforce A4 pages, East Asian font mapping, heading hierarchy, list indentation, headers, page numbers, and per-section pagination.

---

## GUI (Streamlit)

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements-gui.txt
streamlit run gui_app.py
```

The GUI provides three deliberate levels and supports:

- **Basic mode** with one full-workflow button, progress bar, current stage, elapsed time, and ETA only
- **Advanced/Debug modes** with a fixed-height auto-scrolling log panel and prerequisite-aware step buttons
- **API key** input with local project storage (`.local/gui_secrets.json`)
- **Separate confirmed actions** for clearing input and output directories
- **In-app review** of `segment_index.csv`, `score_index.csv`, `selected_items.csv`, and `review_select_notes.json`
- **Output cards** with Chinese filenames, size/time/status, downloads, and an open-output-folder action

The GUI calls the exact same CLI script as a subprocess — behaviour is identical between GUI and terminal.

---

## Regression Tests

All tests are local — no AI/API/network needed:

```bash
python3 tests/run_tests.py
python3 -m py_compile scripts/gaokao_english_docx_pipeline.py gui_app.py \
  scripts/check_segment_quality.py scripts/segment_quality.py \
  scripts/export_markdown_to_docx.py
```

### Local segment acceptance check

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english_segment_check \
  --mode segment --init --segment-input local --no-segment-warning-fallback

python3 scripts/check_segment_quality.py \
  --out outputs/gaokao_english_segment_check
```

Or use the GUI **Acceptance Check** tab for a one-click equivalent.

---

## Detailed Workflow Guide

<details>
<summary>Click to expand — legacy prompts / analyze / final workflow</summary>

### Generate prompts (no API)

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode prompts
```

Outputs: `extracted_text/`, `items.jsonl`, `analysis_prompts.jsonl`, `analysis_index.csv`, `final_selection_prompt.md`

### AI analysis

```bash
export DEEPSEEK_API_KEY="your-key"
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode analyze
```

Set up a virtual environment first:

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install openai
```

Use `--client http` if you prefer the stdlib-only HTTP fallback (no `openai` package needed).

### Horizontal selection

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode final
```

### Resume & re-initialise

```bash
# Clear output directory only
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --init-only

# Clear and immediately run
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode prompts --init
```

`--init` / `--init-only` only clears the `--out` directory. Protected paths (`input_docx`, `scripts`, `config`, `.venv`, the project root) are refused.

### API display controls

```bash
--show-output [none|preview|full]    # default: preview
--show-reasoning [none|preview|full] # default: preview
--preview-chars 3000                 # default: 1200
--no-save-conversations              # skip API conversation archiving
```

### Non-DeepSeek endpoints

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode analyze \
  --base-url "https://your-endpoint/v1" \
  --model "your-model" \
  --api-key-env YOUR_API_KEY_ENV \
  --thinking omit \
  --reasoning-effort none
```
</details>

---

## GitHub About

> Copy the fields below into your repository's **About** section on GitHub.

**Description** (≤ 160 chars):

```
Automated pipeline for Gaokao English mock-exam DOCX files. Segments, scores, selects, and enriches questions with AI to produce classroom-ready Markdown practice sets and teacher notes.
```

**Topics** (copy-paste into the topics field):

```
gaokao english education docx python exam-preparation high-school teaching deepseek nlp text-extraction question-bank automated-curriculum teacher-tools markdown-generation chinese-english pipeline ai-in-education teaching-materials
```

**Website**: _leave empty (or link to your repo)_

---

## License

This project is open source under the MIT License. See [LICENSE](LICENSE) for details.
