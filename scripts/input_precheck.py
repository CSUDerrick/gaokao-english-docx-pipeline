"""Local, deterministic sanity check on extracted paper text — before any AI money is spent.

Mirrors ``segment_quality.py``: no third-party dependencies, importable from CLI / GUI /
tests. It only decides "this text looks suspect and why"; the pipeline decides whether to
escalate a suspect paper to a cheap model and how loudly to warn. It never blocks a run.

The failure it catches is a *bad input*: a PDF that OCR'd into mojibake, a docx that came
out almost empty, a scan that produced a wall of replacement characters. That is a different
problem from the model breaking its own JSON on a perfectly good paper — this module does
nothing about the latter.
"""

from __future__ import annotations

import re

REPLACEMENT_CHAR = "�"

# Thresholds are deliberately loose: this is a smoke alarm, not a grader. It should stay
# silent on every real paper and only trip on text that is visibly broken.
MIN_USEFUL_CHARS = 200          # below this a "paper" is empty/near-empty
MAX_REPLACEMENT_CHARS = 20      # U+FFFD count that means the decode already failed
MAX_REPLACEMENT_RATIO = 0.002   # or this share of the text, whichever hits first
MIN_LATIN_RATIO = 0.10          # an English paper that is nearly free of A–Z is suspect
MAX_CONTROL_RATIO = 0.02        # stray control bytes beyond \n \r \t
MAX_NOSPACE_RUN = 2000          # one unbroken non-whitespace run this long = layout collapse

_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[一-鿿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NOSPACE_RUN = re.compile(r"\S+")


def precheck_text(text: str) -> dict:
    """Return ``{"suspect": bool, "reasons": [str, ...]}`` for one paper's extracted text."""
    reasons: list[str] = []
    raw = text or ""
    stripped = raw.strip()

    if len(stripped) < MIN_USEFUL_CHARS:
        reasons.append(f"正文过短（仅 {len(stripped)} 字），可能抽取失败或整份是图片")
        # Everything below is a ratio over the text; on an almost-empty string they are noise.
        return {"suspect": True, "reasons": reasons}

    total = len(raw)
    replacements = raw.count(REPLACEMENT_CHAR)
    if replacements >= MAX_REPLACEMENT_CHARS or replacements / total >= MAX_REPLACEMENT_RATIO:
        reasons.append(f"出现 {replacements} 个 U+FFFD 替换字符，多半是编码/OCR 失败")

    controls = len(_CONTROL.findall(raw))
    if controls / total >= MAX_CONTROL_RATIO:
        reasons.append(f"控制字符占比 {100 * controls / total:.1f}%，文本可能已损坏")

    latin = len(_LATIN.findall(raw))
    cjk = len(_CJK.findall(raw))
    letters = latin + cjk
    # Only judge "not enough English" when there is meaningful letter content at all —
    # a page of pure symbols is already caught by the control/replacement checks.
    if letters >= 100 and latin / letters < MIN_LATIN_RATIO:
        reasons.append(
            f"英文字母占比过低（{100 * latin / letters:.0f}%），英语卷不该这样，可能抽取错乱"
        )

    longest = max((len(m.group(0)) for m in _NOSPACE_RUN.finditer(raw)), default=0)
    if longest >= MAX_NOSPACE_RUN:
        reasons.append(f"存在超长无空格串（{longest} 字），版面可能已塌陷")

    return {"suspect": bool(reasons), "reasons": reasons}


def describe(name: str, result: dict) -> str:
    """One-line human summary for the preflight log."""
    reasons = "；".join(result.get("reasons") or []) or "无"
    return f"{name}：{reasons}"
