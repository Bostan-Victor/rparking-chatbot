from __future__ import annotations

import re


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip()
    return bool(_EMAIL_RE.fullmatch(v))


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def is_valid_phone(value: str | None) -> bool:
    if not value:
        return False

    v = value.strip()
    # Reject obvious non-phone inputs even if they contain many digits.
    if re.search(r"[A-Za-z]", v):
        return False

    digits = phone_digits(v)
    # Moldova: 8 significant digits; 9 with leading 0 (068...); 11 with +373 country code.
    return len(digits) in {8, 9, 11}


def parse_locations_count_strict(value: object) -> int | None:
    """Parse a locations count and reject non-integers.

    Accepts:
    - int
    - numeric strings like "3" (with whitespace)

    Rejects:
    - floats (e.g. 2.0, 2.5)
    - strings with non-digits (e.g. "3 locatii", "2.5")
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        raise ValueError("'nr_of_locations' must be an integer")

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        raise ValueError("'nr_of_locations' must be an integer")

    if isinstance(value, str):
        s = value.strip()
        if not s.isdigit():
            raise ValueError("'nr_of_locations' must be an integer")
        return int(s)

    raise ValueError("'nr_of_locations' must be an integer")
