# Word Compatibility & OOXML Fix Ledger

- **问题表现**: 导出 docx 后，Microsoft Word 提示"发现无法读取的内容"，点击恢复后才能正常打开。
- **根本原因**: 
  1. AI 生成的 Markdown 文本中混入了非法的 XML 控制字符（如不可见的 `\x00-\x08`, `\x0b-\x0c`, `\x0e-\x1f`），直接导致 Word 的 OOXML 解析器崩溃。
  2. 代码调用了 Word 底层未注册的 XML 原生样式，造成样式命名空间冲突。
- **解决手段与开发规范**:
  1. **必须**在所有文本写入 docx 之前调用 `_xml_sanitize` 进行正则剥离。
  2. **强制**使用 `Normal` 最基础样式，放弃任何复杂的原生样式嵌套，全部通过 Run 级别的对象属性修改（如 `run.font.bold = True`, `run.font.size`）来实现加粗和字号控制。
  3. 写入前使用 `ElementTree` 对生成的 XML 片段进行内存自检。
