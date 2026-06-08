# Changelog

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
