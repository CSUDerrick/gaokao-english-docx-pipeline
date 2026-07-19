"""Matching a student-edition paper to its separate answers document.

The rule that matters most here is the one about *not* matching: printing B 卷's answer
key under A 卷's questions is far worse than printing no answers at all. So these pin the
classification and the proposal, and leave the final say to the model.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import answer_pairing as ap  # noqa: E402
import docx_blocks as db  # noqa: E402
import gaokao_english_docx_pipeline as pipeline  # noqa: E402


def test_a_self_contained_paper_is_left_alone():
    # Questions plus an answer section down at the tail: the ordinary paper, which needs
    # no pairing and must not be given someone else's answers.
    assert ap.classify("某某中学英语试题.docx", question_segments=9, answer_start=48000,
                       text_length=52000) == ap.BOTH


def test_a_student_edition_has_questions_and_no_answers():
    assert ap.classify("某某中学英语试题.docx", question_segments=9, answer_start=None,
                       text_length=40000) == ap.PAPER


def test_an_answers_document_is_recognised_and_not_a_paper():
    # No questions at all, and the answer header is at the very top rather than the tail.
    assert ap.classify("某某中学英语答案.docx", question_segments=0, answer_start=0,
                       text_length=8000) == ap.ANSWERS
    # Even a scan with an unhelpful name: the shape gives it away.
    assert ap.classify("扫描件2.docx", question_segments=0, answer_start=12,
                       text_length=8000) == ap.ANSWERS
    # And the name alone is enough when the header regex finds nothing.
    assert ap.classify("参考答案.docx", question_segments=0, answer_start=None,
                       text_length=8000) == ap.ANSWERS


def test_a_real_answers_document_is_not_mistaken_for_a_paper():
    # The numbers are measured off a real 合肥168 答案 document, and every "obvious"
    # signal lies about it:
    #   - the segmenter finds 2 "questions" — they are its 参考范文 sections;
    #   - the answer *region* is detected at 2125, deep inside, because the key at the
    #     top has no header line above it and 听力录音稿 is the first header that matches.
    # Only the key's position gives it away: char 29 of 13015.
    assert ap.classify("合肥168中学英语试题答案.docx", question_segments=2, answer_start=2125,
                       text_length=13015, first_answer_pos=29) == ap.ANSWERS


def test_a_real_self_contained_paper_still_reads_as_one():
    # Same paper before it was split: the key is 63% of the way in, which is where a
    # paper always keeps it.
    assert ap.classify("安徽省合肥168中学英语试题.docx", question_segments=9, answer_start=22290,
                       text_length=35305, first_answer_pos=22319) == ap.BOTH


def test_a_real_student_edition_reads_as_a_paper_with_no_answers():
    assert ap.classify("合肥168中学英语试题.docx", question_segments=9, answer_start=None,
                       text_length=22289, first_answer_pos=None) == ap.PAPER


def test_an_unparseable_paper_is_not_mistaken_for_answers():
    # Zero questions and nothing that looks like an answer region: this is a broken
    # paper, and it must keep going down the path it always did (FAIL -> fallback),
    # not get silently dropped from the run as if it were an answer key.
    assert ap.classify("难切的卷子.docx", question_segments=0, answer_start=None,
                       text_length=40000) == ap.UNKNOWN


def test_the_two_halves_of_one_paper_reduce_to_the_same_name():
    assert ap.normalize_title("湖北省武昌实验中学2026届英语试题.docx") == \
           ap.normalize_title("湖北省武昌实验中学2026届英语试题答案.docx")
    assert ap.similarity("A中学英语试题.docx", "A中学英语试题参考答案.docx") > 0.9
    assert ap.similarity("A中学英语试题.docx", "B联盟二模英语试题答案.docx") < 0.6


def test_a_lone_paper_and_a_lone_answer_file_are_proposed_whatever_they_are_called():
    # A scan is often named nothing like its paper. With one of each the pairing is
    # obvious; the model still gets to veto it, so a bad guess costs one cheap call.
    pairs = ap.propose_pairs(["某某中学英语试题.docx"], ["扫描件2.docx"])
    assert len(pairs) == 1
    assert pairs[0][0] == "某某中学英语试题.docx" and pairs[0][1] == "扫描件2.docx"


def test_two_papers_do_not_both_get_handed_the_same_answers():
    papers = ["A中学英语试题.docx", "B联盟英语试题.docx"]
    answers = ["B联盟英语试题答案.docx", "A中学英语试题答案.docx"]
    pairs = ap.propose_pairs(papers, answers)
    assert sorted((p, a) for p, a, _ in pairs) == [
        ("A中学英语试题.docx", "A中学英语试题答案.docx"),
        ("B联盟英语试题.docx", "B联盟英语试题答案.docx"),
    ], "each paper gets its own answers, and no file is used twice"


def test_names_with_nothing_in_common_are_not_proposed():
    pairs = ap.propose_pairs(
        ["A中学英语试题.docx", "B联盟英语试题.docx"],
        ["完全无关的物理答案.docx"],
    )
    assert pairs == [], "no answers at all beats the wrong answers"


def test_nothing_is_proposed_when_there_is_nothing_to_pair():
    assert ap.propose_pairs([], ["答案.docx"]) == []
    assert ap.propose_pairs(["试题.docx"], []) == []


def _write_docx(path: Path, lines: list[str]) -> Path:
    from docx import Document

    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


def _paired_fixture(root: Path) -> tuple[Path, Path, dict]:
    """A student edition and an answers document, shaped like the real pair."""
    paper = _write_docx(root / "某某中学英语试题.docx", [
        "第四部分 写作",
        "第一节 应用文写作 " + "假定你是李华，你的朋友Jim要来访问。请给他回信说明安排。" * 4,
        "第二节 读后续写 " + "阅读下面短文，根据所给情节进行续写。Li Mei was preparing for a show." * 4,
    ])
    answers = _write_docx(root / "某某中学英语试题答案.docx", [
        "某某中学英语试题",
        "1-5 BABCB 6-10 ACCAC 11-15 BACBB 16-20 CABCB",
        "21-23 CDD 24-27 BDBC 28-31 DABD 32-35 ABCA 36-40 GDAEF",
    ])
    texts = {paper: db.read_docx(paper).text, answers: db.read_docx(answers).text}
    return paper, answers, texts


def _run_pairing(root: Path, verdict: dict) -> dict:
    import argparse

    paper, answers, texts = _paired_fixture(root)
    args = argparse.Namespace(quiet=True, pairing_confirm=True)
    original = pipeline.confirm_pairing
    pipeline.confirm_pairing = lambda *a, **k: verdict
    try:
        pairing, answer_only = pipeline.pair_answer_docs(args, [paper, answers], texts)
    finally:
        pipeline.confirm_pairing = original
    return {"pairing": pairing, "answer_only": answer_only, "paper": paper, "answers": answers}


def test_the_model_confirming_the_match_is_what_pairs_them():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        got = _run_pairing(root, {"ok": True, "reason": "题号范围一致"})
        assert got["pairing"] == {got["paper"]: got["answers"]}
        assert got["answer_only"] == [got["answers"]], "the answers file is never segmented as a paper"


def test_the_model_saying_no_means_no_answers_rather_than_the_wrong_ones():
    # Printing B 卷's key under A 卷's questions is far worse than printing none, so a
    # refusal has to leave the paper answer-less, not fall back on the filename guess.
    with tempfile.TemporaryDirectory() as tmp:
        got = _run_pairing(Path(tmp), {"ok": False, "reason": "题号对不上"})
        assert got["pairing"] == {}
        assert got["answer_only"] == [got["answers"]], "an unmatched answers file is still not a paper"


def test_an_unusable_verdict_is_treated_as_no():
    # The model replied with something we could not parse. That is not a yes.
    with tempfile.TemporaryDirectory() as tmp:
        got = _run_pairing(Path(tmp), {"ok": None, "reason": "???"})
        assert got["pairing"] == {}


def test_the_explanation_is_read_from_the_answers_document_not_the_paper():
    """The 【N题详解】 blocks can live in a different file from the questions.

    Every segment used to carry one ``source_path`` backing both the question blocks and
    the explanation blocks, so "the answers are in the other file" was inexpressible.
    ``official_explanation_path`` says which file the explanation indices point into;
    empty still means "the same one as the questions".
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paper = _write_docx(root / "卷.docx", [
            "21. What does the passage mainly talk about?",
            "A. One  B. Two  C. Three  D. Four",
        ])
        answers = _write_docx(root / "卷答案.docx", [
            "21-23 CDD",
            "【21题详解】",
            "细节理解题。根据第二段可知答案为C。",
        ])
        # Index the explanations exactly the way the segmenter does for a paired document.
        official = pipeline.OfficialExplanations(db.read_docx(answers), 0)
        span = official.blocks_for([21])
        assert len(span) == 2, "the fixture's 详解 must be found in the answers document"

        segment = {
            "section": "reading_a",
            "source_path": str(paper),
            "official_explanation_blocks": span,
            "official_explanation_path": str(answers),
            "answer_key": [{"number": "21", "answer": "C"}],
        }
        assert "细节理解题" in pipeline.official_explanation_text(segment)

        # Without the new field the indices resolve against the paper, which has no
        # 详解 in it at all — the old behaviour, and why the field had to exist.
        segment.pop("official_explanation_path")
        assert "细节理解题" not in pipeline.official_explanation_text(segment)


