# Architecture

## Pipeline stages

```
input_docx/*.docx
  │
  ├─ preflight        local  — count docx, estimate API calls
  ├─ segment          local-first — split locally; rough-model fallback on structural WARN/FAIL
  ├─ score            AI     — lightweight scoring per item
  ├─ select           local  — ranking formula, top-2 per section
  ├─ review-select    AI     — pro model re-evaluation (optional)
  ├─ enrich-selected  AI     — vocab/grammar/sentences for winners
  ├─ repair-answers   local  — full-text answer rescan
  ├─ assemble         local  — 3 Markdown outputs
  ├─ quality-report   local  — run_quality_report.md
  └─ export-docx      local  — Markdown → three styled A4 .docx files
```

## Key files

| File | Role |
|------|------|
| `scripts/gaokao_english_docx_pipeline.py` | CLI entry point, all modes, local segmenter |
| `scripts/docx_blocks.py` | Block model — reads docx keeping each paragraph's original OOXML node |
| `scripts/docx_splice.py` | Clone a source docx keeping chosen paragraphs; merge across papers; validate |
| `scripts/export_docx_splice.py` | Builds the three Word deliverables from the clones |
| `scripts/segment_quality.py` | Shared PASS/PASS*/WARN/FAIL evaluator used by fallback and reports |
| `scripts/check_segment_quality.py` | Batch segment quality report using shared rules |
| `gui_app.py` | Education-blue Streamlit GUI with Basic/Advanced/Debug modes |
| `tests/run_tests.py` | Zero-dependency runner for all regression suites |

## Data flow

- Segments: JSON per item in `segments/`, indexed by `segment_index.csv`. Each carries
  `source_path` + `source_blocks` — the half-open range of `w:body` children it occupies
  in the original paper, which is what makes cloning possible.
- Scores: JSON per item in `scores/`, indexed by `score_index.csv`
- Selected: `selected_items.csv` → enrichments → `docx_exports/*.docx`
- `assembled/*.md` is still written as a readable intermediate, but the Word export no
  longer goes through it.

## How the Word export preserves formatting

The student and teacher documents are **not typeset**. For each selected question the
exporter copies the source `.docx` zip whole — `styles.xml`, `theme/`, `word/media/*`
and `_rels` included — and replaces only the body with that question's original
`w:p`/`w:tbl` nodes. Every style id and image relationship is still the one it was
authored against, so fonts, indents, tables and pictures survive untouched.

Questions from different papers are then merged with `docxcompose`, which remaps the
ids. This matters: the three sample papers reuse `a3` for `footer`, `Title` *and*
`Subtitle`, and `rId6` for both an image and `endnotes.xml`. Moving paragraphs between
them without remapping silently renders a footer as a headline.

## Design constraints

- Input files are never changed by processing; explicit GUI “clear input” requires a separate confirmed action
- HTTP client is stdlib `urllib` with `openai` SDK as optional upgrade
- GUI calls CLI as subprocess — identical behaviour
- Every exported Word file must pass `docx_splice.validate()`: all XML parses, A4 portrait,
  no undefined style references, no unresolved relationship ids
