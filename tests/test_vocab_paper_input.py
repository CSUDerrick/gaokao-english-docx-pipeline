"""The vocabulary handout reads a whole paper, not one question at a time.

It used to be asked per selected question, which is the wrong unit: the model saw one
passage in isolation and could not tell whether a word is genuinely hard *for this
paper* — the judgement it is actually being asked to make.

Two things have to hold, and both are the kind that fail silently:

* the answer section must never reach the model. The extracted text is the whole docx,
  answer key and 参考范文 included, and this handout goes to students.
* a chunk boundary must never land inside a sentence. Vocabulary judged on half a
  sentence is vocabulary judged wrong, and nothing downstream would notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gaokao_english_docx_pipeline as pipeline  # noqa: E402
import providers as pv  # noqa: E402

PAPER = """广东省某中学 2026 届高考模拟卷

第二部分 阅读理解
第一节（共 15 小题）

A

Scientists have long known that trees cool the air around them. The process, known as
transpiration, moves water from the soil through the tree and out into the atmosphere.

21. What does the passage mainly discuss?
A. Photosynthesis.  B. Transpiration.  C. Migration.  D. Erosion.

第三部分 语言运用
第一节 完形填空

I still remember the day my grandmother taught me to ride a bicycle. She was
remarkably patient, and never once raised her voice.

41. A. patient  B. angry  C. tired  D. bored
"""

ANSWERS = """
广东省某中学 2026 届高考模拟卷答案
21—23. CBC 24—27 DAAD 28—31 BBAC
41-45 BDBAC

【21题详解】
细节理解题。根据第一段可知……

参考范文
Dear Chris,
I am writing to invite you to our school's annual sports meeting.
Yours,
Li Hua
"""


def test_the_answer_section_never_reaches_the_model():
    """答案绝不能进学生版 (CLAUDE.md), and 参考范文 is not in the students' paper at all."""
    kept = pipeline.trim_answer_tail_from_text(PAPER + ANSWERS)

    assert "transpiration" in kept.lower(), "the passage itself must survive"
    assert "21—23. CBC" not in kept, "the answer key must be gone"
    assert "【21题详解】" not in kept, "the official explanations must be gone"
    assert "参考范文" not in kept
    assert "Dear Chris" not in kept, "a model essay is text the student cannot read"


def test_a_paper_that_fits_is_one_turn():
    """On a 1M-window model, 40% is 336k tokens and a real paper is about 20k.

    So chunking is correct-by-construction and, on every provider we ship, never fires.
    A paper split into parts that did not need splitting would pay for turns it did not
    need and blur the model's view of the paper.
    """
    spec = pv.model_spec("deepseek", "deepseek-v4-pro")
    budget = pv.conversation_budget(spec, "vocab")
    chunks = pipeline.chunk_by_tokens(PAPER, budget)
    assert len(chunks) == 1
    assert chunks[0].strip() == PAPER.strip()


def test_a_paper_too_big_for_the_budget_splits_without_cutting_a_sentence():
    # Force the split: pretend a model with a tiny context window.
    text = "\n\n".join(f"Paragraph {i}. " + "The quick brown fox jumps over the lazy dog. " * 20
                       for i in range(40))
    chunks = pipeline.chunk_by_tokens(text, 4_000)

    assert len(chunks) > 1, "this should have needed splitting"
    assert all(chunk.strip() for chunk in chunks), "no empty chunk"

    # Lossless: every paragraph survives exactly once, in order.
    rejoined = "\n\n".join(chunks)
    assert rejoined.split() == text.split()

    # And no chunk ends mid-sentence.
    for chunk in chunks:
        assert chunk.strip().endswith("."), f"chunk cut mid-sentence: ...{chunk.strip()[-40:]!r}"


def test_an_unsplittable_line_goes_out_whole_rather_than_being_cut():
    """One enormous unbroken passage.

    Splitting it mid-sentence would be worse than one over-budget turn, so it goes out
    intact. Silently truncating the passage is the failure this exists to prevent.
    """
    huge = "This sentence has no paragraph breaks anywhere at all. " * 500
    chunks = pipeline.chunk_by_tokens(huge, 2_000)
    assert "".join(chunks).split() == huge.split(), "nothing may be dropped"


def test_empty_text_produces_no_turns():
    assert pipeline.chunk_by_tokens("", 10_000) == []
    assert pipeline.chunk_by_tokens("   \n\n  ", 10_000) == []


