"""Index safety and the draft -> promote -> redo -> PDF workflow."""
import argparse
import json
import shutil
import threading
from pathlib import Path

import pytest

from resume_mcp import server as s

DRAFT = """<!-- Generated: 2026-11-02 | Tailored for: EM GRC at Acme -->

**Pat Example**

**Engineering Leader | Platform**

Somewhere, NC | [github.com/pat](https://github.com/pat)

**Experience**

**Acme | 2020 – 2026**

**Director, Platform** *2024 – 2026*

* Led the platform team.

**Technical Fluency**

**Cloud & Infrastructure:** AWS, GCP, Terraform, and a long list that must wrap rather than run off the page
"""


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    data = tmp_path / "data"
    s.configure(data)
    output = tmp_path / "base-resumes"
    #a stylesheet alone, no filter: the minimum a custom style needs
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "resume.css").write_text("body { font-family: sans-serif; }")
    curator = {"output_dir": str(output), "pdf_dir": str(tmp_path / "pdfs"),
               "style_dir": str(styles)}
    (data / "curator.json").write_text(json.dumps(curator))
    (output / "jobs").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    return data, output / "jobs"


def write_draft(jobs: Path, name: str, text: str = DRAFT) -> Path:
    path = jobs / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def ns(**kw):
    kw.setdefault("data_dir", None)
    kw.setdefault("no_pdf", True)
    return argparse.Namespace(**kw)


def run(cmd, data, **kw):
    #commands reconfigure from --data-dir, so point them at the fixture
    return cmd(ns(data_dir=str(data), **kw))


#--------INDEX SAFETY-------------
def test_reconcile_restores_entries_missing_from_index(corpus):
    data, _ = corpus
    for stem in ("A", "B", "C"):
        (s.SOURCE_DIR / f"{stem}.pdf").write_bytes(b"%PDF")
        (s.CORPUS_DIR / f"{stem}.md").write_text(stem)  #newer than the source
    s.save_index({"A": "A.md"})
    s.reconcile_corpus()
    assert s.load_index() == {"A": "A.md", "B": "B.md", "C": "C.md"}


def test_unreadable_index_is_never_overwritten(corpus):
    s.CONFIG_FILE.write_text('{"a": "a.md", "b')
    assert s.update_config_index("X", "X.md") is False
    assert s.CONFIG_FILE.read_text() == '{"a": "a.md", "b'


def test_concurrent_updates_lose_nothing(corpus):
    s.save_index({})
    threads = [threading.Thread(target=s.update_config_index, args=(f"k{i}", "x.md"))
               for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s.load_index()) == 100


#--------MATCHING-------------
def test_words_from_a_name_find_the_draft(corpus):
    _, jobs = corpus
    target = write_draft(jobs, "2026-11-02-acme-engineering-manager-grc")
    write_draft(jobs, "2026-11-02-acme-staff-engineer")
    assert s.match_drafts("acme grc") == [target]
    assert len(s.match_drafts("acme")) == 2


#--------PROMOTE-------------
def test_promote_records_provenance_and_indexes(corpus):
    _, jobs = corpus
    draft = write_draft(jobs, "d1")
    assert s.promote_generated(draft, "Pat_GRC").startswith("Promoted")
    meta, body = s.split_provenance((s.CORPUS_DIR / "Pat_GRC.md").read_text())
    assert meta["source"] == draft.resolve()
    assert body == DRAFT
    assert s.load_index()["Pat_GRC"] == "Pat_GRC.md"


def test_promote_refuses_existing_and_source_backed_names(corpus):
    _, jobs = corpus
    draft = write_draft(jobs, "d1")
    s.promote_generated(draft, "Pat_GRC")
    assert "redo Pat_GRC" in s.promote_generated(draft, "Pat_GRC")
    (s.SOURCE_DIR / "Pat_Orig.pdf").write_bytes(b"%PDF")
    assert s.promote_generated(draft, "Pat_Orig").startswith("Refused")


def test_cmd_promote_asks_for_a_name_and_retries_on_conflict(corpus, monkeypatch):
    data, jobs = corpus
    (s.SOURCE_DIR / "Taken.pdf").write_bytes(b"%PDF")
    write_draft(jobs, "2026-11-02-acme-grc")
    answers(monkeypatch, "Taken", "Pat_GRC", "y")
    assert run(s.cmd_promote, data, name="acme grc", as_name=None) == 0
    assert "Pat_GRC" in s.load_index()


