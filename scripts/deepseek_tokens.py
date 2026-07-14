#!/usr/bin/env python3
"""Count tokens with DeepSeek's own tokenizer, so the cost estimate is not a guess.

The pipeline used to estimate ``len(text) // 3``. That is wrong in both directions
and by a lot: Chinese runs about 1.7 characters per token (the estimate *under*-counts
by ~40%), English about 4 (it *over*-counts by ~40%). A quote to a teacher of what a
run will cost is worth nothing if the token count is off by half.

The vocabulary ships in the repo (``assets/deepseek_tokenizer/tokenizer.json``, 7.5 MB,
DeepSeek's published V3 tokenizer). Only ``tokenizers`` is needed to read it —
``transformers`` would drag in most of the HF stack for a BPE table we already have.

A missing wheel or a missing vocab file must never stop a run: this is a *cost display*.
It degrades to the old character estimate and says so once.
"""

from __future__ import annotations

import functools

from bundle_paths import tokenizer_path

# Characters per token, measured on this repo's own prompts. Only used when the real
# tokenizer is unavailable — a bad number in the cost line beats a crashed run.
_CHARS_PER_TOKEN = 2.6


@functools.lru_cache(maxsize=1)
def _tokenizer():
    """The real tokenizer, or None. Loaded once; ~0.1s and ~30 MB resident."""
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None
    path = tokenizer_path()
    if not path.exists():
        return None
    try:
        return Tokenizer.from_file(str(path))
    except Exception:
        return None


def is_exact() -> bool:
    """True when counts come from DeepSeek's tokenizer rather than the fallback."""
    return _tokenizer() is not None


def count(text: str) -> int:
    """Tokens in ``text``, as DeepSeek would count them.

    Excludes the chat template's own framing (system role markers and so on), so a
    real ``prompt_tokens`` runs a little higher than this — tens of tokens per call,
    against thousands. Good enough to quote a price; not a substitute for the usage
    the API reports back, which is what the ledger records.
    """
    if not text:
        return 0
    tokenizer = _tokenizer()
    if tokenizer is None:
        return max(1, int(len(text) / _CHARS_PER_TOKEN))
    return len(tokenizer.encode(text, add_special_tokens=False).ids)