def test_a_paired_answers_document_is_taken_whole_not_cut_at_its_first_header():
    """The answers document is the answer region start to finish.

    Measured on a real one: its 参考范文 sit *above* 听力录音稿. Running the ordinary tail
    detector over it cut at 听力录音稿 — the first header that matches — and threw the
    essays above it away, so both writing sections came back with no answer at all while
    the very same paper, unsplit, produced them fine.
    """
    essay = ("One possible version Dear Jim, I am delighted to hear that you are coming. "
             "We will visit the museum together and I will show you around the old town. "
             "Please tell me what you would like to see. Yours, Li Hua. ") * 2
    continuation = ("One possible version Paragraph 1: Li Mei devoted herself to preparing "
                    "for the show, her heart pounding with hope and quiet fear all week. "
                    "Paragraph 2: When the curtain rose she smiled at last, and the applause "
                    "told her every late night had been worth it. ") * 2
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paper = _write_docx(root / "卷.docx", [
            "第四部分 写作",
            "第一节 应用文写作 " + "假定你是李华，你的朋友Jim要来访问。请给他回信说明安排。" * 4,
            "第二节 读后续写 " + "阅读下面短文，根据所给情节进行续写。Li Mei was preparing for a show." * 4,
        ])
        answers = _write_docx(root / "卷答案.docx", [
            # Shaped like a real one: a title and a bare answer run, with no 答案 header
            # above them — which is why the detector skips past all of this...
            "某某中学2026届高三英语试题",
            "1-5 BABCB 6-10 ACCAC 11-15 BACBB 16-20 CABCB",
            "21-23 CDD 24-27 BDBC 28-31 DABD 32-35 ABCA 36-40 GDAEF",
            "第一节 应用文写作",
            essay,
            "第二节 读后续写",
            continuation,
            # ...and lands here, BELOW the essays. That is the trap.
            "听力录音稿",
            "Text 1 M: Hi, how is the project going? W: It is fine.",
        ])
        paper_doc, answers_doc = db.read_docx(paper), db.read_docx(answers)
        cut = pipeline._find_answer_section_start(answers_doc.text)
        assert cut is not None and cut > answers_doc.text.find("第一节"), \
            "the fixture must reproduce the trap: the detected header sits below the essays"

        segments = pipeline.local_segment_paper(
            paper.name, paper_doc.text, paper_doc,
            answer_doc=answers_doc, answer_text=answers_doc.text, answer_path=str(answers),
        )

    by_section = {s["section"]: s for s in segments}
    for section in ("practical_writing", "continuation_writing"):
        answer = by_section[section]["answer_key"]
        assert isinstance(answer, str) and "One possible version" in answer, \
            f"{section} lost its 范文 to the tail detector"
        assert by_section[section]["answer_source"] == "答案区/范文"
        # And the explanation blocks must point at the answers file, not the paper.
        assert by_section[section]["official_explanation_path"] == str(answers)
