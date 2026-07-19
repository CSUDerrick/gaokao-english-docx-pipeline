"""Pairing a student-edition paper with its separate answers document.

A paper does not always arrive as one self-contained file. Often it is two: the 试卷 the
students got (questions, no answers) and a 答案 document (answers and 详解, but not the
questions). Everything downstream is built around "one docx = one paper", so the two have
to be recognised and matched before segmentation — after which the answers document is
consumed as if it were the paper's own answer section, and the paper is segmented as usual.

This module holds only the *decision rules*, like ``segment_quality`` does: the pipeline
measures (how many questions did the local segmenter find, where does the answer region
start) and passes the numbers in. That keeps the rules importable and testable without a
docx, a network, or the pipeline itself.

The one thing it must never do is guess. Pairing the wrong answers onto a paper prints
B 卷's answers under A 卷's questions — far worse than printing no answers at all — so a
proposal here is only a *candidate*, and the caller confirms it with the model before use.
"""

from __future__ import annotations

import difflib
import re

PAPER = "paper"        # questions, no answers of its own
ANSWERS = "answers"    # answers/详解 only — not a paper, must not be segmented as one
BOTH = "both"          # the normal self-contained paper: questions + a tail answer section
UNKNOWN = "unknown"    # cannot tell; the caller treats it as a paper, as it always did

# Words that mark a file as the answers half. Deliberately narrow: 「答题卡」 is an answer
# *sheet* for students to fill in, not an answer key, so it is not here.
_ANSWER_WORDS = re.compile(r"答案|参考答案|详解|解析|评分标准|评分细则")

# Noise to drop before comparing two names: 「X英语试题.docx」 and 「X英语试题答案.docx」
# must reduce to the same string.
_NOISE = re.compile(r"[（）()【】\[\]{}_\-—–\s·、,，。.]+")
_TRAILING = re.compile(r"(试题卷?|试卷|答案|参考答案|详解|解析|评分标准|评分细则|版)+$")

# Below this, two names are not the same paper by any reading.
PROPOSE_THRESHOLD = 0.55

# An answers document opens with its key; a paper buries it in the tail. Measured on a
# real pair: the 答案 file's key starts at char 40 of 13k (0.3%), the paper's at 63%.
ANSWERS_AT_TOP_RATIO = 0.15
# ...and an answers document has almost no question text. Not *zero*, though: its 参考范文
# sections parse as the two writing questions, which is why the count alone cannot decide.
ANSWERS_MAX_SEGMENTS = 3


def classify(filename: str, question_segments: int, answer_start: int | None,
             text_length: int, first_answer_pos: int | None = None) -> str:
    """What kind of document is this?

    ``question_segments`` is what the local segmenter found, ``answer_start`` where the
    answer *region* begins, and ``first_answer_pos`` where the first answer key
    (「21—23 CDD」) appears — all measured by the caller.

    The deciding signal is where the key sits, not whether questions were found. On a real
    答案 document the segmenter reports two "questions" (its 参考范文), and its answer
    region is detected late (at 听力录音稿, because the key at the top has no header above
    it) — so both of those signals say "paper" about a file that is nothing of the kind.
    What no paper ever does is *open* with its answer key.
    """
    name_says_answers = bool(_ANSWER_WORDS.search(filename))

    answers_at_top = (
        first_answer_pos is not None
        and first_answer_pos <= max(400, text_length * ANSWERS_AT_TOP_RATIO)
    )
    if answers_at_top and question_segments <= ANSWERS_MAX_SEGMENTS:
        return ANSWERS

    if question_segments > 0:
        # A paper either way; the only question is whether it carries its own answers.
        has_answers = answer_start is not None or first_answer_pos is not None
        return BOTH if has_answers else PAPER

    # No questions at all. Either an answers document, or a paper we failed to parse.
    starts_at_top = answer_start is not None and answer_start <= max(200, text_length * 0.1)
    if name_says_answers or starts_at_top:
        return ANSWERS
    return UNKNOWN


def normalize_title(name: str) -> str:
    """Reduce a filename to the paper it names, so the two halves compare equal."""
    stem = re.sub(r"\.(docx|pdf)$", "", name, flags=re.I)
    stem = _ANSWER_WORDS.sub("", stem)
    stem = _NOISE.sub("", stem)
    return _TRAILING.sub("", stem)


def similarity(paper_name: str, answer_name: str) -> float:
    """0..1 on how much two filenames look like the two halves of one paper."""
    left, right = normalize_title(paper_name), normalize_title(answer_name)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def propose_pairs(papers: list[str], answers: list[str]) -> list[tuple[str, str, float]]:
    """Candidate (paper, answers, score) matches, best first, each file used once.

    Greedy over the whole score matrix rather than best-per-answer: with two papers and
    two answer files, taking each answer's favourite independently can hand both to the
    same paper and orphan the other.

    A lone paper and a lone answers document are proposed whatever their names look like
    — the filenames may be nothing alike ("扫描件2.pdf") while the pairing is obvious.
    The model still gets the final say, so a bad guess here costs one cheap call, not a
    wrong answer key.
    """
    if not papers or not answers:
        return []

    scored = sorted(
        ((similarity(p, a), p, a) for p in papers for a in answers),
        key=lambda row: (-row[0], row[1], row[2]),
    )
    if len(papers) == 1 and len(answers) == 1:
        score, paper, answer = scored[0]
        return [(paper, answer, score)]

    pairs: list[tuple[str, str, float]] = []
    used_papers: set[str] = set()
    used_answers: set[str] = set()
    for score, paper, answer in scored:
        if score < PROPOSE_THRESHOLD:
            break
        if paper in used_papers or answer in used_answers:
            continue
        pairs.append((paper, answer, score))
        used_papers.add(paper)
        used_answers.add(answer)
    return pairs
