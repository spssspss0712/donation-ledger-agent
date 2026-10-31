from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class SheetReadError(Exception):
    """The sheet could not be read at all (missing file, auth failure, empty sheet)."""


@dataclass(frozen=True)
class SheetTable:
    """Header row plus data rows, all as display strings. Data row 1 is sheet row 2."""

    header: Sequence[str]
    rows: Sequence[Sequence[str]]


class SheetReader(Protocol):
    def read(self) -> SheetTable: ...
