# Changelog

## v0.3.0 — 2026-07-12

### Fixed — 排版保真（本版重点）

- **题目原文改为克隆原卷 OOXML，不再重新排版。** 此前 `extract_docx_text` 把 docx 读成
  一根扁平字符串，原卷 100% 的段落带 `pPr`、100% 的 run 带 `rPr`，全部被丢弃，再由
  Pandoc 从零重新发明排版——这就是「导出的 Word 还要手动调格式」的根因。
- **插图不再丢失。** 此前所有导出的 docx 中 media 数量恒为 0（湖北卷 8 张配图全丢）。
- **段落不再坍缩。** 单换行被 Pandoc 当作软换行，整篇阅读＋题干＋选项被压成一个
  10008 字符的段落。
- **`____36____` 不再被误当加粗。** Pandoc 把它解析成 `__strong__`，随机加粗正文。
- **下划线不再丢失。** 下划线是英语卷的填空线，此前 120 条全部消失。

实测（学生版，旧 → 新）：插图 0→9；下划线 0→120；表格 0→1；最长段落 10008→1051 字符。
加粗/下划线/斜体/插图/表格数量与原卷对应段落**逐一相等**。

### Added

- `scripts/docx_blocks.py` 块模型：保留每个段落的原始 OOXML 节点；扁平文本与旧实现逐字节一致。
- `scripts/docx_splice.py` 单源克隆 + `docxcompose` 跨卷合并 + 导出校验。
- `scripts/pdf_ingest.py` PDF 输入（PaddleOCR-VL 1.6）：OCR → 生成 docx → 走同一条管道。
- `app/main.py` macOS 原生应用（PySide6）；API Key 存钥匙串；应用内检查更新。
- `packaging/build_macos.sh` 打包 .app/.dmg，预留签名与公证开关。
- `.github/workflows/release.yml` 打 tag 自动发版。
- `requirements.txt`（此前 python-docx / openai / pandoc 全部未声明）。

### Removed

- **Pandoc**（未声明的系统二进制依赖，也是打包 macOS 应用的最大障碍）。
- 根目录过期的 pipeline 副本（2487 行，缺 3 个模式，只被一个测试引用着）。

### Security

- 清除原卷 `docProps` 里的作者信息（会把出卷老师的真名带进成品）。
- 补交此前未跟踪但必需的 `scripts/segment_quality.py` 和 `assets/`——`origin/main`
  此前 clone 下来会直接 ImportError。

## v0.2.0 — 2026-07-12

### Added

- Structural segmentation quality gate with rough-model fallback for affected papers only.
- Shared `segment_quality.py` evaluator and persisted `segment_fallback_report.json` audit trail.
- Professional education-blue GUI with Basic progress/ETA and fixed auto-scrolling logs for Advanced/Debug modes.
- Independent, confirmed “clear input” and “clear output” actions with project-path protection.
- Three committed A4 Pandoc reference DOCX templates for student, teacher, and answer outputs.
- Zero-dependency `tests/run_tests.py` that actually executes every plain `test_*` function.

### Changed

- Segment, score, and enrichment worker defaults are all 16 in GUI and CLI.
- Full GUI workflow now runs explicit ordered stages instead of hiding them inside one `stage1` subprocess.
- Exported Word files use clear Chinese names and validate A4, headers, footers, and East Asian font mappings.
- Assembled teacher Markdown uses semantic headings for vocabulary, grammar, long sentences, and teaching notes.

### Verified

- 61 regression tests passed.
- Three Word outputs rendered as A4 PDFs with readable Chinese, stable spacing, section hierarchy, and page numbers.

## v0.1.0 — 2026-06-04

First stable release of the Gaokao English Docx Pipeline.

### Added

