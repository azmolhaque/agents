"""HTML to readable text.

Brought forward from Phase 3 because the Phase 1 gate needs it: feeding raw marketing
HTML to a 4B model wastes roughly 5x the prompt tokens on script tags and Tailwind
classes, and measurably hurts extraction on a small model. Stripping first is the
difference between a benchmark that reflects the model and one that reflects the noise.

``selectolax`` (4.9 MB, aarch64 wheels) does the job properly and is installed via the
``[extract]`` extra. When it is absent this falls back to a stdlib parser so a bare
Phase 0/1 checkout still runs -- slower and slightly worse, never broken.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["extract_text", "extract_title", "html_to_text", "selectolax_available"]

# Tags whose contents are never prose.
_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head"}
)

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def selectolax_available() -> bool:
    try:
        import selectolax.parser  # noqa: F401
    except ImportError:
        return False
    return True


class _TextHarvester(HTMLParser):
    """stdlib fallback. Deliberately simple: drop non-prose tags, keep block breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title_parts: list[str] = []
        self._suppress = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._suppress += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "div", "section", "li", "tr", "h1", "h2", "h3", "h4", "br"):
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._suppress = max(0, self._suppress - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        # <title> sits inside <head>, and <head> is suppressed. Capture the title
        # before the suppression check or it is always lost.
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._suppress:
            return
        text = data.strip()
        if text:
            self.chunks.append(text + " ")


def _normalize(text: str) -> str:
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def _with_selectolax(html: str) -> tuple[str, str]:
    from selectolax.parser import HTMLParser as LexborParser

    tree = LexborParser(html)
    # Read the title BEFORE decomposing: <title> lives inside <head>, and <head> is in
    # the drop set, so doing this in the other order silently loses it every time.
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body is not None else ""
    return _normalize(text), title


def _with_stdlib(html: str) -> tuple[str, str]:
    harvester = _TextHarvester()
    try:
        harvester.feed(html)
        harvester.close()
    except (AssertionError, ValueError):
        # Malformed markup is normal in the wild; keep whatever we got.
        pass
    return _normalize("".join(harvester.chunks)), " ".join(harvester.title_parts).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(text, title)``. Uses selectolax when installed."""
    if not html.strip():
        return "", ""
    if selectolax_available():
        try:
            return _with_selectolax(html)
        except Exception:
            pass
    return _with_stdlib(html)


def extract_text(html: str, *, max_chars: int = 1500) -> str:
    """Prose only, truncated at a word boundary.

    The cap is the single biggest latency lever measured on the Pi 5. Prompt eval
    runs at roughly 10-35 tok/s there, so page text is expensive to read:

        max_chars=4000 -> median page 150 s, peak 81.2 C
        max_chars=1500 -> median page  64 s, peak 79.6 C, still 100% schema-valid

    1500 is chosen from that measurement, not from taste. It is a *latency* choice
    made against a *schema-validity* gate, so Phase 3 should re-tune it against the
    hand-labelled golden set, where the metric is field accuracy: information that
    lives in a page footer or below the fold is exactly what a short budget drops.
    """
    text, _ = html_to_text(html)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > max_chars * 0.8 else cut).rstrip() + " …"


def extract_title(html: str) -> str:
    return html_to_text(html)[1]