def test_commands_refuse_without_a_terminal(corpus, monkeypatch):
    data, jobs = corpus
    draft = write_draft(jobs, "d1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert run(s.cmd_promote, data, name="d1", as_name="Pat") == 2
    assert "Pat" not in s.load_index()

    s.promote_generated(draft, "Pat")
    draft.write_text(DRAFT + "\n* New bullet.\n")
    assert run(s.cmd_redo, data, name="Pat", draft=None) == 2
    assert "New bullet" not in (s.CORPUS_DIR / "Pat.md").read_text()


#--------REDO-------------
def test_redo_uses_original_draft_and_keeps_history(corpus, monkeypatch):
    data, jobs = corpus
    draft = write_draft(jobs, "d1")
    s.promote_generated(draft, "Pat")
    draft.write_text(DRAFT + "\n* New bullet.\n")
    answers(monkeypatch, "y")
    assert run(s.cmd_redo, data, name="Pat", draft=None) == 0
    assert "New bullet" in (s.CORPUS_DIR / "Pat.md").read_text()
    history = list((data / s.HISTORY_DIR_NAME).iterdir())
    assert len(history) == 1 and "New bullet" not in history[0].read_text()
    assert s.load_index()["Pat"] == "Pat.md"


def test_redo_with_a_different_draft(corpus, monkeypatch):
    data, jobs = corpus
    s.promote_generated(write_draft(jobs, "v1"), "Pat")
    v2 = write_draft(jobs, "v2", DRAFT.replace("Led the platform team.", "Led it, v2."))
    answers(monkeypatch, "y")
    assert run(s.cmd_redo, data, name="Pat", draft="v2") == 0
    meta, body = s.split_provenance((s.CORPUS_DIR / "Pat.md").read_text())
    assert meta["source"] == v2.resolve() and "v2" in body
    assert body.count("<!-- Promoted") == 0


def test_redo_declined_changes_nothing(corpus, monkeypatch):
    data, jobs = corpus
    draft = write_draft(jobs, "d1")
    s.promote_generated(draft, "Pat")
    before = (s.CORPUS_DIR / "Pat.md").read_text()
    draft.write_text(DRAFT + "\n* New bullet.\n")
    answers(monkeypatch, "n")
    run(s.cmd_redo, data, name="Pat", draft=None)
    assert (s.CORPUS_DIR / "Pat.md").read_text() == before


def test_redo_refuses_resumes_converted_from_source(corpus):
    _, jobs = corpus
    (s.SOURCE_DIR / "Orig.pdf").write_bytes(b"%PDF")
    s.update_config_index("Orig", "Orig.md")
    assert s.redo_promoted("Orig", write_draft(jobs, "d1")).startswith("Refused")


#--------REVIEW-------------
def test_review_flags_drafts_edited_since_promotion(corpus, capsys):
    data, jobs = corpus
    draft = write_draft(jobs, "d1")
    s.promote_generated(draft, "Pat")
    run(s.cmd_review, data)
    assert "in corpus as Pat (" in capsys.readouterr().out
    draft.write_text(DRAFT + "\n* New bullet.\n")
    run(s.cmd_review, data)
    assert "resume-mcp redo Pat" in capsys.readouterr().out


#--------RENDER-------------
needs_pandoc = pytest.mark.skipif(
    not (shutil.which("pandoc") and shutil.which("weasyprint")),
    reason="pandoc and weasyprint are needed to render")


@needs_pandoc
def test_render_writes_styled_pdf_to_pdf_dir_only(corpus):
    pypdfium2 = pytest.importorskip("pypdfium2")
    data, jobs = corpus
    draft = write_draft(jobs, "d1")
    s.promote_generated(draft, "Pat")
    assert run(s.cmd_render, data, name="Pat") == 0

    pdf = s.resolve_pdf_dir() / "Pat.pdf"
    assert pdf.is_file()
    assert not list(jobs.glob("*.pdf")) and not list(s.CORPUS_DIR.glob("*.pdf"))

    page = pypdfium2.PdfDocument(str(pdf))[0]
    text = page.get_textpage().get_text_range()
    assert "Experience" in text and "Pat Example" in text
    assert "Promoted" not in text
    #nothing may start left of the page margin (the old overflow bug)
    tp = page.get_textpage()
    lefts = [tp.get_charbox(i)[0] for i in range(tp.count_chars())
             if not text[i].isspace()]
    assert min(lefts) > 30


def test_render_reports_missing_styles(corpus, capsys):
    data, jobs = corpus
    cfg = json.loads((data / "curator.json").read_text())
    cfg["style_dir"] = str(data / "nope")
    (data / "curator.json").write_text(json.dumps(cfg))
    write_draft(jobs, "d1")
    assert run(s.cmd_render, data, name="d1") == 1
    assert "resume.css" in capsys.readouterr().err


PLAIN_DRAFT = """<!-- Generated: 2026-11-02 | Tailored for: EM at Acme -->

PAT EXAMPLE
Engineering Leader | Platform
Somewhere, NC | github.com/pat

SUMMARY
Engineering leader who builds platforms.

EXPERIENCE

Acme, Inc. Nov 2006 – Jul 2026

Director of Platform Engineering 2024 – 2026
- Led the platform team.
- Shipped the thing.
"""


@needs_pandoc
def test_render_handles_plain_style_drafts(corpus):
    pypdfium2 = pytest.importorskip("pypdfium2")
    data, jobs = corpus
    write_draft(jobs, "plain", PLAIN_DRAFT)
    assert run(s.cmd_render, data, name="plain") == 0
    pdf = pypdfium2.PdfDocument(str(s.resolve_pdf_dir() / "plain.pdf"))
    text = pdf[0].get_textpage().get_text_range()
    assert "PAT EXAMPLE" in text and "Led the platform team." in text
    assert "- Led" not in text  #bullets became a list, not a run-on paragraph


def test_render_prefers_the_promoted_copy(corpus, capsys):
    data, jobs = corpus
    s.promote_generated(write_draft(jobs, "2026-11-02-acme-grc"), "Pat_Acme_GRC")
    write_draft(jobs, "2026-11-02-acme-grc-v2")
    monkey_render = []
    s_render = s._render_and_report
    try:
        s._render_and_report = lambda path, name: monkey_render.append(name) or True
        run(s.cmd_render, data, name="acme grc")
        run(s.cmd_render, data, name="acme grc v2")
    finally:
        s._render_and_report = s_render
    assert monkey_render == ["Pat_Acme_GRC", "2026-11-02-acme-grc-v2"]
