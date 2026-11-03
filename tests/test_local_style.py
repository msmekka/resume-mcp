"""Tests for a local resume.lua filter in styles/.

styles/ holds personal styling and is not committed, so these skip when it is
absent. They pin the behaviors the default filter is expected to provide; drop
or adapt them if your filter does something different.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

STYLES = Path(__file__).resolve().parent.parent / "styles"
LUA = STYLES / "resume.lua"

pytestmark = [
    pytest.mark.skipif(not LUA.is_file(), reason="no local styles/resume.lua"),
    pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc is needed"),
]


def to_html(md: str) -> str:
    html = subprocess.run(
        ["pandoc", "-f", "markdown+lists_without_preceding_blankline", "-t", "html",
         "--wrap=none", f"--lua-filter={LUA}"],
        input=md, capture_output=True, text=True, check=True).stdout
    return html.replace("\u00a0", " ")


@pytest.mark.parametrize("line, title, dates", [
    ("Acme, Inc. Nov 2006 – Jul 2026", "Acme, Inc.", "Nov 2006 – Jul 2026"),
    ("Principal Engineer, Office of the CTO 2020 – 2024",
     "Principal Engineer, Office of the CTO", "2020 – 2024"),
    ("Example Lab — Visiting Engineer Current", "Example Lab — Visiting Engineer", "Current"),
    ("Acme — Director, Platform (Jan 2024 – Jul 2026)", "Acme — Director, Platform",
     "Jan 2024 – Jul 2026"),
    ("**Director, Platform** — 2020 – 2024", "Director, Platform", "2020 – 2024"),
    ("**Acme, Inc.** — Nov 2006 – Jul 2026", "Acme, Inc.", "Nov 2006 – Jul 2026"),
])
def test_plain_role_lines_split_at_the_dates(line, title, dates):
    md = f"NAME\nTagline\nContact\n\nEXPERIENCE\n\n{line}\n- bullet\n"
    html = to_html(md)
    assert f"<strong>{title}</strong>" in html
    assert f'<span class="dates">{dates}</span>' in html


def test_labels_and_sentences_are_not_role_lines():
    md = ("**Name**\n\n**Tagline**\n\nContact\n\n**Skills**\n\n"
          "**Cloud & Infrastructure:** AWS, GCP, Terraform\n\n"
          "**State University** — B.S. Computer Science, 1997\n\n"
          "Shipped the platform in 2024.\n")
    html = to_html(md)
    assert 'class="dates"' not in html
    assert "<h2" in html and "Skills" in html
