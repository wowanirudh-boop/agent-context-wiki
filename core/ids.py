from __future__ import annotations

from typing import Literal

from ulid import ULID

IdPrefix = Literal["cb", "ch", "run", "pg", "sv", "ev"]


def new_id(prefix: IdPrefix) -> str:
    return f"{prefix}_{str(ULID()).lower()}"


def review_row_id(run_id: str, row_number: int) -> str:
    if row_number < 1:
        raise ValueError("Review row numbers are 1-based")
    return f"RR-{run_id}-{row_number}"
