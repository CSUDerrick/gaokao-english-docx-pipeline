"""Persisted app settings, so a second run is one click.

Only preferences live here — API keys go to the macOS Keychain, never to disk.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]
EFFORTS = ["none", "low", "medium", "high"]


@dataclass
class Settings:
    input_dir: str = ""
    output_dir: str = ""
    mode: str = "basic"  # basic | advanced | debug
    seen_intro: bool = False

    # advanced
    segment_model: str = "deepseek-v4-flash"
    score_model: str = "deepseek-v4-flash"
    enrich_model: str = "deepseek-v4-flash"
    review_model: str = "deepseek-v4-pro"
    score_effort: str = "none"
    enrich_effort: str = "none"
    review_effort: str = "medium"
    workers: int = 16
    review_select: bool = False

    # debug
    keep_intermediates: bool = True
    verbose: bool = False

    _path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}  # a corrupt settings file must not block startup
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and not k.startswith("_")}
        obj = cls(**known)
        obj._path = path
        return obj

    def save(self) -> None:
        if self._path is None:
            return
        payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
