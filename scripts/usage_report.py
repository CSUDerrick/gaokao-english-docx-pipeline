#!/usr/bin/env python3
"""What a run cost, read off the ledger the pipeline writes as it goes.

Every API call appends one line to ``<out>/usage.jsonl`` (see ``record_usage``),
carrying the stage, the model it actually ran on, the token split and the wall time.
That is the whole input here.

The previous version reconstructed all of this after the fact by scanning
``scores/``, ``explanations/`` and ``vocab/`` for embedded usage blocks, and
recovering the model by *grepping the saved markdown transcripts*. Two things were
wrong with it and both understated or overstated real money:

* turning off 保留中间产物 removed the transcripts, so every run was priced as if it
  had used pro — a flash run was quoted at roughly 3x its true cost;
* ``segment`` and ``review-select`` write no per-item directory, so their calls were
  never counted at all.

A run made before the ledger existed still reports: the old scan is kept as a
fallback.

    python3 scripts/usage_report.py outputs/gaokao_english
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import providers as pv

DEFAULT_MODEL = "deepseek-v4-pro"
USD_TO_CNY = 7.1


def rate(model: str, provider: str = "") -> pv.PriceSpec | None:
    """USD per 1M tokens for this model, or None if we do not know.

    Prices live on the ModelSpec in providers.py, one table for everything.

    **None means none.** The old code said ``PRICES.get(model) or PRICES[DEFAULT_MODEL]``,
    which quietly priced anything it did not recognise at pro's rate — 决策 20 is about
    exactly that: a flash run was quoted at roughly 3x what it really cost. With half a
    dozen providers and model ids that churn, an unknown model is now normal, so the
    honest answer is to say so rather than to invent a number.
    """
    if provider:
        spec = pv.model_spec(provider, model)
        if spec.price:
            return spec.price
    # A ledger line from before providers existed carries no provider. Fall back to
    # whichever provider lists this exact model id.
    for candidate in pv.PROVIDERS.values():
        for spec in candidate.models:
            if spec.id == model and spec.price:
                return spec.price
    return None

LEDGER = "usage.jsonl"

# Only used to read runs made before the ledger existed.
LEGACY_STAGE_DIRS = {
    "score": "scores",
    "explain": "explanations",
    "enrich": "enrichments",
    "vocab": "vocab",
}

STAGE_LABELS = {
    "segment": "切分",
    "score": "评分",
    "review_select": "复核选题",
    "explain": "逐题解析",
    "vocab": "词汇表",
    "enrich": "备课笔记",
}

# Report order: the sequence a run actually goes through.
STAGE_ORDER = ["segment", "score", "review_select", "explain", "vocab", "enrich"]


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    fresh: int = 0
    output: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0
    # Calls we could not price, because we have no rate for that model. Reported rather
    # than folded into the total: a cost line that silently omits half a run is worse
    # than one that admits it.
    unpriced: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            self.calls + other.calls,
            self.cached + other.cached,
            self.fresh + other.fresh,
            self.output + other.output,
            self.seconds + other.seconds,
            self.cost_usd + other.cost_usd,
            self.unpriced + other.unpriced,
        )

    @property
    def prompt(self) -> int:
        return self.cached + self.fresh

    @property
    def hit_rate(self) -> float:
        return self.cached / self.prompt if self.prompt else 0.0

    @property
    def cost_cny(self) -> float:
        return self.cost_usd * USD_TO_CNY

    @property
    def priced(self) -> bool:
        return self.unpriced == 0


def price(cached: int, fresh: int, output: int, model: str, provider: str = "") -> float | None:
    spec = rate(model, provider)
    if spec is None:
        return None
    return (cached * spec.hit + fresh * spec.miss + output * spec.out) / 1_000_000


def price_call(usage: dict, model: str, provider: str = "") -> Usage:
    """Price one call from the API's own usage block."""
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("prompt_cache_hit_tokens") or usage.get("cache_hit") or 0)
    # Older logs only carry prompt_tokens; treat everything as a miss then.
    fresh = int(usage.get("prompt_cache_miss_tokens") or usage.get("cache_miss") or max(prompt - cached, 0))
    output = int(usage.get("completion_tokens") or 0)
    cost = price(cached, fresh, output, model, provider)
    return Usage(
        calls=1,
        cached=cached,
        fresh=fresh,
        output=output,
        seconds=float(usage.get("seconds") or 0.0),
        cost_usd=cost or 0.0,
        unpriced=0 if cost is not None else 1,
    )


def read_ledger(out_dir: Path) -> list[dict]:
    path = Path(out_dir) / LEDGER
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written line from a killed run must not lose the rest
    return entries


