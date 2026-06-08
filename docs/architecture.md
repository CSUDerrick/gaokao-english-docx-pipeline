# Architecture

## Pipeline stages

```
input_docx/*.docx
  │
  ├─ preflight        local  — count docx, estimate API calls
  ├─ segment          local  — split into 9 exam-section JSONs
  ├─ score            AI     — lightweight scoring per item
  ├─ select           local  — ranking formula, top-2 per section
  ├─ review-select    AI     — pro model re-evaluation (optional)
  ├─ enrich-selected  AI     — vocab/grammar/sentences for winners
  ├─ repair-answers   local  — full-text answer rescan
  ├─ assemble         local  — 3 Markdown outputs
  ├─ quality-report   local  — run_quality_report.md
  └─ export-docx      local  — Markdown → compliant .docx
```

## Key files

| File | Role |
|------|------|
| `scripts/gaokao_english_docx_pipeline.py` | CLI entry point, all modes, OOXML text extraction, local segmenter |
| `scripts/export_markdown_to_docx.py` | Pure-stdlib Markdown → docx (13-part OOXML skeleton) |
| `scripts/check_segment_quality.py` | Batch segment quality report (PASS/WARN/FAIL) |
| `gui_app.py` | Streamlit GUI (Basic/Advanced/Debug modes, 6 tabs) |
| `tests/` | 48 regression tests across 3 suites |

## Data flow

- Segments: JSON per item in `segments/`, indexed by `segment_index.csv`
- Scores: JSON per item in `scores/`, indexed by `score_index.csv`
- Selected: `selected_items.csv` → enrichments → `assembled/*.md` → `docx_exports/*.docx`

## Design constraints

- `input_docx/` is read-only — never mutated
- CLI core has zero mandatory pip dependencies
- HTTP client is stdlib `urllib` with `openai` SDK as optional upgrade
- GUI calls CLI as subprocess — identical behaviour
