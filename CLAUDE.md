# CLAUDE.md

- **Project**: 高三英语模拟题自动整理工具 (gaokao-english-docx-pipeline)
- **Goal**: 从 docx 中本地切分提取题目，AI 辅助生成教师讲解，最终重新导出为合规的 docx。
- **Rules**:
  1. 优先保证正确性，严禁出现破坏文档结构的坏味道。
  2. 绝不修改或污染原始输入数据。
  3. 所有导出结果必须通过底层 XML 校验与自动化压测。
- **Architecture**: 详见 [`docs/architecture.md`](docs/architecture.md)
- **Current Status**: 详见 [`docs/current_status.md`](docs/current_status.md)
- **Key decisions**: 详见 [`docs/decisions.md`](docs/decisions.md)
- **Word compat**: 详见 [`docs/word_compatibility.md`](docs/word_compatibility.md)
- **Test baseline**: 详见 [`docs/test_results.md`](docs/test_results.md)

## Quick Commands

```bash
# 一键完整流程 (AI)
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode stage1 --init --client http --review-select --score-workers 4 --enrich-workers 2 --max-retries 8

# 本地验收（不花钱）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english_segment_check --mode segment --init --segment-input local
python3 scripts/check_segment_quality.py --out outputs/gaokao_english_segment_check

# Word 导出
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode export-docx

# 测试
python3 tests/test_answer_extraction.py tests/test_segment_tail_trim.py tests/test_export_markdown_to_docx.py

# GUI
streamlit run gui_app.py
```
