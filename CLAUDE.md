# CLAUDE.md

- **Project**: AI英语试卷整理工具 (gaokao-english-docx-pipeline)
- **Goal**: 从 docx 中本地切分提取题目，AI 辅助生成教师讲解，最终重新导出为合规的 docx。
- **Rules**:
  1. 优先保证正确性，严禁出现破坏文档结构的坏味道。
  2. 绝不修改或污染原始输入数据。
  3. 所有导出结果必须通过底层 XML 校验与自动化压测。
  4. **每次加功能或修 bug 后，都要重新打一个 mac 包**（`./packaging/build_macos.sh`）。
  5. **打包前必须先改版本号**（`app/main.py` 的 `VERSION`，全项目唯一一处；
     打包脚本从这里读，用来命名 dmg 和写 Info.plist）。**大功能进大版本号**（`0.8.0 → 0.9.0`），
     **小修补进小版本号**（`0.9.0 → 0.9.1`）。老师靠版本号归档，两次构建同号就分不清了。
     **打包脚本会拦**：同号直接中止（记录在 `packaging/.last_built_version`，构建成功才写）。
     确实要重打同一版（构建失败重试、调打包脚本）用 `ALLOW_SAME_VERSION=1 ./packaging/build_macos.sh`。
  6. **测试跑 flash（`--preset speed`），不要默认烧 pro。** 出成品才切 `--preset quality`。
- **Architecture**: 详见 [`docs/architecture.md`](docs/architecture.md)
- **Current Status**: 详见 [`docs/current_status.md`](docs/current_status.md)
- **Key decisions**: 详见 [`docs/decisions.md`](docs/decisions.md)
- **Error playbook（故障手册，每次线上报错都记一条）**: 详见 [`docs/error_playbook.md`](docs/error_playbook.md)
- **Word compat**: 详见 [`docs/word_compatibility.md`](docs/word_compatibility.md)
- **Test baseline**: 详见 [`docs/test_results.md`](docs/test_results.md)

## Quick Commands

