# 高三英语模拟题 docx 自动整理流程

这个小工具用于批量处理英语模拟题 `.docx`：

1. 批量提取 docx 文本
2. 尽量按题型切分为阅读 A/B/C/D、七选五、完形、语法填空、应用文、读后续写
3. 为每个候选篇目生成“单篇标注评分”提示词
4. 可选：调用 DeepSeek/OpenAI 兼容 API 自动分析
5. 输出 JSONL、CSV 和最终横向筛选提示词

## 第一阶段新流水线：preflight -> segment -> score -> select -> enrich -> assemble

推荐先用这个新流程。它把任务拆成多步，便于审核，也能减少后续 token：

```text
preflight  本地预检 docx 数量、可识别题目数、预计调用量，不调用 API
segment    默认本地按高考试卷结构切成题型 JSON，保存题目和答案，不调用 API
score      用 deepseek-v4-flash 对每个题目做轻量评分，不输出词汇/长难句清单
select     程序只读取评分，本地选出各题型候选
review-select  可选：用 deepseek-v4-pro 对本地候选做最终复核
enrich-selected  只对最终入选题目补充词汇、语法词形、长难句
assemble   从本地 segments 取题目原文，把入选题目组合成 Markdown，答案统一放最后
```

建议先预检，确认本地切割能识别出每份卷子的 9 个题型单元：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode preflight
```

一键跑完整第一阶段：

```bash
export DEEPSEEK_API_KEY="你的 key"

python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode stage1 \
  --init \
  --client http \
  --review-select \
  --segment-workers 4 \
  --score-workers 4 \
  --enrich-workers 2 \
  --max-retries 8
```

默认分工：

```text
segment_input = local，本地切割，不调用 API
score_model   = deepseek-v4-flash，只做轻量评分
select        = 本地程序按评分取候选，不再输入完整题目
review_model  = deepseek-v4-pro，仅输入评分摘要，不输入完整题目
enrich_model  = deepseek-v4-flash，只处理最终入选题
assemble      = 本地程序组合题目和答案，不再调用模型
```

为了降低成本，`segment` 和 `score` 默认显式关闭 thinking；`review-select` 默认开启 thinking medium：

```text
segment_thinking = disabled
score_thinking   = disabled
review_thinking  = enabled
enrich_thinking  = disabled
review_reasoning_effort = medium
```

注意：`omit` 不是“关闭 thinking”，它表示不传这个参数，服务端可能使用默认 thinking。若要控成本，优先使用 `disabled`。

`segment` 默认本地切割：

```text
segment_input = local
```

这样不会产生切割阶段 API token。若某份试卷本地切割质量不好，可以切到 `rough` 或 `full`，再让模型切割：

```bash
--segment-input rough
--segment-input full
```

遇到 `429 Too Many Requests` 时，程序会自动等待并重试。仍频繁 429 时，先降低：

```bash
--score-workers 2 --enrich-workers 1 --max-retries 12
```

如果你想分步审核，按这个顺序跑：

```bash
# 1. 切割试卷，输出题目和答案 JSON
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode segment \
  --init \
  --client http

# 审核 outputs/gaokao_english/segment_index.csv

# 2. 对切割后的题目评分
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode score \
  --client http \
  --score-workers 4

# 审核 outputs/gaokao_english/score_index.csv

# 3. 根据评分本地选题
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode select

# 审核 outputs/gaokao_english/selected_items.csv

# 3.5 可选：用 pro 对每类前 6 个候选复核，最终每类选 2 个
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode review-select \
  --client http

# 审核 outputs/gaokao_english/review_select_notes.json 和 selected_items.csv

# 4. 只对最终入选题补充词汇、语法词形和长难句
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode enrich-selected \
  --client http \
  --enrich-workers 2

# 5. 本地组合最终 Markdown，答案统一放最后
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode assemble
```

第一阶段主要输出：

```text
outputs/gaokao_english/segments/                         每个切割后的题目 JSON
outputs/gaokao_english/rough_segments/                   本地粗切结果，供审核和节省 token
outputs/gaokao_english/segment_index.csv                 切割审核表
outputs/gaokao_english/scores/                           每个题目的评分 JSON
outputs/gaokao_english/score_index.csv                   评分审核表
outputs/gaokao_english/selected_items.csv                入选题目清单
outputs/gaokao_english/review_select_notes.json          pro 复核选择理由（如果运行 review-select）
outputs/gaokao_english/enrichments/                      最终入选题的词汇/语法/长难句补充 JSON
outputs/gaokao_english/assembled/final_selected_questions_with_answers.md
outputs/gaokao_english/assembled/final_teacher_notes.md
outputs/gaokao_english/assembled/final_answers_only.md
outputs/gaokao_english/api_conversations/                每次 API 调用的 prompt 和输出，Markdown 保存
outputs/gaokao_english/run_quality_report.md             自动质量报告（运行 --mode quality-report）
```

如果不想保存每次 API 对话，添加：

```bash
--no-save-conversations
```

### 修复答案（不调用 AI）

如果 `segment_index.csv` 中某些卷子 `answer_count` 偏低（常见原因是 docx 答案区使用了表格格式、波浪号 `~`、双短横 `--` 或语法填空拼接在一起），运行 `repair-answers` 重新扫描全文提取答案，并自动重新 assemble：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode repair-answers
```

