#!/usr/bin/env python3
"""How long a stage will take, learned from how long it took last time.

The GUI used to extrapolate linearly over ``(stage_index + fraction) / stage_count``,
which weights every stage equally. Segmentation is local and finishes in under a
second; explanation takes minutes. So the bar jumped to 11% instantly and then sat
there, and the "预计剩余" it printed early in a run was off by an order of magnitude.

The unit of work differs per stage — papers for segmentation, questions for the
per-item stages, the whole run for the local ones — so what is stored is
**seconds per unit**, keyed by everything that actually moves the number:

    (stage, model, effort, thinking)

flash is several times faster than pro; thinking and a higher effort both cost real
seconds. A key with no history falls back to a built-in prior measured on this
repo's own runs, and every finished stage appends a sample — so the estimate is
roughly right on the first run and converges after that.

Nothing here talks to the network: the history file is the only input.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

# Seconds per unit, measured on a 3-paper run (flash, medium, thinking on).
# Only used until that key has real history of its own.
PRIORS: dict[str, float] = {
    "segment": 0.4,        # per paper, local — the model is only an odd-paper fallback
    "score": 2.3,          # per question, 16 workers
    "select": 0.3,         # whole stage; local ranking
    "review-select": 4.0,  # per section, one call each
    "explain": 9.5,        # per question; conversations are serial within a paper
    "vocab": 6.0,          # per question, same shape as explain
    "assemble": 1.0,       # whole stage
    "repair-answers": 1.0,
    "export-docx": 10.0,   # whole stage; docxcompose merges ~40 parts
}

# Stages whose cost does not scale with the number of questions.
FIXED_STAGES = {"select", "assemble", "repair-answers", "export-docx"}

# pro thinks longer than flash for the same prompt. Applied to the prior only —
# once a key has its own samples they already carry this.
MODEL_FACTORS = {"deepseek-v4-flash": 1.0, "deepseek-v4-pro": 2.6}
EFFORT_FACTORS = {"none": 0.6, "low": 0.8, "medium": 1.0, "high": 1.3}

MAX_SAMPLES = 20  # per key; enough to be stable, short enough to follow a real change


def _key(stage: str, model: str, effort: str, thinking: str) -> str:
    return f"{stage}|{model}|{effort}|{thinking}"


class Timings:
    """The rolling history, persisted next to the app's settings."""

    def __init__(self, path: Path):
        self.path = path
        self.samples: dict[str, list[float]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.samples = {
                        k: [float(x) for x in v][-MAX_SAMPLES:]
                        for k, v in data.items()
                        if isinstance(v, list)
                    }
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                self.samples = {}  # a corrupt history must not block a run

    def per_unit(self, stage: str, model: str, effort: str, thinking: str) -> float:
        """Seconds per unit for this exact configuration."""
        history = self.samples.get(_key(stage, model, effort, thinking))
        if history:
            # Median, not mean: one 429-throttled run must not poison the estimate.
            return statistics.median(history)

        prior = PRIORS.get(stage, 2.0)
        if stage in FIXED_STAGES or stage == "segment":
            return prior  # local work; the model does not touch it
        return prior * MODEL_FACTORS.get(model, 1.0) * EFFORT_FACTORS.get(effort, 1.0) * (
            1.0 if thinking == "enabled" else 0.7
        )

    def estimate(self, stage: str, units: int, model: str, effort: str, thinking: str) -> float:
        """Seconds this stage should take."""
        scale = 1 if stage in FIXED_STAGES else max(1, units)
        return self.per_unit(stage, model, effort, thinking) * scale

    def record(self, stage: str, units: int, model: str, effort: str, thinking: str, seconds: float) -> None:
        """Fold one finished stage into the history."""
        scale = 1 if stage in FIXED_STAGES else max(1, units)
        per_unit = seconds / scale
        if per_unit <= 0:
            return
        key = _key(stage, model, effort, thinking)
        history = self.samples.setdefault(key, [])
        history.append(round(per_unit, 3))
        del history[:-MAX_SAMPLES]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.samples, indent=2), encoding="utf-8")
        except OSError:
            pass  # a read-only home directory costs accuracy, not the run


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分 {secs} 秒" if minutes else f"{secs} 秒"