def collect(out_dir: Path) -> dict[tuple[str, str], Usage]:
    """Usage per (stage, model). Two models in one run stay two rows."""
    entries = read_ledger(out_dir)
    if not entries:
        return _collect_legacy(out_dir)

    totals: dict[tuple[str, str], Usage] = {}
    for entry in entries:
        key = (str(entry.get("stage") or "?"), str(entry.get("model") or DEFAULT_MODEL))
        provider = str(entry.get("provider") or "")
        totals[key] = totals.get(key, Usage()).add(price_call(entry, key[1], provider))
    return totals


def _collect_legacy(out_dir: Path) -> dict[tuple[str, str], Usage]:
    """Runs made before the ledger existed: scan the per-stage output JSON."""
    totals: dict[tuple[str, str], Usage] = {}
    for stage, folder in LEGACY_STAGE_DIRS.items():
        stage_dir = Path(out_dir) / folder
        if not stage_dir.is_dir():
            continue
        for path in sorted(stage_dir.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            usage = row.get("usage")
            if not isinstance(usage, dict):
                continue
            model = str(row.get("model") or DEFAULT_MODEL)
            key = (stage, model)
            totals[key] = totals.get(key, Usage()).add(price_call(usage, model))
    return totals


def grand_total(totals: dict[tuple[str, str], Usage]) -> Usage:
    grand = Usage()
    for usage in totals.values():
        grand = grand.add(usage)
    return grand


def summary_line(out_dir: Path) -> str:
    """One line for the GUI: what this run cost, and how much the cache saved."""
    totals = collect(out_dir)
    if not totals:
        return "本次花费：暂无用量数据"
    grand = grand_total(totals)
    cost = (
        f"本次花费 ≈ ¥{grand.cost_cny:.2f}（${grand.cost_usd:.3f}）"
        if grand.priced
        else f"本次花费 ≈ ¥{grand.cost_cny:.2f}（另有 {grand.unpriced} 次调用的模型未配价格，未计入）"
    )
    return (
        f"{cost} · {grand.calls} 次调用 · "
        f"输入 {grand.prompt:,} / 输出 {grand.output:,} tokens · "
        f"缓存命中 {grand.hit_rate * 100:.0f}%"
    )


def _sort_key(key: tuple[str, str]) -> tuple[int, str]:
    stage, model = key
    order = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)
    return (order, model)


def report(out_dir: Path) -> str:
    totals = collect(out_dir)
    if not totals:
        return "暂无用量数据。"

    header = (
        f"{'阶段':<6}{'模型':<20}{'调用':>5}{'输入':>10}{'命中':>10}"
        f"{'未命中':>9}{'输出':>9}{'耗时':>8}{'花费(¥)':>10}"
    )
    lines = [header, "-" * len(header)]
    for key in sorted(totals, key=_sort_key):
        stage, model = key
        usage = totals[key]
        label = STAGE_LABELS.get(stage, stage)
        cost = f"{usage.cost_cny:>10.3f}" if usage.priced else f"{'未配价格':>8}"
        lines.append(
            f"{label:<6}{model:<20}{usage.calls:>5}{usage.prompt:>10,}{usage.cached:>10,}"
            f"{usage.fresh:>9,}{usage.output:>9,}{usage.seconds:>7.0f}s{cost}"
        )

    grand = grand_total(totals)
    lines.append("-" * len(header))
    lines.append(
        f"{'合计':<6}{'':<20}{grand.calls:>5}{grand.prompt:>10,}{grand.cached:>10,}"
        f"{grand.fresh:>9,}{grand.output:>9,}{grand.seconds:>7.0f}s{grand.cost_cny:>10.3f}"
    )
    if not grand.priced:
        lines.append(f"（其中 {grand.unpriced} 次调用的模型没有配价格，未计入合计）")

    # What the prefix cache is worth: the same tokens at the miss rate. Only over the
    # models we can actually price — a saving figure that silently skipped half the run
    # would be worse than no figure.
    without_cache = 0.0
    for (_, model), usage in totals.items():
        full = price(0, usage.prompt, usage.output, model)
        if full is not None:
            without_cache += full
    saved = without_cache - grand.cost_usd
    if saved > 0:
        lines.append("")
        lines.append(f"缓存省下 ≈ ¥{saved * USD_TO_CNY:.2f}（不缓存要 ¥{without_cache * USD_TO_CNY:.2f}）")

    lines.append("")
    lines.append(summary_line(out_dir))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    print(report(Path(sys.argv[1] if len(sys.argv) > 1 else "outputs/gaokao_english")))
