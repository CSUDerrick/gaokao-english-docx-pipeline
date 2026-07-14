# Word Compatibility & OOXML Fix Ledger

Word 报「发现无法读取的内容」时，**先看这一节的清单**——每一条都真实发生过一次，
并且都有对应的自动化闸门（`docx_splice.validate()`）拦住复发。

## 已知的四个根因

1. **非法 XML 控制字符**（`\x00-\x08`、`\x0b-\x0c`、`\x0e-\x1f`）混进模型生成的讲解文本。
   - 闸门：`docx_splice.sanitize()`，写入任何文本前必调。
2. **引用了 `styles.xml` 里没有定义的样式**。
   - 闸门：`ensure_note_styles()` 必须在任何 `add_paragraph(style=...)` **之前**调用；
     `validate()` 断言 `{pStyle, rStyle, tblStyle}` ⊆ styles.xml 里的 styleId。
   - 推论：表格边框用 `OxmlElement` 直接写 `w:tblBorders`（见 `docx_splice.set_table_borders()`），
     **不要** `table.style = "Table Grid"`——那个样式不保证存在于模板里。
3. **用 `xml.etree` 重新序列化 `document.xml` 根节点**——它会丢掉命名空间声明，
   导致 `mc:Ignorable` 引用未声明的前缀。用 `lxml`。
   - 闸门：`test_clone_preserves_root_namespaces_and_mc_ignorable`。
4. **悬空的成对标记**（2026-07 定位）。`w:bookmarkStart` / `w:bookmarkEnd` 这类标记可以横跨段落；
   按段落下标裁剪正文时，很容易留下一半、丢掉另一半。
   - 实测：导出的教师讲解版有 8 个 `bookmarkStart` 但只有 6 个 `bookmarkEnd`，
     多出来的是原卷里的 `OLE_LINK7` / `OLE_LINK8`——它们的 `bookmarkEnd` 落在没被克隆的段落里。
     答案汇总版没有书签，所以只有它不报错，这和用户的观察完全一致。
   - 闸门：`clone_subset()` 调用 `drop_orphan_range_markers()` 清理；
     `validate()` 新增 `orphan_range_markers()` 断言。覆盖 `bookmark` / `commentRange` /
     `moveFromRange` / `moveToRange` / `perm` 五组标记。

5. **把带类型的字段清空，而不是删掉**（2026-07 定位，**这才是学生版/教师版报错的真凶**）。
   `scrub_metadata()` 为了抹掉原作者，把 `cp:lastPrinted` 的文本设成了 `""`，
   产出 `<cp:lastPrinted></cp:lastPrinted>`。但它的类型是 **dateTime**——
   空字符串不是「没有日期」，是**一个格式错误的日期**。Word 直接报错。
   - 佐证：原卷的 `lastPrinted` 是合法的 `2026-05-30T03:25:00Z`；Word 修复后的版本把这个元素**整个删掉**了；
     而「答案汇总版」是用模板新建的、根本没有 `lastPrinted` 可清空——所以只有它从不报错。
     这和用户的观察完全吻合。
   - 闸门：`scrub_metadata()` 改为**删除元素**而不是置空；
     `validate()` 新增 `empty_typed_core_properties()` 断言。

6. **属性/元素顺序违反 schema 的 sequence**（同批定位）。
   OOXML 的 `CT_TblPrBase`、`CT_PPrBase` 都是**有序序列**，不是任意集合。
   - `set_table_borders()` 原来直接 `append(w:tblBorders)`，而 python-docx 的 `add_table()`
     已经写了 `tblW` + `tblLook`——于是 `tblBorders` 落到了 `tblLook` **之后**，顺序非法。
   - Pandoc 生成的模板里，`w:keepNext` / `w:pageBreakBefore` 也排在了它们该在的位置之后，
     且 `w:pgMar` 缺少 schema 要求的 `w:gutter`。
   - 闸门：`set_table_borders()` 按 `TBL_PR_ORDER` 插到正确位置；
     `blank_template()` 调 `normalize_template_ooxml()` 修好模板自带的顺序问题和缺失的 gutter。

## 定位方法（下次再出现时照做）

**不要靠猜。用 Word 自己的修复版本当标准答案，再用真正的 XSD 做裁判。**

1. 让用户把报错的文件在 Word 里打开→修复→另存。这份就是「Word 认可的形态」。
2. 包级 diff：`[Content_Types].xml` / `.rels` / `r:id` / 命名空间 / zip 完整性。
   （第 4、5 条的时候这些**全是干净的**——所以包级 diff 干净不代表文件没问题。）
3. **Schema 校验**（决定性的一步）。拉 ECMA-376 的 XSD：
   ```bash
   # python-docx 仓库里有一套自洽的 schema
   curl -sfL https://raw.githubusercontent.com/python-openxml/python-docx/master/ref/xsd/wml.xsd -o wml.xsd
   # ...以及同目录下的 dml-*.xsd / shared-*.xsd / vml-*.xsd，再补一个 w3.org 的 xml.xsd
   ```
   然后用 `lxml.etree.XMLSchema` 校验**每一个 part**（不只是 document.xml！第 5 条就在
   `docProps/core.xml` 里）。
4. **关键技巧**：原卷本身也会有几百个 schema「错误」（Word 容忍扩展命名空间）。
   所以不要看错误总数——要**对比错误的种类**：
   把「我们的输出」的错误类型集合，减去「Word 能正常打开的文件（原卷 + Word 修复版）」的错误类型集合。
   **差集里剩下的，才是真凶。** 第 5、6 条就是这样一眼看出来的。

## 排版策略（v0.3，决策 5 之后）

- **题目原文一律克隆原卷 OOXML 节点，不重新排版**；Pandoc 已移除。
- 只有「答案汇总版」和「重难点词汇表」是新建文档——它们没有原卷排版可继承。
  这两份走 `assets/word_templates/` 的 reference DOCX。
- `blank_template()` 会清空模板自带的示例内容——**包括示例表格**。
  只删 `doc.paragraphs` 会漏掉 `w:tbl`，历史上导致每份答案汇总版都夹带一张
  "Table 1 2" 的示例表格。
- 页面尺寸不信任模板：`blank_template()` 显式强制 A4（python-docx 默认是美国 Letter）。
- 每份成品必须通过 `docx_splice.validate()`：XML 可解析 / A4 纵向 / 样式已定义 /
  rId 不悬空 / 成对标记配平。任何一条不过就直接失败，绝不保留「看似成功」的损坏文件。
- **学生版另有一道内容闸门** `assert_student_edition_is_clean()`：
  正文一旦出现「来源」「【N题详解】」「参考范文」「答案键」或 Markdown 的 `**`，直接报错。