```bash
# 一键完整流程 (AI)。--preset speed = 快模型（日常/测试）；--preset quality = 强模型（出成品）
# 两个 preset 都跑「该模型最深的一档」。--provider 决定连哪家 API（见 scripts/providers.py）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode stage1 --init --client http --provider deepseek --preset speed --review-select --segment-workers 16 --score-workers 16 --enrich-workers 16 --max-retries 12

# 换一家 API（模型名/接口地址/密钥变量都从 provider 表里取，只填 --provider 就够）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/x --mode vocab --provider anthropic   # 或 openai / zhipu / qwen / custom

# 本地验收（不花钱）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english_segment_check --mode segment --init --segment-input local --no-segment-warning-fallback
python3 scripts/check_segment_quality.py --out outputs/gaokao_english_segment_check

# 分步重跑：只重做不满意的那一块（GUI 里是「分步重跑」那一排按钮）
# 改完 prompts/*.md 后最常用的一条：
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode explain --force --client http --preset speed
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode export-docx
# 换一批题（本地选题是确定性的，重跑没用，必须走 AI 复核并告诉它「这批不满意」）：
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode review-select --reselect --client http --preset speed

# 重难点词汇表（给学生的，两张表）。两条路，老师在基础模式里二选一（决策 33）：
#   困难（整卷，默认）——通读整卷（自动去掉答案区），一份卷子一次调用
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode vocab --vocab-mode whole
#   完整（分块）——逐题提词，词表逐题对应学生手上的卷子
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode vocab --vocab-mode chunked

# Word 导出（克隆原卷排版，不经过 Markdown）
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode export-docx

# 导出体检（跑完一轮后必做：答案泄漏 / 书签悬空 / 切分越界 / 编号）
python3 scripts/check_export_quality.py --out outputs/gaokao_english

# 本次花费与缓存命中
python3 scripts/usage_report.py outputs/gaokao_english

# 测试
python3 tests/run_tests.py
python3 app/main.py --selftest

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
  表格边框用 `docx_splice.set_table_borders()` 直写 `w:tblBorders`，别用 `"Table Grid"` 样式。
- **按段落下标裁剪正文会切断跨段落的成对标记**（`bookmarkStart`/`bookmarkEnd` 等）。
  留下一半就会让 Word 报「发现无法读取的内容」——`clone_subset()` 必须清理，`validate()` 会拦。
- **答案绝不能进学生版。** 题干里出现答案键/`【N题详解】`/参考范文一律按 structural 处理，
  宁可停下也不能发出去；`assert_student_edition_is_clean()` 是最后一道闸。
- **教师讲解版 = 原题 + 官方答案与解析 + 详细解析和解答步骤**，三段，每段一个 docx part。
  标题里**不要出现「AI」**——老师拿这份去讲课。
  官方解析靠 `answer_explanation.py` **按块归属**（每个段落属于哪道题）定位，
  别改成按字符区间切——试过，相邻题组会共用一个 `w:p` 而串块，范文也会被算进解析里。
  原卷没给解析是常态（广东卷 9 个题型里有 4 个没有），此时标注「原卷未提供逐题解析」，
  但答案照印；越界（区间内混进别的题号）一律返回空，宁可不印也不能印错题的解析。
- **逐题解析的 prompt 是 `prompts/*.md`，不是 Python 字面量。** 老师要能直接改。
  打包时必须 `--add-data "prompts:prompts"`，路径走 `bundle_paths.prompt_dir()`。
- **完形/语法填空按每 5 题一轮问**（`EXPLAIN_CHUNK_SIZE`）。一次问 15 题，JSON 会被
  `max_tokens` 截断（见下条）。同一篇试卷共用一个 `Conversation`，逐轮吃前缀缓存。
- **`--init` 是 `shutil.rmtree` 整个输出目录**，只有整轮运行能用。分步重跑绝不能带它。
  `--force` 只加在用户点的那一个阶段上；下游不加，才能靠逐题缓存「只为新题花钱」。
- **改了选题就必须补跑 explain。** `select` / `review-select` 会覆写 `selected_items.json`，
  新题没有解析——`assert_selection_is_complete()` 会在导出前拦住。
  **vocab 要不要补跑，取决于老师选的模式**（决策 33）：分块词表换题就过期（必须重跑），
  整卷词表不会（词属于卷，卷没变）。

- **词汇表有两条路，别再单方面砍掉一条。** 上一版把「逐题分块」直接换成「整卷」，那是越权——
  哪条更好取决于老师怎么用这张纸。两条都留着，基础模式二选一：
  - **完整（`--vocab-mode chunked`）**：喂 `segment_body`，**天然不含答案键**，不用做任何裁剪。
  - **困难（`--vocab-mode whole`，默认）**：喂 `extracted_text/*.txt`，那是**整个 docx**，
    含答案键和**参考范文**。词汇表是**学生版**——必须过 `trim_answer_tail_from_text()`（决策 6 的边界）。
    忘了这一步不会报错，只会让学生词表里混进他那份卷子里根本没有的范文的词。

- **导出闸门问的是词表自带的 `vocab_mode`，不是当时的命令行。** 老师可以拨了开关就去导出。
  拿 `args.vocab_mode` 去判，会用分块的规矩卡掉一份完全合格的整卷词表。
  盘上的老词表没有这个字段——按**形状**认（有 `item_id` 的是分块），统一给个默认值会认错一半。

- **答案可以在另一份文档里（决策 34）。** 「学生版试卷 + 答案文档」两份进来时：
  - **认答案文档看「答案键在哪」，不看「有没有题」**——真答案文档的**参考范文会被切成 2 道写作题**，
    `_find_answer_section_start` 还会把答案区定位到**听力录音稿**。两个信号都会骗你。
    没有哪张卷子拿答案键开头（实测 0.3% vs 63%），这才是那个信号（`segment_quality.first_answer_run`）。
  - **答案文档整篇就是答案区，要整份取。** 对它再跑尾部检测会切在「听力录音稿」，
    而**参考范文在它上面**——范文全丢，写作题答案直接变空。
  - **解析块走 `official_explanation_path`，题目块永远只从原卷克隆。** 留空 = 同一个文件。
  - **不确定就不配。** 文件名只提名，flash 复核拍板；说不准就按「原卷未提供答案」走。

- **PaddleOCR 是异步 job API（提交→轮询→下载 JSONL），`bearer` 鉴权，一个全局地址。**
  它现在也在轮询，所以 `convert_pdfs` **必须给它传 `sleep=_sleep_or_cancel`**，否则取消无效。
  服务地址是可选覆盖，别再把它变成必填。图片是 URL 不是 base64。
  **没有 `block_order` 的块（表格）不能当成 0**——那会让每张表跳到该页最上面。

- **`chat_payload()` 里不许再出现任何厂商专属字段。** 全部交给 `providers.request_fields()`，
  它返回 `(standard, extras)`：`extras` 是非 OpenAI 标准的扩展（DeepSeek 的 `thinking`、
  GLM/Qwen 的 `enable_thinking`），走 HTTP 并进 body，**走 SDK 必须放 `extra_body`**——
  当普通关键字传给 OpenAI SDK 会被**静默丢掉**，模型就不思考了，而且没有任何报错。

- **「深度档」不是字符串 `max`。** DeepSeek 的最深档是 `max`，OpenAI/Claude 是 `xhigh`，
  GLM 压根没有强度维度。任何地方要判断「是不是在最深地思考」都得问
  `providers.is_deepest()`——拿 `== "max"` 去比，会让 OpenAI/Claude 在深度 preset 下
  用**浅档的 token 上限**，那就是决策 9 换了张皮（`effective_max_tokens` 正是靠它决定要不要 ×3）。

- **新增 provider 不许自己写 HTTP。** 一律走 `post_json()`——取消（`SHUT_RDWR`，决策 25）
  和钥匙串校验（决策 24）是靠「绕不过去」保证的，不是靠你记得再接一遍。Claude 的原生
  `/v1/messages` adapter 也走同一条路。

- **上下文窗口不确定就往小了写；价格不知道就写 `None`。** 猜小了只多切一块；猜大了撑爆上下文、
  整轮作废。价格未知一律显示「未配价格，未计入」——按别的模型的价瞎算正是决策 20 骂过的事。
- **本地选题是确定性排序，重跑结果一模一样。** 「换一批题」必须走 `--mode review-select
  --reselect`，把上一轮选中的题号告诉模型说老师不满意，否则它会原样再选一遍。
- **模型 JSON 里一个没转义的双引号就废掉整道题。** 写英文范文/词汇释义时模型迟早会用引号。
  解析器**不做静默修补**（会把范文悄悄改坏），而是让模型自己重发一次（`ask_for_json`，
  会话内前缀命中缓存，几乎不花钱）；再错才硬失败。explain 和 vocab 都走这条。
- **每次 API 调用都要记一行 `<out>/usage.jsonl`**（`record_usage`）。花费和耗时都从这里读，
  不要再回去扫 `api_conversations/*.md` 猜模型名——关掉「保留中间产物」就失灵了。
- **thinking 打开时 `max_tokens` 要覆盖「思考 + 回答」**——推理 token 计入输出配额，
  上限给小了会把 JSON 截断（曾经只剩 0 个 token 写 JSON）。解析失败不许静默降级。
- 导出必须通过 `docx_splice.validate()`。

- **HTTPS 必须走 macOS 钥匙串**（`net_tls.install()`，进程最开头调）。学校/公司代理和杀毒软件
  会在中间拆 TLS 并用自己的根证书重签；系统信任它，但 Python 自带的 CA 列表看不见，
  于是一台新电脑上 API 测试就报 `self-signed certificate in certificate chain`——**整个 App 等于废了**。
  certifi 救不了这种情况（企业根证书不在 Mozilla 的列表里），只有 `truststore` 能。
- **DeepSeek 的 `reasoning_effort` 只有 `high` 和 `max`。** 它把 `low`/`medium` 都映射成 `high`
  （官方文档写明），所以别再给它加「低强度」档位——那是个摆设。**但这是 DeepSeek 的事实，不是全局事实**：
  档位列表现在由 `ModelSpec.efforts` 声明，OpenAI 有 6 档，GLM 一档都没有。
  `max` 的思考链很长，而推理 token 算进输出配额，所以最深档必须同步放大 `max_tokens`。

- **两个 preset 都跑最深档，vocab 也不例外**（决策 31，用户明确要求）。这是**明知故犯**地
  重踩决策 9 的坑：vocab 放开思考会把预算烧在思考上、一个 JSON 都吐不出来。护栏已经从
  「限制 effort」换成「放大 output」（×3 并 clamp 到 `max_output`），加上 `require_parsed()`
  截断即硬失败。**如果 vocab 又开始报「被 max_tokens 截断」，就把它钉回标准档**——
  `test_both_presets_think_as_deeply_as_the_model_allows` 就是那个开关。
- **词汇表/解析给模型的引号规则要说清楚两条**：JSON 的**语法符号**必须是英文半角引号，
  只有字符串**内部**引用原文时才用中文引号。只说「不要用英文双引号」，flash 会把
  JSON 的键名也写成 `{“word”: “x”}`——真的挂过（vocab 跑到第 9 项崩）。
- **取消要真的能取消。** 光有标志位不够：worker 阻塞在 socket 读上，必须 `shutdown(SHUT_RDWR)`
  把连接拆掉（只 `close()` 唤不醒它）。DeepSeek 没有「停止生成」接口，断连是唯一手段，
  已发出的请求可能仍计费——界面要如实告诉老师。