这个模式：
- 不调用 AI，不修改 score / review / enrich 结果
- 读取 `extracted_text/*.txt` 全文，支持 `-` `—` `~` `--` 四种分隔符，有无空格均可
- 支持语法填空拼接格式（如 `56. would spark57. playfully`）
- 更新 `segments/*.json` 的 `answer_key`，重新生成 `segment_index.csv`
- 自动运行 `assemble` 使答案生效

如果某份试卷确实没有答案区，会被标记为 `原卷未提供答案`。

### 自动质量报告（不调用 AI）

生成一份 Markdown 质量报告，汇总全流程输出：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode quality-report
```

报告包含：
- 输入/输出概览（docx 数、segments、评分、入选数、API 调用次数）
- 每卷 × 每题型答案覆盖矩阵
- 每题型评分均分分布
- 入选题目清单
- Pro 复核摘要
- API token 用量估算
- 输出文件大小一览

输出位置：`outputs/gaokao_english/run_quality_report.md`。

### 回归测试

答案解析函数的回归测试位于 `tests/test_answer_extraction.py`。不依赖 pytest，直接运行：

```bash
python3 tests/test_answer_extraction.py
```

覆盖的格式：
- 标准短横线：`21-23 BDC`
- 全角破折号 + 句号：`21—23. BDB`
- 波浪号：`21~23 CDB` / `21~23DBC`（无空格）
- 双短横线：`41--45. DADBA`
- 语法填空拼接：`56. would spark57. playfully`
- 5 份真实试卷烟雾测试（每份必须达到 35/35 选择题 + 10/10 语法填空）

## GUI 运行方式

命令行模式会继续保留。如果你想用图形界面调参数、分步运行和审核表格，可以启动 Streamlit GUI。

第一次使用先安装 GUI 依赖。建议在虚拟环境里安装：

```bash
cd /Users/junyouchen/Documents/Codex/2026-05-29/docx-1-2-a-2-3
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-gui.txt
```

启动 GUI：

```bash
streamlit run gui_app.py
```

GUI 包含：

```text
状态：查看 docx 数量、segments、scores、selected、最终文件是否生成
运行：上传 docx、初始化、分步运行 segment/score/select/review-select/assemble、
      repair-answers（修复答案）、quality-report（质量报告）、stage1 一键运行
审核：预览 segment_index.csv、score_index.csv、selected_items.csv、review_select_notes.json
结果：预览和下载最终 Markdown/CSV
```

GUI 支持中英文切换。语言切换在左侧边栏顶部：

```text
界面语言 / Language
中文 / English
```

每个主要参数旁边都有悬停说明。鼠标靠近参数右侧的小问号，可以看到这个参数的用途、推荐值和成本/质量影响。

GUI 不会替代命令行。它只是把参数表单转成同一个命令行脚本调用，所以 GUI 和 TUI 的行为一致。

API key 有两种方式：

```text
1. 推荐：在终端 export DEEPSEEK_API_KEY="你的 key"，GUI 默认读取环境变量
2. 临时：在 GUI 侧边栏输入 API Key，只传给当前子进程
3. 记忆：在 GUI 侧边栏输入 API Key 后点击“保存 API Key”
```

保存后的 API Key 会写入本项目的本地文件：

```text
.local/gui_secrets.json
```

GUI 会尽量把该文件权限设置为仅当前用户可读写。你也可以在 GUI 里点击“清除 Key”删除它。

## 文件夹建议

把试卷放到：

```text
input_docx/
```

例如：

```text
input_docx/2026北京一模.docx
input_docx/2026杭州二模.docx
input_docx/2026南京三模.docx
```

## 第一步：生成提取文本和分析提示词

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode prompts
```

输出内容：

```text
outputs/gaokao_english/extracted_text/          每份 docx 的纯文本
outputs/gaokao_english/items.jsonl              自动切分出的候选篇目
outputs/gaokao_english/analysis_prompts.jsonl   每篇对应的单篇分析 prompt
outputs/gaokao_english/analysis_index.csv       方便检查的索引表
outputs/gaokao_english/final_selection_prompt.md 横向筛选总 prompt
```

