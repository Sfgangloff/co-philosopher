"""Tests for the critical-review feature: comment-syntax selection, the
strip / annotate round-trip and its idempotency, the runner (mocked Claude),
and the CLI surface (including --clear and --dry-run).

No network or API calls — Claude is faked the same way the extraction and
biblio tests fake it.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from cophilo.cli import app
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.extract.claude import ExtractionResult
from cophilo.review import clear_review_comments, review_file
from cophilo.review.annotate import (
    SENTINEL,
    annotate,
    clean_source,
    numbered_source,
    strip_lines,
    syntax_for,
)
from cophilo.review.schemas import FileReview, ReviewComment


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COPHILO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPHILO_DB_PATH", str(data_dir / "db" / "cophilo.sqlite"))
    get_config.cache_clear()
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)
    yield cfg
    get_config.cache_clear()


# --- comment syntax ------------------------------------------------------


def test_syntax_for_picks_per_suffix():
    assert syntax_for(".tex").probe.startswith("%")
    assert syntax_for(".md").probe.startswith("<!--")
    assert syntax_for(".py").probe.startswith("#")
    # unknown → loud plain-text marker
    assert syntax_for(".rst").probe.startswith(">>>")
    assert syntax_for(".TEX") is syntax_for(".tex")  # case-insensitive


def test_markdown_comment_lines_are_self_closing_html():
    s = syntax_for(".md")
    line = s.line("hello")
    assert line.startswith("<!--") and line.endswith("-->")
    assert s.is_ours(line)
    assert not s.is_ours("a normal paragraph of prose")


# --- strip / annotate round-trip ----------------------------------------


def _review() -> FileReview:
    return FileReview(
        summary="A promising sketch whose central premise is asserted, not argued.",
        comments=[
            ReviewComment(line=1, kind="clarity", comment="The title overclaims."),
            ReviewComment(line=3, kind="weakness", comment="This premise needs an argument."),
            ReviewComment(line=0, kind="question", comment="What grounds the modal claim?"),
            ReviewComment(line=999, kind="suggestion", comment="Out-of-range → folded in."),
        ],
    )


def test_annotate_inserts_before_lines_and_keeps_originals():
    src = "First line\nSecond line\nThird line\n"
    syntax = syntax_for(".tex")
    out = annotate(src, _review(), syntax)

    lines = out.splitlines()
    # every review line carries the sentinel and is a LaTeX comment
    review_lines = [ln for ln in lines if syntax.is_ours(ln)]
    assert review_lines and all(SENTINEL in ln and ln.lstrip().startswith("%") for ln in review_lines)

    # original lines are present and unmodified, in order
    assert [ln for ln in lines if not syntax.is_ours(ln) and ln] == [
        "First line",
        "Second line",
        "Third line",
    ]
    # comment for line 3 sits immediately before "Third line"
    idx = lines.index("Third line")
    assert syntax.is_ours(lines[idx - 1]) and "premise needs an argument" in lines[idx - 1]
    # general (line 0) + out-of-range comments live in the banner, before body
    assert "modal claim" in out and "folded in" in out
    assert out.index("modal claim") < out.index("First line")
    assert out.endswith("\n")  # trailing newline preserved


def test_annotate_is_idempotent_and_reversible():
    src = "alpha\nbeta\ngamma\n"
    syntax = syntax_for(".md")
    once = annotate(src, _review(), syntax)
    twice = annotate(once, _review(), syntax)  # re-review its own output
    assert once == twice  # no stacking
    # stripping restores the exact original
    restored = "\n".join(strip_lines(once.splitlines(), syntax)) + "\n"
    assert restored == src


def test_annotate_preserves_crlf_line_endings():
    crlf = "alpha\r\nbeta\r\n"
    syntax = syntax_for(".tex")
    out = annotate(crlf, _review(), syntax)
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")  # no bare \n
    restored = "\r\n".join(strip_lines(out.splitlines(), syntax)) + "\r\n"
    assert restored == crlf  # byte-for-byte, CRLF intact


def test_numbered_source_and_clean_source():
    assert numbered_source("a\nb") == "   1│a\n   2│b"
    syntax = syntax_for(".tex")
    annotated = annotate("x\ny\n", _review(), syntax)
    assert clean_source(annotated, syntax) == "x\ny"  # model never sees its own marks


def test_annotate_reanchors_when_line_drifts():
    # The model anchored a remark to line 2 via a quote; the user then
    # inserted a paragraph, so line 2 is now unrelated text.
    review = FileReview(
        summary="ok",
        comments=[
            ReviewComment(
                line=2,
                kind="weakness",
                comment="Begs the question.",
                anchor="the will is free",
            )
        ],
    )
    edited = "Intro added later.\nA brand new sentence.\nClearly the will is free here.\n"
    out = annotate(edited, review, syntax_for(".md"))
    lines = out.splitlines()
    target = lines.index("Clearly the will is free here.")
    # the remark followed its quote to line 3, not the stale line 2
    assert "Begs the question" in lines[target - 1]
    assert "Begs the question" not in lines[lines.index("A brand new sentence.") - 1]


def test_annotate_only_filters_kinds_but_keeps_summary():
    src = "First line\nSecond line\nThird line\n"
    out = annotate(src, _review(), syntax_for(".tex"), only={"weakness"})
    assert "premise needs an argument" in out  # the weakness survived
    assert "title overclaims" not in out  # clarity filtered out
    assert "central premise is asserted" in out  # summary always kept


def test_tally_phrase_and_sidecar(isolated_data_dir, tmp_path):
    from cophilo.review.annotate import sidecar_markdown, tally_phrase
    from cophilo.review.runner import sidecar_path

    assert tally_phrase(_review()) == "1 weakness, 1 question, 1 suggestion, 1 clarity"
    assert tally_phrase(_review(), only={"weakness"}) == "1 weakness"

    md = sidecar_markdown("essay.tex", _review())
    assert md.startswith("# Review of `essay.tex`")
    assert "central premise is asserted" in md  # summary
    assert "[WEAKNESS] line 3" in md

    f = tmp_path / "essay.tex"
    original = "\\section{X}\nDeterminism is false.\n"
    f.write_text(original, encoding="utf-8")
    result = review_file(
        isolated_data_dir, f, client=FakeClient(_review()), sidecar=True
    )
    assert result.sidecar and result.written
    assert f.read_text(encoding="utf-8") == original  # source untouched
    side = sidecar_path(f)
    assert side.name == "essay.tex.review.md" and side.exists()
    assert SENTINEL in side.read_text(encoding="utf-8")


# --- runner (mocked Claude) ---------------------------------------------


class FakeClient:
    def __init__(self, parsed: Any):
        self.parsed = parsed
        self.calls: list[dict] = []

    def call(self, *, model, system, user, response_model, max_tokens):
        self.calls.append({"model": model, "system": system, "user": user})
        return ExtractionResult(
            parsed=self.parsed,
            cache_read_tokens=5,
            cache_write_tokens=6,
            input_tokens=7,
            output_tokens=8,
        )


def test_review_file_writes_in_place(isolated_data_dir, tmp_path):
    cfg = isolated_data_dir
    f = tmp_path / "essay.tex"
    f.write_text("\\section{Free will}\nDeterminism is false.\n", encoding="utf-8")
    fake = FakeClient(_review())

    result = review_file(cfg, f, client=fake, write=True)

    assert result.written and result.comment_count == 4
    assert result.input_tokens == 7 and result.cache_write_tokens == 6
    assert fake.calls[0]["model"] == cfg.claude_model_hard
    assert "essay.tex" in fake.calls[0]["system"]
    assert "1│" in fake.calls[0]["user"]  # numbered source was sent
    on_disk = f.read_text(encoding="utf-8")
    assert SENTINEL in on_disk and "\\section{Free will}" in on_disk


def test_review_file_dry_run_does_not_write(isolated_data_dir, tmp_path):
    cfg = isolated_data_dir
    f = tmp_path / "note.md"
    original = "# Title\n\nA claim with no support.\n"
    f.write_text(original, encoding="utf-8")

    result = review_file(cfg, f, client=FakeClient(_review()), write=False)
    assert not result.written
    assert f.read_text(encoding="utf-8") == original  # untouched
    assert SENTINEL in result.annotated_text


def test_review_file_rejects_empty(isolated_data_dir, tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        review_file(isolated_data_dir, f, client=FakeClient(_review()))


def test_review_consumes_outline_open_questions(isolated_data_dir, tmp_path):
    """§2.1 — when reviewing a file inside a drafts/ folder, a sibling
    OUTLINE.md's `## Open questions` bullets must be threaded into the
    review prompt so the model can report coverage."""
    import frontmatter as fm
    cfg = isolated_data_dir
    draft_dir = cfg.corpus_drafts_dir / "forgetting-as-constitutive"
    draft_dir.mkdir(parents=True)
    f = draft_dir / "article.tex"
    f.write_text("\\section{X}\nA claim.\n", encoding="utf-8")

    outline = fm.Post(
        (
            "## Thesis\n\nForgetting is a condition of thought.\n\n"
            "## Outline\n\n1. Intro\n\n"
            "## Open questions\n\n"
            "- What distinguishes principled forgetting from lossy degradation?\n"
            "- Does the extended-mind thesis inherit the Funes problem?\n"
            "- Where does trauma testimony fit?\n"
        ),
        title="Forgetting Well", kind="draft",
    )
    (draft_dir / "OUTLINE.md").write_text(fm.dumps(outline) + "\n", encoding="utf-8")

    fake = FakeClient(_review())
    result = review_file(cfg, f, client=fake, write=False)
    system = fake.calls[0]["system"]
    # Three open-questions bullets reach the prompt:
    assert "principled forgetting from lossy degradation" in system
    assert "extended-mind thesis inherit the Funes problem" in system
    assert "trauma testimony" in system
    # And the prompt's coverage-instruction stub mentions the contract:
    assert "propose_question_coverage" in system
    # The annotated output renders unchanged for now (model returned no
    # coverage in the fake), but accepting coverage doesn't break the flow:
    assert SENTINEL in result.annotated_text


def test_review_renders_propose_question_coverage(isolated_data_dir, tmp_path):
    """§2.1 — when the model returns propose_question_coverage entries, both
    inline annotation and sidecar markdown surface them."""
    from cophilo.review.annotate import sidecar_markdown
    from cophilo.review.schemas import (
        FileReview as _FR,
        QuestionCoverage,
        ReviewComment,
    )
    review_with_coverage = _FR(
        summary="A draft.",
        comments=[ReviewComment(line=2, kind="weakness", comment="x", anchor="A claim.")],
        propose_question_coverage=[
            QuestionCoverage(
                question="What distinguishes principled forgetting?",
                status="skipped",
                evidence="The criterion is gestured at but not given.",
                evidence_line=2,
            ),
            QuestionCoverage(
                question="Does the extended-mind thesis inherit Funes?",
                status="engaged",
                evidence="Section 4 addresses it directly.",
                evidence_line=2,
            ),
        ],
    )
    src = "\\section{X}\nA claim.\n"
    syntax = syntax_for(".tex")
    out = annotate(src, review_with_coverage, syntax)
    # Inline output contains the trailing PROPOSE-QUESTION COVERAGE block:
    assert "PROPOSE-QUESTION COVERAGE" in out
    assert "[SKIPPED]" in out and "principled forgetting" in out
    assert "[ENGAGED]" in out and "extended-mind thesis inherit Funes" in out
    # Sidecar markdown does too:
    md = sidecar_markdown("article.tex", review_with_coverage)
    assert "## Propose-question coverage" in md
    assert "[SKIPPED]" in md and "[ENGAGED]" in md


def test_review_no_outline_means_no_questions_block(isolated_data_dir, tmp_path):
    """When the reviewed file has no sibling OUTLINE.md, the prompt's
    open-questions section is `(none)` and the model is not asked to
    report coverage."""
    cfg = isolated_data_dir
    f = tmp_path / "standalone.tex"
    f.write_text("\\section{X}\nclaim\n", encoding="utf-8")
    fake = FakeClient(_review())
    review_file(cfg, f, client=fake, write=False)
    system = fake.calls[0]["system"]
    assert "(none)" in system  # the placeholder is what the prompt shows


def test_respond_to_review_round2(isolated_data_dir, tmp_path):
    """§2.2 — given a sidecar review the user replied to (with `> reply:`
    lines), `respond_to_review` parses the exchanges, calls Claude once,
    and writes `<name>.review-r2.md`. The model never sees its own prior
    monologue: only (critique, reply) pairs."""
    from cophilo.review.runner import (
        _parse_sidecar_exchanges,
        respond_to_review,
        round_path,
    )
    from cophilo.review.schemas import CounterReply, CounterRound

    cfg = isolated_data_dir
    article = tmp_path / "article.tex"
    article.write_text("\\section{X}\nA claim.\n", encoding="utf-8")
    sidecar = tmp_path / "article.tex.review.md"
    sidecar.write_text(
        "# Review of `article.tex`\n\n"
        "> A draft.\n\n"
        "## Line-anchored remarks\n\n"
        "- **[WEAKNESS] line 2**  ·  “A claim.”\n"
        "  This premise needs an argument.\n"
        "  > reply: Granted, but my next paragraph addresses it.\n"
        "- **[QUESTION] line 2**  ·  “A claim.”\n"
        "  What grounds the modal claim?\n"
        "  (no reply provided)\n"
        "- **[WEAKNESS] line 0**\n"
        "  The piece overclaims throughout.\n"
        "  > reply: I have toned down the abstract.\n",
        encoding="utf-8",
    )

    # Only the two bullets with `> reply:` lines become exchanges.
    exchanges = _parse_sidecar_exchanges(sidecar.read_text(encoding="utf-8"))
    assert len(exchanges) == 2
    assert "premise needs an argument" in exchanges[0].original_comment
    assert exchanges[0].user_reply == "Granted, but my next paragraph addresses it."
    # The bullet without a reply is skipped:
    assert all("modal claim" not in ex.original_comment for ex in exchanges)

    canned = CounterRound(
        summary="One critique conceded, one sharpened.",
        counters=[
            CounterReply(
                original_comment=exchanges[0].original_comment,
                user_reply=exchanges[0].user_reply,
                counter="If the next paragraph carries the argument, lead with it.",
                verdict="sharpen",
                anchor="A claim.",
            ),
            CounterReply(
                original_comment=exchanges[1].original_comment,
                user_reply=exchanges[1].user_reply,
                counter="Fair — the toned-down abstract resolves the overclaim.",
                verdict="concede",
            ),
        ],
    )
    fake = FakeClient(canned)
    result = respond_to_review(cfg, article, prior=sidecar, client=fake)

    assert result.round_index == 2
    assert result.exchanges_count == 2
    assert result.path == round_path(article.resolve(), 2)
    assert result.path.name == "article.tex.review-r2.md"
    assert result.path.exists()
    written = result.path.read_text(encoding="utf-8")
    assert "round 2" in written
    assert "[SHARPEN]" in written and "[CONCEDE]" in written
    # The model received the exchanges, not just the prior review verbatim:
    system = fake.calls[0]["system"]
    assert "premise needs an argument" in system
    assert "next paragraph addresses it" in system
    # Tokens flow into the result:
    assert result.input_tokens == 7


def test_respond_to_review_rejects_when_no_replies(isolated_data_dir, tmp_path):
    from cophilo.review.runner import respond_to_review
    cfg = isolated_data_dir
    article = tmp_path / "x.tex"
    article.write_text("\\section{X}\nclaim\n", encoding="utf-8")
    sidecar = tmp_path / "x.tex.review.md"
    sidecar.write_text(
        "# Review of `x.tex`\n\n> S.\n\n## Line-anchored remarks\n\n"
        "- **[WEAKNESS] line 2**\n  some critique\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        respond_to_review(cfg, article, prior=sidecar, client=FakeClient(None))
    assert "no '> reply:' lines" in str(excinfo.value)


def test_respond_to_review_caps_at_round_three(isolated_data_dir, tmp_path):
    """Depth cap: after round 3, no more counter-passes."""
    from cophilo.review.runner import respond_to_review
    cfg = isolated_data_dir
    article = tmp_path / "x.tex"
    article.write_text("\\section{X}\nclaim\n", encoding="utf-8")
    # Pretend we are about to spawn round 4 by giving a -r3 sidecar:
    r3 = tmp_path / "x.tex.review-r3.md"
    r3.write_text(
        "# Counter-review of `x.tex` — round 3\n\n> s.\n\n"
        "## Counter-replies\n\n"
        "- **[SHARPEN]**\n  *Original:* o\n  *Reply:* r\n  *Counter:* c\n"
        "  > reply: I disagree.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        respond_to_review(cfg, article, prior=r3, client=FakeClient(None))
    assert "capped at 3" in str(excinfo.value)


def test_clear_review_comments_round_trip(tmp_path):
    f = tmp_path / "doc.tex"
    src = "Line one\nLine two\n"
    f.write_text(src, encoding="utf-8")
    syntax = syntax_for(".tex")
    f.write_text(annotate(src, _review(), syntax), encoding="utf-8")

    assert clear_review_comments(f) is True
    assert f.read_text(encoding="utf-8") == src
    assert clear_review_comments(f) is False  # nothing left to clear


# --- CLI -----------------------------------------------------------------

runner = CliRunner()


def test_cli_review_dry_run(monkeypatch, isolated_data_dir, tmp_path):
    import cophilo.cli as cli_mod
    from cophilo.review.runner import ReviewResult

    f = tmp_path / "draft.tex"
    f.write_text("\\section{X}\nclaim\n", encoding="utf-8")

    def fake_review_file(cfg, path, **kw):
        return ReviewResult(
            path=path,
            review=_review(),
            annotated_text="% cophilo-review | [SUMMARY] ok\nclaim\n",
            language="en",
            written=False,
            comment_count=4,
            input_tokens=1,
            output_tokens=2,
        )

    monkeypatch.setattr(cli_mod, "review_file", fake_review_file)
    res = runner.invoke(app, ["review", str(f), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "cophilo-review" in res.stdout  # annotated text printed
    assert f.read_text(encoding="utf-8") == "\\section{X}\nclaim\n"  # not written


def test_cli_review_clear(isolated_data_dir, tmp_path):
    f = tmp_path / "x.md"
    src = "para one\n"
    f.write_text(annotate(src, _review(), syntax_for(".md")), encoding="utf-8")
    res = runner.invoke(app, ["review", str(f), "--clear"])
    assert res.exit_code == 0, res.output
    assert "Cleared review comments" in res.stdout
    assert f.read_text(encoding="utf-8") == src


def test_cli_review_rejects_bad_lang(isolated_data_dir, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi there\n", encoding="utf-8")
    res = runner.invoke(app, ["review", str(f), "--lang", "de"])
    assert res.exit_code != 0
