# Test Results & Benchmarks

- **自动化回归**: `python3 tests/run_tests.py` — **255 passed, 0 failed**（v0.11）。
  新增 `test_merge_vocab.py`（18 条）：把导出器自己写出的 docx 读回来、四条去重规则、
  认不出的表跳过而不猜、一份都认不出就报错不写空表、合并两遍结果不变，
  以及一条 offscreen 的对话框端到端测试（**结果回调必须落在界面线程上**）。
  跑测试要用装了 PySide6 的解释器（`.venv/bin/python`）；`test_cancel.py` 会占用约 60 秒
  等它那个「永不应答」的测试服务器释放，那是正常的，不是卡死。
- **合并词汇表实测**（老师手上 9 份词汇表，其中 2 份是阅读/语法分开的）：
  阅读词汇 **1166 → 1032**，语法变形 **837 → 713**，两秒出结果，零 API 调用。
- **v0.8 基准**: `python3 tests/run_tests.py` — **201 passed, 0 failed**。
  新增：`test_providers.py`（每家 provider 的 payload 快照 + 会话预算）、
  `test_vocab_paper_input.py`（两种词汇模式 + 切块边界 + 答案区必须被砍掉）、
  `test_mineru_ingest.py`、`test_export_gate.py`（闸门按词表自带的模式分派）。
- **应用自检**: `python3 app/main.py --selftest` — 证明界面上的每个模型/强度/thinking/并发选项
  都真的进了 CLI（构造 Worker 的 argv，再用 pipeline 自己的 parser 读回来断言）。
- **导出体检**: `python3 scripts/check_export_quality.py --out <run>` — **19 passed, 0 failed**。
  断言：题干不含答案键/逐题解析、不吞下一节标题、题型顺序与位置一致；
  学生版无来源/无答案/无 `**`、按「第 N 篇」连续编号；三份 docx 书签配对且过 `validate()`；
  词汇表恰有 2 张表且非空。
- **本地切分**: 3 份真实试卷 → 每份 9 段（27 段），**PASS 3 / WARN 0 / FAIL 0**，全程零 API 调用。
  （修复前是 26 段、2 PASS + 1 structural WARN，且江苏卷需要一次付费的模型回退。）
- **端到端**: 3 份卷 → 18 道选题 → 学生版 / 教师讲解版 / 答案汇总版 / 重难点词汇表（263 词 + 227 词形）。
- **成本**（deepseek-v4-pro + thinking，含 vocab 阶段）: 约 **$0.19（¥1.35）/ 轮**，缓存命中 76%。
  - score 缓存命中 **96%**（先串行暖一次缓存再 fan-out；此前 71%）
  - enrich 缓存命中 **72%**（每份卷一个多轮会话；此前 21%）
- **Schema 校验**（ECMA-376 XSD，逐 part）: 四份成品相对「Word 能正常打开的文件」（三份原卷 + Word 修复版）
  **零独有违规**。此前学生版/教师版各有一处：空的 `cp:lastPrinted`（dateTime 置空）；
  词汇表/答案版另有 `tblBorders` 排在 `tblLook` 之后、`pgMar` 缺 `gutter`。
- **打包**: `./packaging/build_macos.sh` → `dist/高三英语试卷整理工具-0.4.0.dmg`（70 MB）。
  冻结包的 `--selftest` 会真的建一份 docx 并跑 `validate()`，不过就不出 DMG。
- **历史基准**: 25 份试卷、225 segments、0 structural WARN、0 FAIL 的 v0.1 基准继续保留作为回归参考。
