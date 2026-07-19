# 故障手册（Error Playbook）

每次线上跑出一个运行时错误，就在这里记一条，避免再犯。格式固定四段：
**症状 → 根因 → 修复 → 护栏**（护栏是那条「以后再犯就会先在测试里红掉」的测试）。

这份是**失败导向**的，和 `decisions.md`（设计决策导向）互补。真出错时，先看
`<out>/failures/` 里那次的完整原始输出（`dump_failure()` 落的盘），再对照这里。

---

## E1 · 读后续写解析报错「返回的不是合法 JSON」（模型把 JSON 语法写成中文引号 / 漏逗号）

- **症状**：质量优先模式跑到读后续写题，整轮硬失败：
  ```
  RuntimeError: explain 返回的不是合法 JSON：..._continuation_writing__01
  （前 200 字：{审题":{"原文情境”:"Li Mei随父母移居巴黎..."人物与性格":...）
  ```
  报错只印前 200 字，看不到全貌；一道坏题让 assemble/export 全跑不到。
  （2026-07 安徽合肥168中学卷，质量档 deepseek-v4-pro。）

- **根因**：读后续写是**一个巨型嵌套 JSON**——审题 + 2–3 条思路，每条含完整英文范文 + 亮点 + 提纲 + 评分要点。
  引号密度和嵌套最高，模型最容易把「JSON 语法引号」和「字符串内部引号」写混、还漏逗号（上面那段就是
  ASCII `"` 和中文 `”` 混用）。而当时 `ask_for_json` **只给 1 次纠错机会**，之后 `require_parsed` 直接 `raise`。
  这**不是输入坏**（那份 docx 完全正常）——输入预检挡不住它。

- **修复**（三层，都在 `scripts/gaokao_english_docx_pipeline.py` + `prompts/`）：
  1. **拆小**：`explain_writing()` 把写作题从一个巨型 JSON 拆成「先审题+评分要点，再每轮一个思路」，
     每次只是一篇范文的小 JSON，同一 Conversation 逐轮命中前缀缓存。合并回 `{审题,思路[],评分要点}`——
     **和导出侧 `_render_writing_explanation` 期望的形状完全一致，导出一行没改**。
     提示词：`continuation_writing.md` / `practical_writing.md` 改成首轮只出审题；新增 `writing_more_idea.md`。
  2. **多轮重试**：`ask_for_json` 的纠错从 1 次改成 `JSON_REPAIR_MAX_TURNS`（=2）次，第二次起把模型
     自己那段坏输出的开头回贴给它看。截断仍然短路（重试只会再截断）。解析器**依然不做静默改写**。
  3. **留证**：最终失败时 `dump_failure()` 把**完整**原始输出 + prompt + provider/model/preset 写到
     `<out>/failures/<时间戳>_<item>.md`，报错不再只有 200 字。

- **护栏**（`tests/test_explain_json.py`）：
  - `test_writing_is_asked_in_small_pieces_and_merges_to_the_expected_shape`——写作走小 JSON、合并形状可被导出渲染。
  - `test_recovers_within_a_couple_of_corrective_turns`——先坏两次再好，能在 N 轮内恢复。
  - `test_a_broken_writing_idea_leaves_the_whole_reply_on_disk`——硬失败时 `failures/` 落全文。
  - **若又开始报截断**：那是「答案太长」不是「引号写错」，别加重试——按 `require_parsed` 的提示拧
    `--explain-max-tokens` 或降强度档。

---

## E3 · OCR 调用失败（PaddleOCR 换成异步 job API，老的同步调用全线报废）

- **症状**：处理 PDF 时报 `PaddleOCR API 调用失败 (HTTP …)`（`pdf_ingest.py` 的老 `call_api`），OCR 完全用不了。

- **根因**：PaddleOCR 把接口换成了**异步 job API**，老代码的**每一层**都对不上：
  地址 `{base}/layout-parsing` → `/api/v2/ocr/jobs`；鉴权 `token <t>` → **`bearer <t>`**；
  上传 base64 塞 JSON → **multipart**；流程「一次 POST 拿结果」→ **提交 → 轮询 → 下载 JSONL**；
  图片 base64 → **URL**。

