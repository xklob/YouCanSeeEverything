"""Deterministic local export parsers.

Parser content is untrusted data. Parsers never execute imported instructions,
perform network requests, or modify source files.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Iterable

from .events import EventCollection


@dataclass
class ParseResult:
    events: EventCollection = field(default_factory=EventCollection)
    parser: str = "unknown"
    status: str = "unsupported"
    notes: list[str] = field(default_factory=list)


def parse_file(
    path: Path, original_path: str, source: str, *, max_text_chars: int = 200_000,
    excluded_directory_names: Iterable[str] = (),
) -> ParseResult:
    """Extract searchable events while preserving honest dates and locators."""
    from .engine import parse

    return parse(Path(path), original_path, source, max_text_chars=max_text_chars, excluded_directory_names=excluded_directory_names)


__all__ = ["ParseResult", "parse_file"]
