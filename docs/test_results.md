# Test Results & Benchmarks

- **自动化回归**: `python3 tests/run_tests.py` — 61 passed, 0 failed。
- **语法检查**: GUI、流水线、切分质量模块、导出器和模板生成器全部通过 `py_compile`。
- **当前本地样本切分**: 3 份试卷共 26 segments；关闭模型回退的诊断结果为 2 PASS、1 structural WARN。
- **回退门验证**: 默认模式正确识别缺少 `gap_filling` 的异常试卷，并在无 API key 时停止且给出明确修复提示。
- **Word 验收**: 三份 DOCX 均通过 OOXML、A4、页眉、页脚、页码和 East Asian 字体映射校验。
- **视觉验收**: LibreOffice 转 PDF 后抽查学生版、教师版和答案版首页/正文页，中文正常、无重叠或越界，标题与分页清晰。
- **历史基准**: 25 份试卷、225 segments、0 structural WARN、0 FAIL 的 v0.1 基准继续保留作为回归参考。
