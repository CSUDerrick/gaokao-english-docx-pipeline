# 高三英语模拟题自动整理工具 (gaokao-english-docx-pipeline)

这是一个专门帮助高中英语老师把多份散乱的模拟题 Word 文档，自动整理成标准学生版、教师讲解版、答案汇总版并重新导出为规范 Word (.docx) 文档的本地自动化工具。

## 🎯 它能为您做什么？
- **保留原卷排版**: 选中的题目**直接从原卷里原样搬运**——字体、字号、加粗、下划线填空线、缩进、表格、插图全部与原卷一致。**您不需要重新调格式。**
- **智能切分**: 自动把整张试卷按阅读、完形、七选五、语法填空、写作等标准题型精准切分。
- **答案提取**: 自动提取分散在各处的答案，聚合成答案汇总版。
- **AI 教师讲解**: 分析长难句，补充核心词汇、语法点拨和教学建议。
- **AI 轻量评分选题**: 对题目难度与质量做评估，自动挑出每个题型里最值得练的。
- **支持 PDF 试卷**: 扫描件用 PaddleOCR-VL 识别后，同样走完整流程。

## 🚀 最推荐的使用方式（macOS）

到 [Releases](https://github.com/CSUDerrick/gaokao-english-docx-pipeline/releases) 下载 `.dmg`，
拖进「应用程序」，双击打开即可。

> ⚠️ 首次打开如果提示「无法打开，因为无法验证开发者」：**右键点图标 →「打开」→ 再点「打开」**。
> 只需要做这一次，之后双击就能开。（这是因为应用没有花钱做 Apple 公证。）

界面上：选试卷文件夹 → 填 DeepSeek API Key → 点「开始整理」。
API Key 保存在 macOS 钥匙串里，不会明文落盘。

## 📥 从源码运行（开发者 / Windows / Linux）

### 环境要求
- **Python** 3.10 或更高版本
- **Git**
- **DeepSeek API Key**（[申请](https://platform.deepseek.com/)，AI 评分与讲解需要）
- **PaddleOCR 令牌**（仅处理 PDF 时需要，见下文）

> 📌 已不再需要 Pandoc。题目原文改为直接克隆原卷 OOXML，导出不再经过 Markdown。

```bash
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
cd gaokao-english-docx-pipeline

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt        # 核心
pip install -r requirements-gui.txt    # 可选：Streamlit 网页版界面
pip install -r requirements-app.txt    # 可选：macOS 原生应用 + 打包

python3 tests/run_tests.py             # 应显示 54 passed
```

命令行跑完整流程：

```bash
export DEEPSEEK_API_KEY=sk-...
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode stage1 --init --client http
python3 scripts/gaokao_english_docx_pipeline.py input_docx \
  --out outputs/gaokao_english --mode export-docx
```

### PDF 试卷

PaddleOCR 的解析服务是**按账号部署**的，没有统一地址，需要您自己去拿：

1. 令牌：https://aistudio.baidu.com/account/accessToken
2. 服务地址：https://aistudio.baidu.com/paddleocr/task

```bash
export PADDLEOCR_ACCESS_TOKEN=...
export PADDLEOCR_BASE_URL=https://<你的专属地址>.aistudio-app.com
```

把 PDF 和 docx 一起丢进 `input_docx/` 即可，PDF 会先 OCR 成 docx 再走同一条流程。

## 🛠 自己打包 macOS 应用

```bash
./packaging/build_macos.sh          # 产出 dist/*.app 和 dist/*.dmg
```

要发给其他老师用（免掉 Gatekeeper 警告），需要 Apple 开发者账号（$99/年），
然后设好环境变量再跑同一个脚本，代码无需改动：

```bash
export APPLE_DEV_ID="Developer ID Application: 你的名字 (TEAMID)"
export APPLE_ID=...  APPLE_TEAM_ID=...  APPLE_APP_PASSWORD=...
./packaging/build_macos.sh          # 自动签名 + 公证
```

发布新版本：`git tag v0.4.0 && git push --tags` —— CI 会自动构建 DMG 并发到
Releases，应用内「检查更新」就是跟它比对的。

### Windows

```powershell
winget install Python.Python.3.13
winget install Git.Git
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
cd gaokao-english-docx-pipeline
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-gui.txt
streamlit run gui_app.py
```

### Linux (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
cd gaokao-english-docx-pipeline
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-gui.txt
streamlit run gui_app.py
```

### CLI 模式（无图形界面）

适用于服务器环境：

```bash
# 将你的 .docx 文件放入 input_docx 目录
mkdir -p input_docx
# 拷贝试卷文件...
cp /path/to/your/mock_exam.docx input_docx/

# 一键运行完整流水线（本地切分 + AI 讲解）
python3 scripts/gaokao_english_docx_pipeline.py \
  input_docx \
  --out outputs/gaokao_english \
  --mode stage1 \
  --init

# 仅切分验收（不消耗 API 额度）
python3 scripts/gaokao_english_docx_pipeline.py \
  input_docx \
  --out outputs/gaokao_english_segment_check \
  --mode segment \
  --init \
  --segment-input local \
  --no-segment-warning-fallback

# 导出为 .docx
python3 scripts/gaokao_english_docx_pipeline.py \
  input_docx \
  --out outputs/gaokao_english \
  --mode export-docx
```

### 配置 API Key

在 GUI 的 **"基础模式"** 中粘贴你的 DeepSeek API Key，或在命令行中设置环境变量：

```bash
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

> 免费申请地址：[platform.deepseek.com](https://platform.deepseek.com/)

---

## 💰 免费与计费功能说明（省钱攻略）
本工具最大程度保护您的钱包，核心高频功能完全在您的电脑本地运行，无需消耗任何 API 额度：
- **完全本地免费运行**: 试卷本地切分、切分质量检查、答案修复、质量报告查看、最终 Word (.docx) 导出、GUI 调试验收模式。
- **按需调用模型**: 正常试卷保持本地切分；仅结构性 WARN/FAIL 的异常试卷自动触发模型 rough 重切。
- **需要消耗 API 额度**: 异常模型重切、AI 轻量评分、教师讲解补充、AI 复核筛选。

## 📦 核心输出文件
所有整理好的文件都会妥善存放在 `outputs/` 目录下：
1. `高三英语精选试题_学生版.docx`：A4 试卷版式，按题型分页，适合直接打印训练。
2. `高三英语精选试题_教师讲解版.docx`：A4 讲义版式，包含词汇、语法、长难句和教学建议。
3. `高三英语精选试题_答案汇总版.docx`：紧凑答案版式，供快速核对。

**学生版和教师讲解版里的题目原文，是从原卷里逐段搬运过来的**——不是重新排版的。
所以字体、字号、加粗、下划线填空线、缩进、表格和插图都和原卷一模一样，您不需要再调格式。
AI 讲解作为新段落插入，用统一的「讲解正文 / 讲解标题」样式，可以在 Word 的样式面板里一键改。

答案汇总版没有原始排版可以继承（内容本来就是答案字母和范文），仍由模板生成。

## 🧭 网页模式

- **基础模式**：一键运行，只展示进度和预计时间。
- **进阶模式**：可调整模型、并发和回退策略，日志在固定文本框中自动滚动。
- **调试模式**：按真实顺序提供单步按钮，并根据前置产物自动禁用不可运行步骤。
- **数据管理**：分别提供“清空输入目录”和“清空输出目录”，每次都会显示路径、文件数量并二次确认。

## ⚠️ 注意事项
1. **默认并发**: 切分、评分、讲解默认均为 16；若接口返回 429，可在进阶模式降低并发。
2. **人工复核**: AI 生成的讲解内容仅供教学参考，请老师们在上课前进行最终的人工核对。
