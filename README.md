# 高三英语模拟题自动整理工具

> 把一叠 Word 试卷放进去，自动切题、选题、讲解，生成学生版、教师版、答案版和 Word 文档。

📖 [技术说明 / Technical Guide](README.en.md) · [更新日志](CHANGELOG.md) · [MIT 许可证](LICENSE)

---

## 这个工具是做什么的？

如果你是高中英语老师，你可能经常需要：

- 从好几份模拟卷里挑出合适的题给学生练
- 整理每道题的词汇、语法点和长难句
- 把答案汇总成一份文件发给学生

**这个工具就是帮你自动完成这些事的。**

你只需要把 `.docx` 格式的试卷放进一个文件夹，工具会：

1. 自动把每份卷子切成阅读 A/B/C/D、七选五、完形、语法填空、应用文、读后续写
2. 用 AI 给每道题打分（题材新不新、难不难、词汇和语法有没有讲解价值）
3. 自动从 5 份卷子里选出每个题型最好的 2 道题
4. 对入选的题补充重点词汇、语法变形、长难句分析
5. 生成三份文件：学生版、教师讲解版、答案汇总
6. 还可以导出 Word 文档，直接打印或发给学生

## 适合谁用？

- 高中英语老师 — 备课、组卷、专题训练
- 教研组 — 批量整理模拟题
- 教培机构 — 快速制作讲义
- 不想手工整理几十道题的任何人

## 你需要准备什么？

