"""Lightweight tests for the HTML sanitizer.

Run with: `python -m pytest app/core/test_html_sanitizer.py -q`
from `backend/CIVCON/`. Or directly:
    python -m app.core.test_html_sanitizer
"""
from app.core.html_sanitizer import sanitize_article_html


def assert_contains(haystack: str, needle: str) -> None:
    assert needle in haystack, f"expected {needle!r} in {haystack!r}"


def assert_not_contains(haystack: str, needle: str) -> None:
    assert needle not in haystack, f"unexpected {needle!r} in {haystack!r}"


def test_strips_script_tags() -> None:
    out = sanitize_article_html("<p>hi</p><script>alert(1)</script>")
    assert_contains(out, "<p>hi</p>")
    assert_not_contains(out, "<script")
    assert_not_contains(out, "alert(1)")


def test_strips_event_handlers() -> None:
    out = sanitize_article_html('<p><img src="x" onerror="alert(1)"></p>')
    # The img tag should survive, but the onerror attribute is dropped.
    assert_not_contains(out, "onerror")
    assert_not_contains(out, "alert(1)")


def test_strips_javascript_href() -> None:
    out = sanitize_article_html('<p><a href="javascript:alert(1)">x</a></p>')
    assert_not_contains(out, "javascript:")
    assert_not_contains(out, "alert(1)")


def test_strips_iframe() -> None:
    out = sanitize_article_html("<iframe src='https://evil.example'></iframe>")
    assert_not_contains(out, "<iframe")
    assert_not_contains(out, "evil.example")


def test_preserves_safe_markup() -> None:
    out = sanitize_article_html(
        '<p>Hello <strong>world</strong></p>'
        '<ul><li>one</li><li>two</li></ul>'
    )
    assert_contains(out, "<strong>world</strong>")
    assert_contains(out, "<ul>")
    assert_contains(out, "<li>one</li>")


def test_strips_off_host_image() -> None:
    out = sanitize_article_html('<img src="https://evil.example/x.png" alt="x">')
    assert_not_contains(out, "evil.example")


def test_allows_cloudinary_image() -> None:
    out = sanitize_article_html(
        '<img src="https://res.cloudinary.com/demo/image/upload/v1/foo.jpg" alt="x">'
    )
    assert_contains(out, "res.cloudinary.com")


def test_empty_input_returns_empty() -> None:
    assert sanitize_article_html("") == ""
    assert sanitize_article_html(None) == ""


def test_target_blank_gets_rel() -> None:
    out = sanitize_article_html(
        '<a href="https://example.com" target="_blank">x</a>'
    )
    assert_contains(out, 'rel="noopener noreferrer"')


def test_strips_inline_style() -> None:
    out = sanitize_article_html('<p style="color:red">x</p>')
    assert_not_contains(out, "color:red")


def main() -> None:
    tests = [
        test_strips_script_tags,
        test_strips_event_handlers,
        test_strips_javascript_href,
        test_strips_iframe,
        test_preserves_safe_markup,
        test_strips_off_host_image,
        test_allows_cloudinary_image,
        test_empty_input_returns_empty,
        test_target_blank_gets_rel,
        test_strips_inline_style,
    ]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()