"""
Reusable input validators and sanitizers for the CIV-CON API.

Routers can call these directly. They're not wired into the Pydantic
schema layer because that would force a stricter contract across
all existing endpoints; instead, new code can call them where useful
without breaking the existing frontend compatibility contract.
"""
from __future__ import annotations

import re

# ============================================================================
# Phone number validation
# ============================================================================


# Uganda phone numbers:
#   - Mobile:  +256 7XX XXX XXX  (10 digits after country code, 70/71/72/73/74/75/76/77/78/79)
#   - Landline: +256 4XX XXX XXX
#
# We accept E.164 ("+2567..."), national ("07..."), and the legacy
# double-zero prefix ("002567...").
_UGANDA_MOBILE_RE = re.compile(
    r"^(?:\+?256|0|00256)?(7[0-9])\d{7}$"
)
_UGANDA_LANDLINE_RE = re.compile(
    r"^(?:\+?256|0|00256)?(4[0-9])\d{7}$"
)


def is_valid_uganda_phone(phone: str) -> bool:
    """Return True if `phone` is a valid Ugandan phone number."""
    if not phone:
        return False
    digits = re.sub(r"[\s\-()]", "", phone)
    return bool(
        _UGANDA_MOBILE_RE.match(digits) or _UGANDA_LANDLINE_RE.match(digits)
    )


def normalize_uganda_phone(phone: str) -> str | None:
    """Return the E.164 form of a Ugandan phone number, or None if invalid.

    Examples:
        "0700123456"   -> "+256700123456"
        "256700123456" -> "+256700123456"
        "+256 700 123 456" -> "+256700123456"
    """
    if not phone:
        return None
    digits = re.sub(r"[\s\-()]", "", phone)
    # Strip leading "00" or "0" so we can re-add the canonical prefix.
    if digits.startswith("00256"):
        digits = digits[5:]
    elif digits.startswith("+256"):
        digits = digits[4:]
    elif digits.startswith("256"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]

    if len(digits) != 9 or not digits.isdigit():
        return None
    # Mobile prefixes
    if digits[0] == "7" and digits[1] in {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}:
        return f"+256{digits}"
    # Landline (4XX...)
    if digits[0] == "4":
        return f"+256{digits}"
    return None


# ============================================================================
# Text sanitization
# ============================================================================


# Common control characters that we strip from user input. We keep
# regular whitespace (tab, newline, space) but drop everything else
# below ASCII 32 except those three.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_text(value: str | None, *, max_length: int = 5000) -> str:
    """Strip control characters and trim whitespace from a string.

    Returns "" for None or empty input. Truncates to `max_length`.
    """
    if not value:
        return ""
    cleaned = _CONTROL_CHAR_RE.sub("", value)
    cleaned = cleaned.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def normalize_email(value: str | None) -> str | None:
    """Trim whitespace and lower-case an email address. Returns None if empty."""
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None