- 一台 Windows 或 Mac 电脑
- 若干份 `.docx` 格式的英语模拟题（试卷）
- 如果要使用 AI 评分和讲解：一个 DeepSeek API key（[免费注册](https://platform.deepseek.com)）
- 如果只做本地切分和质量检查：**不需要** API key

## 最快上手：图形界面

### 第一步：安装 Python

去 [python.org](https://python.org) 下载 Python 3.10 或更高版本，安装时**勾选"Add Python to PATH"**。

### 第二步：下载项目

点击 GitHub 页面上方的绿色 **Code** 按钮 → **Download ZIP**，解压到任意文件夹。

或者你用 git：

```bash
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
```

### 第三步：安装依赖

打开终端（Mac）或 PowerShell（Windows），进入项目文件夹：

**Windows：**
```
python -m pip install -r requirements-gui.txt
python -m streamlit run gui_app.py
```

**Mac：**
```bash
python3 -m pip install -r requirements-gui.txt
python3 -m streamlit run gui_app.py
```

如果提示缺 `pip`，先运行 `python -m ensurepip`。

### 第四步：放进试卷

把 `.docx` 试卷放进项目里的 `input_docx` 文件夹。

### 第五步：填 API Key 并运行

1. 浏览器打开图形界面后，在左侧边栏填入你的 DeepSeek API key
2. 点击 **保存 API Key**
3. 点击「运行」标签 → **一键运行 stage1**
4. 等待完成（大约几分钟）。完成后可以在「结果」标签里预览和下载文件

### 第六步：看结果

打开项目里的 `outputs/gaokao_english/` 文件夹：

| 文件 | 用途 |
|------|------|
| `assembled/final_selected_questions_with_answers.md` | 学生版：入选题目 + 答案汇总 |
| `assembled/final_teacher_notes.md` | 教师版：评分理由、词汇、语法、长难句 |
| `assembled/final_answers_only.md` | 纯答案版 |
| `docx_exports/*.docx` | Word 版，可直接打开、打印 |
| `run_quality_report.md` | 质量报告 |

## 哪些功能不需要 API key？

这些功能完全在本地运行，**不花钱，不联网**：

- ✅ 本地切分 — 把试卷切成题型
- ✅ 切分质量检查 — 看有没有切坏
- ✅ 答案修复 — 重新扫描提取答案
- ✅ 质量报告 — 生成汇总报告
- ✅ Word 导出 — Markdown 转 docx
- ✅ 本地验收 — GUI 里一键完成以上全部

## 哪些功能需要 API key？

- 🔑 AI 评分 — 给每道题的题材、难度、词汇语法价值打分
- 🔑 AI 复核 — 从候选题里选出最好的
- 🔑 AI 讲解 — 对入选题目生成词汇表、语法点、长难句分析

如果只做切分和验收，**完全不需要 API key**。

## 如果遇到 429 限流怎么办？

这通常不是程序坏了，而是 API 请求太密集了。解决方法：

- 把并发调低：`--score-workers 2 --enrich-workers 1 --max-retries 12`
- 或者在 GUI 左边栏把 **score workers** 调到 2、**enrich workers** 调到 1

## 老师日常推荐流程

1. 把新试卷放进 `input_docx`
2. 打开 GUI，进入「本地验收」标签 → **一键验收**
3. 看到没有 FAIL 之后，进入「运行」标签 → **一键运行 stage1**
4. 进入「成本统计」查看用了多少 token
5. 进入「结果」标签预览 Markdown
6. 点击 **导出 Word** 获得 docx 文件
7. 人工快速检查后发给学生或备课使用

> ⚠️ AI 输出和答案修复结果仍然建议老师人工复核，不要直接当正式答案使用。

## 常见问题

### 必须会编程吗？

不用。下载 ZIP、安装 Python、点按钮就可以了。上面每一步都有说明。

### 支持 PDF 吗？

当前版本只支持 `.docx` 格式。PDF 需要先转成 Word。

### 为什么需要 API key？

AI 评分、选题和讲解需要调用大模型。目前用的是 DeepSeek，需要注册一个账号获取 key。评分用的是便宜的 `flash` 模型，处理一套卷子大概几分钱到一毛钱。

### 会不会把我的试卷上传？

程序的 AI 调用是直接发到 DeepSeek 的服务器。**不会**上传到本项目作者或 GitHub。如果你对隐私非常敏感，可以用本地验收功能，完全离线。

### 没有 API key 能做什么？

能做切分、质量检查、答案修复、Word 导出、质量报告。在 GUI 里使用「本地验收」标签，所有这些都不需要 key。

### 输出的 Word 在哪里？

`outputs/gaokao_english/docx_exports/` 文件夹里。

### 为什么有时会 429？

429 是 DeepSeek 告诉你"请求太多了，慢一点"。程序会自动重试。如果反复遇到，把 score workers 调到 2、enrich workers 调到 1。

### Windows 和 Mac 命令有什么不同？

主要区别是 Python 命令写法：
- Windows：`python`
- Mac：`python3`

其他基本相同。

### 可以直接商用/公开使用吗？

本项目采用 MIT 许可证，可以自由使用、修改和分发。详见 [LICENSE](LICENSE)。

---

## 高级用法：命令行

如果你习惯命令行操作，以下是完整流程。

### 全流程一键运行

```bash
export DEEPSEEK_API_KEY="你的 key"

python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode stage1 \
  --init \
  --client http \
  --review-select \
  --score-workers 4 \
  --enrich-workers 2 \
  --max-retries 8
```

限流时用保守版：

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode stage1 \
  --init \
  --client http \
  --review-select \
  --score-workers 2 \
  --enrich-workers 1 \
  --max-retries 12
```

### 只做本地切分验收（不花钱）

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english_segment_check \
  --mode segment \
  --init \
  --segment-input local

python3 scripts/check_segment_quality.py \
  --out outputs/gaokao_english_segment_check
```

### Word 导出

```bash
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english \
  --mode export-docx
```

### 运行测试

```bash
python3 tests/test_answer_extraction.py
python3 tests/test_segment_tail_trim.py
python3 tests/test_export_markdown_to_docx.py
```

---

## 给开发者/协作者

- [技术说明 / Technical Guide](README.en.md)
- [更新日志 / Changelog](CHANGELOG.md)
- [MIT 许可证](LICENSE)
- 当前版本：v0.1
- 验证状态：25 份 docx，225 个 segment，每卷 9 个题型，0 结构性告警