def test_the_paper_prompt_asks_about_the_paper_and_forbids_both_quote_mistakes():
    prompt = pipeline.build_vocab_paper_prompt("卷子.docx", PAPER)
    assert "整份" in prompt or "整卷" in prompt or "通读全文" in prompt

    # 决策 27: saying only "no English double quotes inside strings" made flash apply the
    # rule to the JSON *syntax* and emit {“word”: “abandon”}. Both halves must be stated.
    assert "语法符号" in prompt, "must say the JSON delimiters are half-width"
    assert "字符串" in prompt, "must say the rule about quotes *inside* a string separately"
    assert "错误示例" in prompt, "give it the counter-example it actually got wrong"


# --------------------------------------------------------------------------- 完整（分块）


def test_the_chunked_prompt_sends_one_question_and_never_the_answer_key():
    """完整（分块）: the answer key stays out for free.

    segment_body carries only the question text, which is why this path never needed the
    answer-trimming the whole-paper path does. That is a property worth pinning down: if
    someone ever "helpfully" switched this to the raw segment JSON, the answer key would
    walk straight into a student handout.
    """
    segment = {
        "item_id": "卷__reading_a__01",
        "question_text": "Scientists have long known that trees cool the air around them.",
        "answer_key": [{"number": "21", "answer": "C"}],
    }
    prompt = pipeline.build_vocab_item_prompt(segment)

    assert "trees cool the air" in prompt
    assert "卷__reading_a__01" in prompt
    assert '"answer"' not in prompt and "answer_key" not in prompt, "答案键绝不能进学生词表"

    # Smaller budget than the whole-paper prompt: 18 questions each allowed 40 words is
    # how the old handout got long and repetitive.
    assert str(pipeline.VOCAB_MAX_ITEM_WORDS) in prompt
    assert pipeline.VOCAB_MAX_ITEM_WORDS < pipeline.VOCAB_MAX_READING_WORDS

    # 决策 27 applies to both prompts, not just the new one.
    assert "语法符号" in prompt and "错误示例" in prompt


def test_the_two_modes_cache_into_separate_directories():
    """A half-finished run of one mode must not be silently reused by the other."""
    out = Path("/tmp/x")
    chunked = pipeline.vocab_dir_for(out, pipeline.VOCAB_CHUNKED)
    whole = pipeline.vocab_dir_for(out, pipeline.VOCAB_WHOLE)
    assert chunked != whole


def test_both_modes_are_reachable_from_the_cli_and_whole_is_the_default():
    for mode in pipeline.VOCAB_MODES:
        assert pipeline.parse_args(["x", "--vocab-mode", mode]).vocab_mode == mode
    assert pipeline.parse_args(["x"]).vocab_mode == pipeline.VOCAB_WHOLE


def test_the_truncation_warning_only_fires_for_the_whole_paper_mode():
    """完整（分块）feeds one question at a time, so a reply is a fraction of the size.

    The 38,934-token peak was measured reading a whole paper. Crying wolf about it on the
    per-question path would train the teacher to ignore the one warning that matters.
    """
    import io
    import contextlib

    def warned(mode: str) -> bool:
        args = pipeline.parse_args(["x", "--provider", "zhipu", "--preset", "quality", "--vocab-mode", mode])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pipeline.warn_if_output_ceiling_is_too_low(args, args.vocab_model)
        return "⚠️" in buf.getvalue()

    # GLM caps output at 32k, below the measured peak — the whole-paper mode may truncate.
    assert warned(pipeline.VOCAB_WHOLE), "整卷模式在小输出上限的模型上必须提前警告"
    assert not warned(pipeline.VOCAB_CHUNKED), "分块模式不该报这个警告"


def test_the_merge_prompt_only_exists_for_a_split_paper():
    merge = pipeline.build_vocab_merge_prompt("卷子.docx", 3)
    assert "去重" in merge and "排序" in merge
    assert "reading_words" in merge and "word_forms" in merge


def test_vocab_is_keyed_by_paper_so_reselecting_questions_cannot_stale_it():
    """The point of the whole change, stated as a test.

    Under per-question extraction, swapping one question invalidated the handout and the
    teacher had to pay to regenerate it. The words belong to the paper, and the paper did
    not change.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        (out / "explanations").mkdir(parents=True)
        (out / "selected_items.json").write_text(json.dumps([
            {"item_id": "a", "source_doc": "p1.docx"},
            {"item_id": "swapped-in-later", "source_doc": "p1.docx"},
        ]), encoding="utf-8")
        for item in ("a", "swapped-in-later"):
            (out / "explanations" / f"{item}.json").write_text("{}", encoding="utf-8")
        # The word list was built before the swap, and names only the paper.
        (out / "vocab_index.json").write_text(
            json.dumps([{"source_doc": "p1.docx", "reading_words": [], "word_forms": []}]),
            encoding="utf-8",
        )

        assert pipeline.vocab_papers(out) == ["p1.docx"]
        pipeline.assert_selection_is_complete(out)  # must not raise
