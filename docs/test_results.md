# Test Results & Benchmarks

- **自动化回归**: `python3 tests/run_tests.py` — **201 passed, 0 failed**（v0.8）。
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
