"""Canonical catalog models and loading helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LinkState = Literal["ok", "broken", "blocked"]

CANONICAL_STATUS_ERRORS = frozenset(
    {
        "connect_error",
        "timeout",
        "protocol_error",
        "too_many_redirects",
        "invalid_url",
        "http_error",
        "redirect_mismatch",
    }
)

_HTTP_STATUS = re.compile(r"^[1-5]\d{2}$")


class CatalogEntry(BaseModel):
    """One validated resource in the idsignpost catalog."""

    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    title: str
    submitted_by: str | None = None
    submitted_at: str | None = None

    description: str | None = None
    authors: str | None = None
    is_free: bool | None = None
    available_as: list[str] = Field(default_factory=list)
    keywords: str | None = None
    comment_body: str | None = None

    web_link: str | None = None
    additional_web_links: list[str] = Field(default_factory=list)

    web_link_status: str | None = None
    web_link_state: LinkState | None = None
    web_link_resolved: str | None = None
    wayback_link: str | None = None
    alternative_link: str | None = None
    alternative_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    alternative_reasoning: str | None = None
    verified_at: str | None = None

    @field_validator("web_link_status")
    @classmethod
    def _validate_status(cls, value: str | None) -> str | None:
        if value is None or _HTTP_STATUS.fullmatch(value):
            return value
        if value in CANONICAL_STATUS_ERRORS:
            return value
        raise ValueError(f"invalid web_link_status: {value!r}")

    @field_validator("submitted_at")
    @classmethod
    def _validate_submitted_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError("submitted_at must be naive site-local time")
        return value

    @field_validator("verified_at")
    @classmethod
    def _validate_verified_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("verified_at must be UTC")
        return value


def ensure_unique_ids(entries: Iterable[CatalogEntry]) -> None:
    """Raise ValueError when two entries share an id."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            duplicates.add(entry.id)
        seen.add(entry.id)
    if duplicates:
        names = ", ".join(sorted(duplicates))
        raise ValueError(f"duplicate catalog ids: {names}")


def load_catalog(path: str | Path) -> list[CatalogEntry]:
    """Load and validate a catalog file, rejecting duplicate ids."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("catalog must be a JSON array")
    entries = [CatalogEntry.model_validate(item) for item in raw]
    ensure_unique_ids(entries)
    return entries
