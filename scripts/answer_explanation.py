#!/usr/bin/env python3
"""Locate the paper's own per-question explanation blocks in the answer section.

Every one of these papers ships an answer section laid out per passage::

    B
    24—27. DBDA
    【导语】本文主要讲述崔雅梦创办无障碍民宿……
    【24题详解】
    细节理解题。根据第二段“Her travels with……”可知……
    【25题详解】
    ……

The teacher edition wants that text under each question, cloned with its original
typesetting — so what the caller needs back is a *body-child index range* it can
hand to :func:`docx_splice.clone_subset`, not a string.

Why this walks blocks and not characters
----------------------------------------
The obvious implementation slices the flat text between two char offsets and maps
the span with :meth:`DocxDoc.body_range`. It does not work. ``body_range``
includes any block that *overlaps* the span, and the natural boundaries here
(the next group's answer run) live in the same paragraph as the previous group's
last sentence — so neighbouring groups came out sharing a block, and a passage's
range ran on into the model essays when the paper simply stopped explaining.

Walking the blocks and asking "which question does this paragraph explain?"
sidesteps the boundary arithmetic entirely: every paragraph belongs to exactly
one question, or to none.

Two defects in the sample papers this has to survive, and does:

* 广东's answer section explains only 21–23 and 32–40. The four sections it skips
  must come back empty, not come back with a neighbour's text.
* 湖北 has a typo — ``【14题详解】`` where 34 belongs. The block still sits inside
  reading_d's contiguous run, so it rides along with the clone (which is right,
  it *is* question 34's explanation) without ever being credited to another item.
"""

from __future__ import annotations

import re

from docx_blocks import DocxDoc

# Which questions each section owns. The paper's own numbering, not ours.
SECTION_QUESTION_RANGES: dict[str, tuple[int, int]] = {
    "reading_a": (21, 23),
    "reading_b": (24, 27),
    "reading_c": (28, 31),
    "reading_d": (32, 35),
    "gap_filling": (36, 40),
    "cloze": (41, 55),
    "grammar": (56, 65),
}

_EXPLANATION_HEAD = re.compile(r"^【(\d+)题详解】")
_GUIDE = re.compile(r"^【导语】")

# A compact answer run ("21—23. ABD") or a grammar answer list ("56. involved 57. a").
# Both introduce the *next* group, so they end the current one.
_ANSWER_RUN = re.compile(
    r"^\s*\d{1,2}\s*[-—~－–]+\s*\d{1,2}\b"
    r"|^\s*\d{1,2}\s*[.．]\s*\S+\s+\d{1,2}\s*[.．]"
)

# Structural lines that belong to no question: part/section headings, the bare
# letter above each reading passage, and the start of the model essays and the
# listening transcript — everything past which the answer section stops
# explaining and starts reproducing.
_NOT_AN_EXPLANATION = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十]+[部节]"
    r"|[A-G]\s*$"
    r"|One possible version"
    r"|Possible version"
    r"|【?参考范文"
    r"|听力(?:原文|录音稿|录音材料|录音原文)"
    r"|评分标准"
    r"|Dear\b"
    r")"
)

_CJK = re.compile(r"[一-鿿]")

ALL_EXPLAINED_QUESTIONS = {n for lo, hi in SECTION_QUESTION_RANGES.values() for n in range(lo, hi + 1)}


def _attach(doc: DocxDoc, first_block: int) -> tuple[dict[int, int], dict[int, int]]:
    """Tag each answer-section block with the question number it explains.

    Returns ``(owner, guides)``: ``owner`` maps body index -> question number,
    ``guides`` maps question number -> the body index of the 【导语】 that
    introduces the passage it belongs to.
    """
    owner: dict[int, int] = {}
    guides: dict[int, int] = {}
    current: int | None = None
    pending_guide: int | None = None

    for block in doc.blocks:
        if block.body_index < first_block:
            continue
        text = block.text.strip()
        if not text:
            continue

        if _GUIDE.match(text):
            pending_guide = block.body_index
            current = None
            continue

        head = _EXPLANATION_HEAD.match(text)
        if head:
            current = int(head.group(1))
            owner[block.body_index] = current
            if pending_guide is not None:
                guides.setdefault(current, pending_guide)
                pending_guide = None
            continue

        if _ANSWER_RUN.match(text) or _NOT_AN_EXPLANATION.match(text):
            current = None
            pending_guide = None
            continue

        # An explanation is always written in Chinese. A run of English is the
        # model essay, which follows the last 详解 with no heading to separate
        # them — without this the whole essay is credited to question 65.
        if current is not None and _CJK.search(text):
            owner[block.body_index] = current
        else:
            current = None

    return owner, guides


class OfficialExplanations:
    """The answer section of one paper, indexed by question number."""

    def __init__(self, doc: DocxDoc, answer_start_char: int | None):
        self.doc = doc
        self.strays: list[int] = []
        if answer_start_char is None:
            self.owner: dict[int, int] = {}
            self.guides: dict[int, int] = {}
            return
        first_block = doc.body_range(answer_start_char, len(doc.text))[0]
        self.owner, self.guides = _attach(doc, first_block)
        self.strays = sorted({n for n in self.owner.values() if n not in ALL_EXPLAINED_QUESTIONS})

    def blocks_for(self, numbers: list[int]) -> list[int]:
        """Body-index range ``[lo, hi)`` holding the explanations for ``numbers``.

        Empty when the paper explains none of them. The range is contiguous, so
        a stray block inside it (a mistyped question number) rides along rather
        than leaving a hole — but a block belonging to a *different* section's
        questions makes the whole range unusable, and we return nothing at all.
        Shipping a plausible-looking explanation of the wrong question is worse
        than shipping none.
        """
        wanted = set(numbers)
        if not wanted:
            return []
        mine = sorted(index for index, number in self.owner.items() if number in wanted)
        if not mine:
            return []

        lead = [self.guides[n] for n in wanted if n in self.guides]
        lo = min(mine + lead)
        hi = max(mine)

        others = ALL_EXPLAINED_QUESTIONS - wanted
        for index in range(lo, hi + 1):
            if self.owner.get(index) in others:
                return []
        return [lo, hi + 1]

    def text_for(self, numbers: list[int]) -> str:
        """The same span as plain text, for feeding the model."""
        span = self.blocks_for(numbers)
        if not span:
            return ""
        lo, hi = span
        return "\n".join(
            block.text.strip()
            for block in self.doc.blocks
            if lo <= block.body_index < hi and block.text.strip()
        )


def question_numbers(section: str, answer_key: object) -> list[int]:
    """The questions an item covers.

    The section's range wins over the answer key, which is only a derivative of it
    and is not always parsed cleanly: 江苏's grammar answers run together as
    ``"58. 59. to"``, so the key lists no question 59 at all. Trusting the key
    there left 59's explanation inside the span but outside the asked-for set,
    which read to :meth:`OfficialExplanations.blocks_for` as another item's text
    and threw the whole (correct) range away.

    The key is only consulted for a section we have no range for.
    """
    span = SECTION_QUESTION_RANGES.get(section)
    if span:
        return list(range(span[0], span[1] + 1))

    numbers: list[int] = []
    if isinstance(answer_key, list):
        for entry in answer_key:
            if isinstance(entry, dict):
                try:
                    numbers.append(int(str(entry.get("number", "")).strip()))
                except ValueError:
                    continue
    return sorted(set(numbers))
