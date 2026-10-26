"""Wayback Machine availability lookups and Save Page Now captures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin

import httpx

from idsignpost.http_checker import USER_AGENT

AVAILABILITY_URL = "https://archive.org/wayback/available"
SAVE_URL = "https://web.archive.org/save/"
LOOKUP_ATTEMPTS = 3
LOOKUP_BACKOFF = (2.0, 4.0)
SAVE_TIMEOUT = 60.0
SAVE_CONCURRENCY = 2
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 2


def _https_archive(url: str) -> str:
    if url.startswith("http://web.archive.org"):
        return "https://web.archive.org" + url[len("http://web.archive.org") :]
    return url


class WaybackClient:
    """Query existing snapshots and optionally request new captures."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        concurrency: int = DEFAULT_CONCURRENCY,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.timeout = timeout
        self._client = client
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._save_semaphore = asyncio.Semaphore(SAVE_CONCURRENCY)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def lookup(self, url: str) -> str | None:
        """Return the closest live snapshot, retrying temporary failures."""
        async with self._semaphore:
            for attempt in range(LOOKUP_ATTEMPTS):
                response: httpx.Response | None = None
                try:
                    response = await self.client.get(
                        AVAILABILITY_URL, params={"url": url}
                    )
                except httpx.HTTPError:
                    response = None
                if response is not None and 200 <= response.status_code < 300:
                    return self._closest_snapshot(response)
                if attempt < len(LOOKUP_BACKOFF):
                    await self._sleep(LOOKUP_BACKOFF[attempt])
        return None

    async def save(self, url: str) -> str | None:
        """Request a new capture, skipping the run on rate limiting."""
        async with self._save_semaphore:
            try:
                response = await self.client.get(
                    f"{SAVE_URL}{url}", timeout=SAVE_TIMEOUT
                )
            except httpx.HTTPError:
                return None
        if response.status_code == 429:
            return None
        return self._snapshot_from_response(response)

    async def find(self, url: str, *, save_if_missing: bool = False) -> str | None:
        """Look up a snapshot, capturing one when allowed and none exists."""
        snapshot = await self.lookup(url)
        if snapshot is not None or not save_if_missing:
            return snapshot
        return await self.save(url)

    @staticmethod
    def _closest_snapshot(response: httpx.Response) -> str | None:
        try:
            payload: Any = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        snapshots = payload.get("archived_snapshots")
        if not isinstance(snapshots, dict):
            return None
        closest = snapshots.get("closest")
        if not isinstance(closest, dict):
            return None
        if str(closest.get("status")) != "200":
            return None
        url = closest.get("url")
        if not isinstance(url, str) or not url:
            return None
        return _https_archive(url)

    @staticmethod
    def _snapshot_from_response(response: httpx.Response) -> str | None:
        candidates = [str(response.url)]
        content_location = response.headers.get("content-location")
        if content_location:
            candidates.append(urljoin(str(response.url), content_location))
        for candidate in candidates:
            if "web.archive.org/web/" in candidate:
                return _https_archive(candidate)
        return None
