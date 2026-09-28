"""Domain-local text quality and redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii


def quality_failure(text: str, config: dict[str, Any]) -> str | None:
    """Return the first configured quality failure code, or ``None``."""

    if len(text) > config.get("max_chars_per_field", 0):
        return "E_QUALITY_MAX_CHARS"
    length = max(len(text), 1)
    control_count = sum(1 for char in text if ord(char) < 32 and char not in "\n\t")
    replacement_count = text.count("\ufffd")
    printable_count = sum(1 for char in text if char.isprintable() or char in "\n\t")
    if control_count / length > config.get("max_control_char_ratio", 0):
        return "E_QUALITY_CONTROL_RATIO"
    if replacement_count / length > config.get("max_replacement_char_ratio", 0):
        return "E_QUALITY_REPLACEMENT_RATIO"
    if printable_count / length < config.get("min_printable_ratio", 1):
        return "E_QUALITY_PRINTABLE_RATIO"
    return None


def redact_sensitive_text(text: str) -> tuple[str, bool]:
    """Redact shared-detector findings and report whether the result is safe."""

    findings = [*detect_credentials(text), *detect_pii(text)]
    if not findings:
        return text, True
    intervals: list[tuple[int, int]] = []
    for finding in sorted(findings, key=lambda item: (item["start"], item["end"])):
        start, end = int(finding["start"]), int(finding["end"])
        if not intervals or start > intervals[-1][1]:
            intervals.append((start, end))
        else:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
    redacted = text
    for start, end in reversed(intervals):
        redacted = redacted[:start] + "[REDACTED]" + redacted[end:]
    safe = not detect_credentials(redacted) and not detect_pii(redacted)
    return redacted, safe


# Structural separators used by identifiers, paths and filenames. Splitting on these
# before scanning is NOT a substitute detector — the shared `detect_pii` /
# `detect_credentials` still make every decision. It exists because those detectors are
# written for prose and match a whole token: `taro@example.com` is found in
# "tracker/taro@example.com", but in "tracker/2026Q3_taro@example.com_progress.xlsx" the
# trailing `_progress.xlsx` is absorbed into the domain, the pattern fails, and the scan
# reports the string as clean. Metadata is where that shape actually occurs — a locator
# is a path, an identifier is minted from something. Feeding the detector one structural
# token at a time makes it effective on those fields without inventing a pattern.
_STRUCTURAL_SPLIT = re.compile(r"([\\/\s_]+)")


def _redact_structural_tokens(text: str) -> str:
    """Run the shared detectors over each structural token of ``text``."""

    parts = _STRUCTURAL_SPLIT.split(text)
    return "".join(
        part if (not part or _STRUCTURAL_SPLIT.fullmatch(part)) else redact_sensitive_text(part)[0] for part in parts
    )


def _is_clean(text: str) -> bool:
    """True when neither the whole string nor any structural token matches a detector."""

    if detect_credentials(text) or detect_pii(text):
        return False
    return _redact_structural_tokens(text) == text


def redact_string_leaves(value: Any) -> tuple[Any, bool, bool]:
    """Redact every string leaf of a JSON-compatible structure.

    Returns ``(redacted, changed, safe)``. ``changed`` reports whether any leaf was
    rewritten; ``safe`` reports that a re-scan of every rewritten leaf found nothing
    left. The two are different questions — an already-clean structure is ``safe``
    but not ``changed`` — and conflating them is what let a clean record be counted
    as redacted and an unredactable one be returned anyway.

    Applied to a whole record rather than one field because a source system puts
    text wherever it likes: a locator can be a spreadsheet filename containing a
    person's name, an identifier can be minted from an address, and a milestone
    label is free text. Redacting only the obvious field leaves the rest in State.
    """

    changed = False
    safe = True

    def walk(node: Any) -> Any:
        nonlocal changed, safe
        if isinstance(node, str):
            redacted = _redact_structural_tokens(redact_sensitive_text(node)[0])
            if redacted != node:
                changed = True
            if not _is_clean(redacted):
                safe = False
            return redacted
        if isinstance(node, dict):
            return {key: walk(child) for key, child in node.items()}
        if isinstance(node, list):
            return [walk(child) for child in node]
        return node

    return walk(value), changed, safe


def iter_string_leaves(value: object, path: tuple[object, ...] = ()) -> Iterator[tuple[tuple[object, ...], str]]:
    """Yield paths and string leaves from JSON-compatible containers."""

    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from iter_string_leaves(child, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from iter_string_leaves(child, (*path, index))
