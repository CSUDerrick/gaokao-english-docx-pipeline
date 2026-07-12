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
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode stage1 --init --client http --review-select --segment-workers 16 --score-workers 16 --enrich-workers 16 --max-retries 12

# 本地验收（不花钱）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english_segment_check --mode segment --init --segment-input local --no-segment-warning-fallback
python3 scripts/check_segment_quality.py --out outputs/gaokao_english_segment_check

# Word 导出（克隆原卷排版，不经过 Markdown）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode export-docx

# 测试
python3 tests/run_tests.py

# macOS 原生应用（推荐给老师）
python3 app/main.py
./packaging/build_macos.sh          # 打包 .app + .dmg

# Streamlit 网页界面（备选）
streamlit run gui_app.py
```

## 关键约束

- **题目原文必须克隆，不许重新排版。** 原卷的 `w:p`/`w:tbl` 节点直接搬运；
  跨卷合并交给 `docxcompose` 重映射 ID（样卷有 17 个冲突 styleId、10 个冲突 rId）。
- **不要用 `xml.etree` 重新序列化 `document.xml` 根节点**——它会丢掉命名空间声明，
  导致 `mc:Ignorable` 引用未声明前缀，Word 报「发现无法读取的内容」。用 `lxml`。
- **引用任何样式前必须先在 `styles.xml` 里定义它**（同上，历史事故根因）。
- 导出必须通过 `docx_splice.validate()`。
