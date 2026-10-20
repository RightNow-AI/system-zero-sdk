"""Shared package-name comparison for release and public-export gates."""

import html as _html
import re as _re
import string as _string
import unicodedata as _unicodedata


_INLINE_TAG = _re.compile(
    r"</[A-Za-z][A-Za-z0-9-]*[ \t\r\n]*>"
    r"|<[A-Za-z][A-Za-z0-9-]*"
    r"(?:[ \t\r\n]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t\r\n]*=[ \t\r\n]*(?:[^ \t\r\n\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*"
    r"[ \t\r\n]*/?>"
)
_MARKDOWN_ESCAPE = _re.compile(r"\\([" + _re.escape(_string.punctuation) + r"])")


def normalize_package_name(value: str) -> str:
    """Return NFKC/casefold text with each run of -, _ and . folded to -."""
    return _re.sub(r"[-_.]+", "-", _unicodedata.normalize("NFKC", value).casefold())


def readme_visible_forms(text: str) -> tuple[str, ...]:
    """Return raw README text and one tag/Markdown/entity comparison view."""
    def strip_tag(match: _re.Match[str]) -> str:
        # An escaped opening angle bracket is literal Markdown text. Recognize
        # actual tag syntax, including quoted '>', rather than arbitrary <...>.
        start = match.start()
        cursor = start
        while cursor and text[cursor - 1] == "\\":
            cursor -= 1
        return match[0] if (start - cursor) % 2 else ""

    without_tags = _INLINE_TAG.sub(strip_tag, text)
    unescaped_markdown = _MARKDOWN_ESCAPE.sub(r"\1", without_tags)
    # Decode once and last: entity-encoded tags remain literal visible text.
    return text, _html.unescape(unescaped_markdown)