- **修复**：`pdf_ingest.py` 传输层照 `mineru_ingest.py` 的异步骨架重写（`_request` 单一缝 / `_submit` /
  `_wait` / `_download_jsonl` / `parse_jsonl`）。**真实探测确认 v2 仍然返回 `prunedResult.parsing_res_list`**，
  所以解析层和 `blocks_to_docx` 以下的共享契约一行没动。顺带：
  - **取消接上了**：Paddle 现在也是轮询，`convert_pdfs` 必须传 `sleep=_sleep_or_cancel`（过去它是同步的，不需要）。
  - **服务地址不用再找了**：v2 是**一个全局地址**，`PADDLEOCR_BASE_URL` 降级成可选覆盖；老的
    `.../layout-parsing` 地址只取 host，自动兼容。App 预检从「要令牌+地址」改成「只要令牌」。
  - 没有引入 `requests`：multipart 手写（全库依然零三方 HTTP 库）。

- **护栏**（`tests/test_pdf_ingest.py`，全部 monkeypatch `_request`，不联网）：
  `test_the_poll_loop_can_be_cancelled`、`test_a_failed_job_says_why`、
  `test_the_service_address_is_optional_and_an_old_one_still_works`、`test_the_upload_is_multipart_...`。

### E3a · 顺带被真实调用揪出来的两个老 bug（都只有真跑一次才看得见）

1. **每份 OCR 出来的卷子顶上都有一张幽灵表格**（`Table | Table / 1 | 2`）。
   *根因*：`blocks_to_docx` 只删 `doc.paragraphs`，模板自带的**演示表格**留下了——
   这正是 `docx_splice.blank_template()` 文档字符串里写过、且**已经为答案版修过**的同一个 bug，
   只是没修到 OCR 这条路上。*修复*：`blocks_to_docx` 改用 `ds.blank_template(TEMPLATE)`（顺带钉死 A4）。
2. **表格永远跳到该页最上面，排在标题前面**。
   *根因*：真实响应里**表格的 `block_order` 是 `None`**，而老代码 `int(order) if order is not None else 0`
   把它变成 **0** → 排序到最前。*修复*：`parsing_res_list` 本身就是阅读顺序，没有 `block_order` 的块
   就**待在原地**（跟在前一块后面），rank ×2 让「紧跟其后」仍是整数。
   *护栏*：`test_a_table_without_a_block_order_stays_where_it_was_found`。

---

## E2 · 输入本身是坏的（OCR 乱码 / 近乎空白 / 抽取错乱）

- **症状**：不是这次那个报错，而是更隐蔽——喂进去的 docx / PDF-OCR 文本已经是乱码或残缺，
  后面切分、打分、解析全建在坏地基上，产出莫名其妙却不报错。

- **根因**：以前没有任何「抽取出来的正文到底是不是一份正常英语卷」的体检。

- **修复**：新增 `scripts/input_precheck.py`（确定性本地规则：U+FFFD 替换字符、英文字母占比、
  控制字符、近乎空、超长无空格串）。`run_preflight` 每份卷先本地体检，**只有可疑的**才升级到 flash 复核
  （`--precheck-escalate`，默认开；仅警告不中止）。`run_segment` 入口也跑一次**免费**本地体检（warn-only）。

- **护栏**：`tests/test_input_precheck.py`——正常双语卷不误报，乱码/空白/纯中文/超长串都能报出来。
  设计上是**烟雾报警器**：宁可漏报也别对正常卷误报（否则每次跑都在喊狼来了）。

- **变种：原卷缺整道题（阅读段有文章没题目）**。症状：导出的「阅读B」只有文章、没有 24-27 题。
  **根因不在切分**——`华南师范` 那份原始 docx 里 24-27 题**压根不存在**（被阅读C题目 28-31 的重复
  占位挤掉了，原卷作者的复制粘贴事故）；`grep '24\.' document.xml` = 0 次。代码没法凭空补题。
  **修复见决策 35**：`segment_quality.missing_question_numbers()` 拿 `answer_key` 的题号去正文里找
  `^N.` 题干，缺了就判定缺题；`drop_incomplete_reading` 在选题阶段踢掉它换完整的一份，
  `export_selected` 兜底**跳过并警告**。**排查一份新卷子**时先跑
  `python3 scripts/check_segment_quality.py --out <out>`，「阅读题目缺失」就是它。
