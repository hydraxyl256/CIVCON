"""
HTML sanitisation for user-authored content.

Background
----------
CIV-CON's article surface renders TipTap-produced HTML via
`dangerouslySetInnerHTML` in `frontend/src/pages/ArticleDetails.tsx`.
The backend persists article content verbatim — the existing
`sanitize_text` helper only truncates length and strips control chars,
which means a `<script>` payload survives the round-trip and executes
in every reader's browser (security finding F-001).

This module is the server-side gate that closes that path. We use
`nh3` (the Rust-backed modern successor to Bleach) with a tight
allowlist matching the TipTap default schema. The allowlist is
intentionally narrower than the TipTap defaults — anything outside
this list is dropped, including:

  - `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`
  - All `on*` event-handler attributes
  - `javascript:`, `data:`, `vbscript:` URL schemes
  - `style` attributes (CSP already restricts inline styles at the
    rendering layer, but we strip here too as defence in depth)
  - External image hosts other than Cloudinary
  - `target="_blank"` without `rel="noopener noreferrer"`
"""
from __future__ import annotations

import re

import nh3

# Tags allowed after sanitisation. Matches the TipTap StarterKit +
# Link + Image schema. Anything not listed is dropped.
_ALLOWED_TAGS: frozenset[str] = frozenset({
    # Block / structural
    "p", "br", "hr", "div", "span", "blockquote", "pre",
    # Headings
    "h1", "h2", "h3", "h4", "h5", "h6",
    # Lists
    "ul", "ol", "li",
    # Inline text
    "strong", "b", "em", "i", "u", "s", "code", "kbd", "sub", "sup",
    # Links + media
    "a", "img",
    # Tables (TipTap Table extension)
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    # Highlight (search highlight etc.)
    "mark",
})

# Attributes allowed on each tag. `"*"` is the catch-all entry.
_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "*": frozenset({"class", "id"}),
    "a": frozenset({"href", "title", "rel", "target"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "td": frozenset({"colspan", "rowspan"}),
}

# URL schemes permitted on `<a href>` and `<img src>`.
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# Image source hosts we trust. Cloudinary is the only first-party
# image host for article content.
_ALLOWED_IMAGE_HOSTS: tuple[str, ...] = ("res.cloudinary.com",)

# Block `target="_blank"` without a `rel=` attribute. Implemented as
# a regex post-pass because nh3 cannot enforce cross-attribute
# invariants.
_BLANK_TARGET_RE = re.compile(r"<a\b([^>]*)>", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_TARGET_RE = re.compile(r"""target\s*=\s*["']_blank["']""", re.IGNORECASE)
_REL_RE = re.compile(r"""\brel\s*=""", re.IGNORECASE)


def _is_safe_image_src(value: str | None) -> bool:
    """Permit only http(s) URLs on the Cloudinary host."""
    if not value:
        return False
    lower = value.lower().strip()
    if lower.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return False
    if not lower.startswith(("http://", "https://")):
        return False
    return any(host in lower for host in _ALLOWED_IMAGE_HOSTS)


def _is_safe_link_href(value: str | None) -> bool:
    """Permit http(s), mailto, in-page anchors, and same-origin paths."""
    if not value:
        return False
    lower = value.lower().strip()
    if any(ch in lower for ch in ("\r", "\n", "\t", " ")):
        return False
    if lower.startswith(("javascript:", "data:", "vbscript:", "file:", "about:")):
        return False
    return lower.startswith(("http://", "https://", "mailto:", "/", "#"))


def _force_blank_target_rel(html: str) -> str:
    """Append `rel="noopener noreferrer"` to every `<a target="_blank">`
    that does not already declare a `rel`. Implemented as a regex post-pass
    because nh3 cannot enforce cross-attribute invariants.
    """

    def fix(match: re.Match[str]) -> str:
        attrs = match.group(1)
        if not _TARGET_RE.search(attrs):
            return match.group(0)
        if _REL_RE.search(attrs):
            return match.group(0)
        # Append before the closing `>`.
        return f"<a{attrs} rel=\"noopener noreferrer\">"

    return _BLANK_TARGET_RE.sub(fix, html)


def sanitize_article_html(raw_html: str | None) -> str:
    """Sanitise article HTML for safe rendering.

    Returns an HTML string containing only the tags/attributes/schemes
    configured above. Empty input yields an empty string (NOT `None`,
    so callers can pass the result directly into a Pydantic model).

    The output preserves text content and inline formatting; payloads
    like `<script>alert(1)</script>` are removed while the surrounding
    paragraph survives.

    Cross-attribute invariants (image-host allowlist, link `rel`)
    are enforced in a post-pass after the nh3 clean because nh3
    operates at the attribute level only.
    """
    if not raw_html:
        return ""

    # First pass: nh3 tag + attribute + scheme enforcement.
    cleaned = nh3.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        strip_comments=True,
    )

    # Second pass: strip `<img>` elements whose src is not on the
    # allowlist. We rewrite the tag to a benign placeholder so the
    # surrounding text still flows naturally.
    def rewrite_img(match: re.Match[str]) -> str:
        tag = match.group(0)
        href = _HREF_RE.search(tag)
        if href and _is_safe_image_src(href.group(1)):
            return tag
        return ""

    cleaned = re.sub(r"<img\b[^>]*>", rewrite_img, cleaned, flags=re.IGNORECASE)

    # Third pass: strip `<a>` whose href is on a disallowed scheme.
    def rewrite_a(match: re.Match[str]) -> str:
        tag = match.group(0)
        href = _HREF_RE.search(tag)
        if href and _is_safe_link_href(href.group(1)):
            return tag
        # Drop the `href` but keep the text content so the link text
        # still renders as plain text.
        return re.sub(r"""\s*href\s*=\s*["'][^"']*["']""", "", tag)

    cleaned = re.sub(r"<a\b[^>]*>", rewrite_a, cleaned, flags=re.IGNORECASE)

    # Fourth pass: force `rel="noopener noreferrer"` on every
    # `target="_blank"` link.
    cleaned = _force_blank_target_rel(cleaned)

    return cleaned