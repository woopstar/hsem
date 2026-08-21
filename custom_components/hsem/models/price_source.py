"""Price-source provenance shared by coordinator and planner models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type PriceSource = Literal["primary", "entsoe", "forecast"]


@dataclass(frozen=True)
class PriceBackupStatus:
    """Safe per-cycle status for the paired published-price backup."""

    configured: bool = False
    matched_slots: int = 0
    rejection_reason: str | None = None

    def as_dict(self) -> dict[str, bool | int | str | None]:
        """Return a JSON-safe diagnostic representation."""
        return {
            "configured": self.configured,
            "matched_slots": self.matched_slots,
            "rejection_reason": self.rejection_reason,
        }