- **Local segmentation** (`--mode segment --segment-input local`): split `.docx` exam papers into 9 standard Gaokao question-type units without calling the AI.
- **Lightweight AI scoring** (`--mode score`): each segment receives a compact score (novelty, difficulty, vocabulary value, grammar value, exam value) using `deepseek-v4-flash`. No verbose vocabulary lists at this stage.
- **Local selection** (`--mode select`): a programmatic ranking formula picks the top 2 candidates per section without AI tokens.
- **Pro review** (`--mode review-select`): optional `deepseek-v4-pro` re-evaluation of the local shortlist with detailed accept/reject reasoning.
- **Targeted enrichment** (`--mode enrich-selected`): only the 18 final selections receive vocabulary, grammar/word-formation, long-sentence analysis, and teaching notes using `deepseek-v4-flash`.
- **Local assembly** (`--mode assemble`): combine segments, scores, and enrichments into three polished Markdown files (student practice set, teacher notes, answers only).
- **Answer repair** (`--mode repair-answers`): rescan extracted text for answers using every known format (`-` `—` `~` `--` separators, concatenated grammar, table-format keys).  No AI calls.
- **Quality report** (`--mode quality-report`): generate `run_quality_report.md` summarising coverage, scores, selections, and token usage.  No AI calls.
- **Segment quality check** (`scripts/check_segment_quality.py`): per-paper diagnostics with PASS/WARN/FAIL grading.  No AI calls.
- **Markdown to docx export** (`--mode export-docx` and `scripts/export_markdown_to_docx.py`): pure-stdlib export of all three assembled Markdown files to Word documents with CJK-friendly fonts.
- **Streamlit GUI** (`gui_app.py`): teacher-facing workflow, parameter tuning, API key management, in-app CSV review, and one-click pipelines for the full workflow, acceptance check, and cost summary.
- **Teacher-first GUI modes** (`gui_app.py`): Basic mode now defaults to pasting an API key and clicking one full-workflow button; advanced controls and single-step debug commands are separated into Advanced and Debug modes.
- **Acceptance Check tab** in GUI: runs regression tests, syntax checks, local segmentation, and quality reporting in one click — no AI calls.
- **Cost Summary tab** in GUI: reads existing output files to display token usage, API call counts, model settings, and per-stage breakdowns.
- **Regression tests**: 17 answer-extraction tests, 10 tail-trimming tests, 8 export tests.

### Changed

- Default segment mode is now `local` (was `rough` in early development).  Zero API tokens for segmentation.
- `score` and `enrich` stages default to `thinking: disabled` to avoid unintended reasoning-token consumption.
- GUI full workflow now chains `stage1`, `repair-answers`, `quality-report`, and `export-docx`; the local follow-up stages run without API key environment variables.
- CLI default concurrency is conservative (`--score-workers 4`, `--enrich-workers 2`) to reduce 429 rate-limit errors.
- GUI Basic mode uses safer daily defaults (`score workers = 2`, `enrich workers = 1`, `max retries = 12`).
- Max retries default to 8 with exponential backoff respecting `Retry-After` headers.

### Fixed

- Answer extraction now handles 5 distinct range-separator formats (`-`, `—`, `~`, `--`) with optional periods and zero-spacing variants.
- Grammar-answer concatenation (`56. would spark57. playfully`) is now parsed correctly.
- Continuation-writing tail-trimming improved: answer section, listening transcript, model essay, and grading rubric boundaries detected significantly more reliably, reducing structural WARNs from 8 to 0 across 25 papers.
- `IncompleteRead` and `RemoteDisconnected` network errors are now retried.
- `_find_answer_section_start` now uses a three-stage detection strategy covering inline answer headers, standalone headers, and tail-embedded answer ranges.

### Verified

- 25 docx files: 225 segments (25 × 9), 0 structural WARN, 0 FAIL.
- 5 papers originally tested: all 9 sections detected, all answers extracted (35/35 choice + 10/10 grammar).
- All regression tests pass without network or AI access.

### Known Limitations

- Some papers use answer-table formats or embed answers inside explanations; these may not be fully parsed by the current answer extraction.
- Writing-section tail bleed (model essays, transcripts) still occurs for papers where the answer section header is not on a recognised pattern; flagged as PASS (tail-bleed) in quality reports.
- The legacy `--mode prompts` / `--mode analyze` / `--mode final` workflow is still present but superseded by the stage1 pipeline.
- GUI requires `streamlit` and `requirements-gui.txt`; the CLI core has no mandatory external dependencies.

### Next (v0.2)

- Expand answer-header pattern recognition for additional paper formats.
- Add structured-table answer parsing (e.g. "题号 | 正确答案 | 解析").
- Improve `--segment-input rough` and `--segment-input full` modes for papers where local segmentation fails.
- Add CI for regression tests.
- Add optional `python-docx` integration for richer Word export.
