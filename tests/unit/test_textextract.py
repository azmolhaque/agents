"""HTML to text.

Feeding raw marketing HTML to a 4B wastes ~5x the prompt tokens on script tags and
utility classes. These tests pin the behaviour the Phase 1 benchmark depends on.
"""

from __future__ import annotations

from cindraleads.textextract import extract_text, extract_title, html_to_text

MESSY = """<!DOCTYPE html><html><head>
<title>Acme Health — AI for clinics</title>
<style>.hero{color:red}</style>
<script>var tracking=1;fbq('init');</script>
</head><body class="bg-slate-900 antialiased">
<nav>Home About Careers</nav>
<h1>Acme Health</h1>
<p>We shipped a patient-facing AI assistant last month.</p>
<div>~35 staff, Dhaka and Singapore.</div>
<noscript>Please enable JavaScript</noscript>
<svg><path d="M0 0"/></svg>
</body></html>"""


def test_script_style_noscript_and_svg_are_dropped():
    text = extract_text(MESSY)
    for noise in ("tracking", "fbq", "color:red", "enable JavaScript", "M0 0"):
        assert noise not in text


def test_prose_survives():
    text = extract_text(MESSY)
    assert "Acme Health" in text
    assert "patient-facing AI assistant" in text
    assert "~35 staff" in text


def test_title_is_extracted_even_though_head_is_dropped():
    """<title> lives inside <head>, which is suppressed. A naive implementation loses
    it, and the title is a strong display_name signal."""
    assert "Acme Health" in extract_title(MESSY)


def test_returns_text_and_title_together():
    text, title = html_to_text(MESSY)
    assert text and title


def test_empty_and_garbage_input_do_not_raise():
    assert extract_text("") == ""
    assert extract_text("   ") == ""
    # Unclosed tags are the norm in the wild; keep whatever was parseable.
    assert "hello" in extract_text("<div><p>hello<span>")


def test_truncation_happens_at_a_word_boundary():
    html = "<p>" + ("alpha beta gamma " * 2000) + "</p>"
    out = extract_text(html, max_chars=200)
    assert len(out) <= 210
    assert out.endswith("…")
    assert not out.rstrip(" …").endswith("alp")


def test_short_documents_are_not_truncated():
    out = extract_text("<p>short and sweet</p>", max_chars=5000)
    assert out == "short and sweet"
    assert "…" not in out


def test_block_tags_become_line_breaks_not_run_together_words():
    """Without block breaks 'AcmeWe shipped' appears as one token and the model reads
    a company name that does not exist."""
    text = extract_text("<h1>Acme</h1><p>We shipped</p>")
    assert "AcmeWe" not in text
