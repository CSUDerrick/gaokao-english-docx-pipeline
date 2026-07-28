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
  ├─ explain          AI     — per-question explanations for winners (teacher edition)
  ├─ vocab            AI     — the student vocabulary handout (two ways, see below)
  ├─ repair-answers   local  — full-text answer rescan
  ├─ assemble         local  — 3 Markdown outputs
  ├─ quality-report   local  — run_quality_report.md
  └─ export-docx      local  — clone the source papers → four A4 .docx files
```

`enrich-selected` (vocab/grammar/long sentences for the winners) was what the
teacher edition used to carry. It is no longer in the chain — nothing renders that
data any more — but the mode still exists and can be run by hand. See 决策 13/14.

### vocab — two paths, and the teacher picks (决策 33)

Neither is the correct one; they answer different questions, so 基础模式 puts the choice
in front of her rather than this file making it for her.

```
                      完整（--vocab-mode chunked）        困难（--vocab-mode whole，默认）
输入                  每道选中题的 segment_body            整卷 extracted_text/<卷>.txt
                      （天然不含答案键）                   （必须先过 trim_answer_tail_from_text）
调用数                18（每题一次）                       3（每卷一次；卷太大才切块+汇总轮）
上限                  ≤20 词 / ≤15 变形（每题）            ≤40 词 / ≤25 变形（每卷）
缓存                  vocab/chunked/<题号>.json            vocab/whole/<卷名>.json
换一批题              词表过期，必须重跑                    不过期（词属于卷，卷没变）
词表覆盖              只有学生手上那几道题                  整份卷子（含他没做到的题）
```

两条路都是「卷间并行、卷内一个 `Conversation`」（决策 8 的前缀缓存，实测命中 99%）。
产出的每一行都盖上 `vocab_mode`，**导出闸门认这个字段、不认命令行**——老师可能拨了开关
但没重跑。老词表没有这个字段，就按形状认：有 `item_id` 的是分块。

## Key files

| File | Role |
|------|------|
| `scripts/gaokao_english_docx_pipeline.py` | CLI entry point, all modes, local segmenter |
| `scripts/docx_blocks.py` | Block model — reads docx keeping each paragraph's original OOXML node |
| `scripts/docx_splice.py` | Clone a source docx keeping chosen paragraphs; merge across papers; validate |
| `scripts/answer_explanation.py` | Finds the paper's own 【N题详解】 blocks, per question, for the teacher edition |
| `scripts/model_presets.py` | The two quality presets (speed/quality); shared by the CLI and the GUI |
| `prompts/*.md` | The per-question-type explanation prompts — editable, shipped as bundle data |
| `scripts/export_docx_splice.py` | Builds the Word deliverables from the clones |
| `scripts/export_vocab_docx.py` | Builds the student word-list handout (the two tables) |
| `scripts/merge_vocab_docx.py` | Merges word-list handouts already on disk into one deduped handout — local only, no API (决策 37) |
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
