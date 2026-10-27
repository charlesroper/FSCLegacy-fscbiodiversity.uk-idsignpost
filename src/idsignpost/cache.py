"""Two-tier atomic JSON cache for link verification results."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from idsignpost.export import atomic_write
from idsignpost.models import LinkState
from idsignpost.urls import cache_key

CACHE_VERSION = 2
SAVE_EVERY = 25
DEFAULT_TTL_DAYS = 30

STATES: frozenset[str] = frozenset({"ok", "broken", "blocked"})


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class UrlRecord:
    """Facts about one URL, shared by every entry that points at it."""

    status: str | None = None
    state: LinkState | None = None
    resolved_url: str | None = None
    is_suspicious_redirect: bool = False
    page_title: str | None = None
    page_h1: str | None = None
    page_snippet: str | None = None
    wayback_url: str | None = None
    checked_at: datetime | None = None


@dataclass(slots=True)
class EntryRecord:
    """Verdicts and alternatives specific to one entry and its URL."""

    redirect_is_match: bool | None = None
    redirect_reason: str | None = None
    alternative_url: str | None = None
    alternative_probability: float | None = None
    alternative_reasoning: str | None = None
    alternative_note: str | None = None
    alternative_checked: bool = False
    alternative_confirmed: bool = False
    checked_at: datetime | None = None

    def drop_alternative(self, note: str) -> None:
        """Clear a stored alternative and record the rejection note."""
        self.alternative_url = None
        self.alternative_probability = None
        self.alternative_reasoning = None
        self.alternative_note = note
        self.alternative_checked = True
        self.alternative_confirmed = False


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _opt_float(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _url_record(raw: Any) -> UrlRecord | None:
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if state is not None and state not in STATES:
        return None
    return UrlRecord(
        status=_opt_str(raw.get("status")),
        state=state,
        resolved_url=_opt_str(raw.get("resolved_url")),
        is_suspicious_redirect=bool(raw.get("is_suspicious_redirect", False)),
        page_title=_opt_str(raw.get("page_title")),
        page_h1=_opt_str(raw.get("page_h1")),
        page_snippet=_opt_str(raw.get("page_snippet")),
        wayback_url=_opt_str(raw.get("wayback_url")),
        checked_at=_parse_time(raw.get("checked_at")),
    )


def _entry_record(raw: Any) -> EntryRecord | None:
    if not isinstance(raw, dict):
        return None
    return EntryRecord(
        redirect_is_match=_opt_bool(raw.get("redirect_is_match")),
        redirect_reason=_opt_str(raw.get("redirect_reason")),
        alternative_url=_opt_str(raw.get("alternative_url")),
        alternative_probability=_opt_float(raw.get("alternative_probability")),
        alternative_reasoning=_opt_str(raw.get("alternative_reasoning")),
        alternative_note=_opt_str(raw.get("alternative_note")),
        alternative_checked=bool(raw.get("alternative_checked", False)),
        alternative_confirmed=bool(raw.get("alternative_confirmed", False)),
        checked_at=_parse_time(raw.get("checked_at")),
    )


def _url_payload(record: UrlRecord) -> dict[str, Any]:
    return {
        "status": record.status,
        "state": record.state,
        "resolved_url": record.resolved_url,
        "is_suspicious_redirect": record.is_suspicious_redirect,
        "page_title": record.page_title,
        "page_h1": record.page_h1,
        "page_snippet": record.page_snippet,
        "wayback_url": record.wayback_url,
        "checked_at": _iso(record.checked_at),
    }


def _entry_payload(record: EntryRecord) -> dict[str, Any]:
    return {
        "redirect_is_match": record.redirect_is_match,
        "redirect_reason": record.redirect_reason,
        "alternative_url": record.alternative_url,
        "alternative_probability": record.alternative_probability,
        "alternative_reasoning": record.alternative_reasoning,
        "alternative_note": record.alternative_note,
        "alternative_checked": record.alternative_checked,
        "alternative_confirmed": record.alternative_confirmed,
        "checked_at": _iso(record.checked_at),
    }


@dataclass
class CatalogCache:
    """URL-tier and entry-tier cache with TTL and atomic saves."""

    path: Path = Path(".url_cache.json")
    ttl_days: int = DEFAULT_TTL_DAYS
    now: Callable[[], datetime] = utcnow
    urls: dict[str, UrlRecord] = field(default_factory=dict)
    entries: dict[str, EntryRecord] = field(default_factory=dict)
    pending_writes: int = 0

    @classmethod
    def load(
        cls,
        path: str | Path = ".url_cache.json",
        *,
        ttl_days: int = DEFAULT_TTL_DAYS,
        now: Callable[[], datetime] = utcnow,
    ) -> CatalogCache:
        """Load a cache file, discarding it when the schema does not match."""
        cache = cls(path=Path(path), ttl_days=ttl_days, now=now)
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cache
        if not isinstance(raw, dict) or raw.get("schema_version") != CACHE_VERSION:
            return cache

        for key, value in (raw.get("urls") or {}).items():
            record = _url_record(value)
            if (
                isinstance(key, str)
                and record is not None
                and record.checked_at is not None
            ):
                cache.urls[key] = record
        for key, value in (raw.get("entries") or {}).items():
            record = _entry_record(value)
            if (
                isinstance(key, str)
                and record is not None
                and record.checked_at is not None
            ):
                cache.entries[key] = record
        return cache

    def _expired(self, checked_at: datetime | None) -> bool:
        if checked_at is None:
            return True
        return self.now() - checked_at > timedelta(days=self.ttl_days)

    def get_url(self, url: str) -> UrlRecord | None:
        """Return a fresh URL record, or None when absent or expired."""
        record = self.urls.get(cache_key(url))
        if record is None or self._expired(record.checked_at):
            return None
        return record

    def put_url(
        self, url: str, record: UrlRecord | None, *, cacheable: bool = True
    ) -> bool:
        """Store a URL record unless the result is transient."""
        if record is None or not cacheable:
            return False
        if record.checked_at is None:
            record.checked_at = self.now()
        self.urls[cache_key(url)] = record
        self._changed()
        return True

    def get_wayback(self, url: str) -> str | None:
        """Return a fresh cached Wayback snapshot for a URL, if any."""
        record = self.get_url(url)
        return record.wayback_url if record is not None else None

    def set_wayback(self, url: str, snapshot: str) -> bool:
        """Store a Wayback snapshot on the URL record, creating one if needed."""
        record = self.get_url(url) or UrlRecord()
        record.wayback_url = snapshot
        return self.put_url(url, record)

    def get_entry(self, entry_id: str, url: str) -> EntryRecord | None:
        """Return a fresh entry-tier record, or None when absent or expired."""
        record = self.entries.get(self._entry_key(entry_id, url))
        if record is None or self._expired(record.checked_at):
            return None
        return record

    def put_entry(
        self,
        entry_id: str,
        url: str,
        record: EntryRecord | None,
        *,
        cacheable: bool = True,
    ) -> bool:
        """Store an entry-tier record unless the result is transient."""
        if record is None or not cacheable:
            return False
        if record.checked_at is None:
            record.checked_at = self.now()
        self.entries[self._entry_key(entry_id, url)] = record
        self._changed()
        return True

    @staticmethod
    def _entry_key(entry_id: str, url: str) -> str:
        return f"{entry_id}::{cache_key(url)}"

    def _changed(self) -> None:
        self.pending_writes += 1
        if self.pending_writes >= SAVE_EVERY:
            self.save()

    def save(self) -> None:
        """Write the cache atomically, dropping expired records."""
        urls = {
            key: _url_payload(record)
            for key, record in sorted(self.urls.items())
            if not self._expired(record.checked_at)
        }
        entries = {
            key: _entry_payload(record)
            for key, record in sorted(self.entries.items())
            if not self._expired(record.checked_at)
        }
        payload = {"schema_version": CACHE_VERSION, "urls": urls, "entries": entries}
        atomic_write(self.path, lambda handle: handle.write(_pretty(payload)))
        self.pending_writes = 0

    def __len__(self) -> int:
        return len(self.urls) + len(self.entries)


def _pretty(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
