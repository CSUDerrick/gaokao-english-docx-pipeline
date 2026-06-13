# 高三英语模拟题自动整理工具 (gaokao-english-docx-pipeline)

这是一个专门帮助高中英语老师把多份散乱的模拟题 Word 文档，自动整理成标准学生版、教师讲解版、答案汇总版并重新导出为规范 Word (.docx) 文档的本地自动化工具。

## 🎯 它能为您做什么？
- **智能切分**: 自动把整张试卷按听力、阅读、完形、填空、写作等标准题型精准切分。
- **答案提取**: 自动为您提取分散在各处的答案，聚合成一键直达的答案汇总版。
- **AI 教师讲解**: 自动分析长难句，补充核心词汇拓展、语法点拨，并为您生成教学建议。
- **AI 轻量评分**: 对试卷和模拟题的难易度与设计质量进行轻量级评估。

## 🚀 最推荐的使用方式
打开工具的图形界面（GUI），在 **"基础模式"** 下粘贴您的 DeepSeek API Key，点击一键运行，即可静待完整的整理成果。

## 📥 快速部署

### 环境要求
- **Python** 3.10 或更高版本
- **Git**（用于克隆仓库）
- **Pandoc**（用于导出 .docx，可选但强烈推荐）
- **DeepSeek API Key**（[免费申请](https://platform.deepseek.com/)，用于 AI 讲解功能）

> 💡 **零依赖模式**：如果只需要试卷切分、答案提取等本地功能，无需安装任何 pip 包，Python 标准库即开即用。

### macOS

```bash
# 1. 安装系统依赖（如已安装可跳过）
brew install python@3.13 pandoc git

# 2. 克隆项目
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
cd gaokao-english-docx-pipeline

# 3. 创建虚拟环境并激活
python3.13 -m venv .venv
source .venv/bin/activate

# 4. 安装 GUI 依赖
pip install -r requirements-gui.txt

# 5. （可选）安装 openai SDK，获得更好的 API 性能
pip install openai

# 6. 启动图形界面
streamlit run gui_app.py
```

### Windows

在 PowerShell 或命令提示符中执行：

```powershell
# 1. 安装系统依赖
#    方式 A：通过 winget（Win10/Win11 自带）
winget install Python.Python.3.13
winget install Pandoc.Pandoc
#    方式 B：手动下载安装
#    Python: https://www.python.org/downloads/
#    Pandoc: https://pandoc.org/installing.html
#    Git:    https://git-scm.com/download/win

# 2. 克隆项目
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
cd gaokao-english-docx-pipeline

# 3. 创建虚拟环境并激活
python -m venv .venv
.venv\Scripts\activate

# 4. 安装 GUI 依赖
pip install -r requirements-gui.txt

# 5. （可选）安装 openai SDK
pip install openai

# 6. 启动图形界面
streamlit run gui_app.py
```

### Linux (Ubuntu/Debian)

```bash
# 1. 安装系统依赖
sudo apt update
sudo apt install -y python3.12 python3.12-venv pandoc git

# 2. 克隆项目
git clone https://github.com/CSUDerrick/gaokao-english-docx-pipeline.git
cd gaokao-english-docx-pipeline

# 3. 创建虚拟环境并激活
python3.12 -m venv .venv
source .venv/bin/activate

# 4. 安装 GUI 依赖
pip install -r requirements-gui.txt

# 5. （可选）安装 openai SDK
pip install openai

# 6. 启动图形界面
streamlit run gui_app.py
```

### CLI 极简模式（无 GUI，零 pip 依赖）

适用于服务器或无图形界面的环境，仅使用标准库即可完成试卷切分和答案提取：

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
  --segment-input local

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
- **需要消耗 API 额度**: AI 轻量评分、教师讲解补充（词汇/语法/长难句深度解析）、AI 复核筛选。

## 📦 核心输出文件
所有整理好的文件都会妥善存放在 `outputs/` 目录下：
1. `学生自测版.md / .docx`：隐去答案，直接供学生打印练习。
2. `教师讲解版.md / .docx`：包含全套词汇扩展、长难句图解和教学建议。
3. `答案汇总版.md`：快速批改参考。

## ⚠️ 注意事项
1. **防止限流**: 如果批量处理文件过多提示请求频繁，请在 GUI 界面中调小"并发参数"。
2. **人工复核**: AI 生成的讲解内容仅供教学参考，请老师们在上课前进行最终的人工核对。