如果自动切分不理想，先看 `extracted_text` 和 `items.jsonl`，把不合适的地方反馈给 Codex 微调规则。

## 中断后重新初始化

如果运行中 `Ctrl+C` 打断，可能留下半成品缓存。下次运行前可以初始化输出目录。

只清空输出目录，不继续运行：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --init-only
```

清空后立刻重新生成 prompts：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode prompts --init
```

清空后立刻重新跑模型分析：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode analyze --init
```

`--init` / `--init-only` 只清理 `--out` 指向的生成结果目录，不会删除 `input_docx` 里的原始试卷。脚本也会拒绝初始化项目根目录、`input_docx`、`scripts`、`config`、`.venv` 等受保护路径。

## 第二步：接入 DeepSeek API 自动分析

先安装 OpenAI SDK。DeepSeek 官方 Python 示例也是用这个 SDK，只是把 `base_url` 改成 DeepSeek。

如果你在 macOS/Homebrew Python 里遇到 `externally-managed-environment`，不要用 `--break-system-packages`，建议在本项目里建虚拟环境：

```bash
cd /Users/junyouchen/Documents/Codex/2026-05-29/docx-1-2-a-2-3
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install openai
```

之后每次重新打开终端，先激活虚拟环境：

```bash
cd /Users/junyouchen/Documents/Codex/2026-05-29/docx-1-2-a-2-3
source .venv/bin/activate
```

设置环境变量：

```bash
export DEEPSEEK_API_KEY="你的 key"
```

然后运行：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode analyze
```

运行时终端会显示：

```text
当前步骤
正在处理的 docx 文件
每份文档切分出的候选篇目数量
当前分析到第几篇 / 总篇数
当前篇目的来源文件、题型、篇目编号
DeepSeek 模型、thinking、reasoning_effort 等参数
API 返回耗时、token usage（如果接口返回）
AI reasoning/thinking 内容预览（如果接口返回）
AI 最终输出预览
JSON 是否解析成功
中间结果和最终文件是否成功写出
```

默认只显示 AI 输出预览，避免文档多时终端太长。你可以按需调整：

```bash
# 显示完整 AI 最终输出和完整 reasoning/thinking
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode analyze \
  --show-output full \
  --show-reasoning full

# 完全不在终端打印 AI 输出，只显示进度
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode analyze \
  --show-output none \
  --show-reasoning none

# 改变预览长度
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode analyze \
  --preview-chars 3000
```

默认参数已经按 DeepSeek 官方 OpenAI SDK 示例设置：

```text
base_url: https://api.deepseek.com
model: deepseek-v4-pro
reasoning_effort: high
thinking: enabled
stream: false
```

脚本里相关参数都用 `DEEPSEEK TUNING` 注释标出来了，方便你直接搜索修改。

如果你不想安装 OpenAI SDK，也可以使用内置 HTTP 备用模式：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode analyze --client http
```

这个模式不依赖 `openai` 包，只使用 Python 标准库，适合先快速跑通。

如果你要换成其他 OpenAI 兼容 API，通常需要关闭 DeepSeek 专属的 thinking 参数：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode analyze \
  --base-url "https://你的接口地址/v1" \
  --model "你的模型名" \
  --api-key-env YOUR_API_KEY_ENV \
  --thinking omit \
  --reasoning-effort none
```

## 第三步：横向筛选

自动分析完成后，会生成：

```text
outputs/gaokao_english/model_analyses.jsonl
outputs/gaokao_english/model_analyses.csv
outputs/gaokao_english/final_selection_prompt.md
```

你可以把 `model_analyses.jsonl` 或 CSV 内容连同 `final_selection_prompt.md` 发给 AI，让它完成最终筛选。

如果之后你希望全自动完成最终筛选，也可以继续运行：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode final
```

`final` 默认会把 `model_analyses.jsonl` 压成紧凑版再发给模型，自动去掉调试用的 `reasoning`、`usage` 等字段，避免最终筛选请求过大：

```text
outputs/gaokao_english/model_analyses.final_compact.jsonl
```

如果你确实想把完整 JSONL 原样发送给模型，可以加：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode final \
  --final-input full
```

如果 DeepSeek 返回 400，脚本现在会打印 API 返回的错误正文，方便判断是参数问题、上下文过长，还是账户/模型限制。

## 推荐实际用法

第一次跑建议只用：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx --out outputs/gaokao_english --mode prompts
```

先确认切分出来的篇目是否靠谱。等切分稳定后，再接 API 批量分析。
