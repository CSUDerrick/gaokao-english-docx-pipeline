#!/usr/bin/env python3
"""Locate bundled data whether running from source or from a packaged .app.

PyInstaller flattens ``scripts/`` onto the top level, so a module's ``__file__``
becomes ``<bundle>/mod.py`` rather than ``<root>/scripts/mod.py``. Walking up two
parents therefore lands *above* the bundle, and the assets silently disappear —
which is how a packaged build ended up writing a US Letter answer sheet after
failing to find its A4 template.
"""

from __future__ import annotations

import sys
from pathlib import Path


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def template_dir() -> Path:
    return bundle_root() / "assets" / "word_templates"


def tokenizer_path() -> Path:
    """DeepSeek's published V3 vocabulary, used to count tokens offline."""
    return bundle_root() / "assets" / "deepseek_tokenizer" / "tokenizer.json"


def prompt_dir() -> Path:
    """Where the per-question-type explanation prompts live.

    They are markdown rather than Python string literals so a teacher can read and
    tweak the wording without touching code — which means they are data, and the
    packaged app has to ship them (``--add-data "prompts:prompts"``).
    """
    return bundle_root() / "prompts"
