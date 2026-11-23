"""Wayback Machine availability lookups and Save Page Now captures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from idsignpost.http_checker import USER_AGENT

AVAILABILITY_URL = "https://archive.org/wayback/available"
SAVE_URL = "https://web.archive.org/save/"
SPN2_SAVE_URL = "https://web.archive.org/save"
SPN2_STATUS_URL = "https://web.archive.org/save/status/"
LOOKUP_ATTEMPTS = 3
LOOKUP_BACKOFF = (2.0, 4.0)
SAVE_TIMEOUT = 60.0
SAVE_POLL_INTERVAL = 2.0
SAVE_MAX_POLLS = int(SAVE_TIMEOUT // SAVE_POLL_INTERVAL)
SAVE_CONCURRENCY = 2
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 2

logger = logging.getLogger(__name__)


def _https_archive(url: str) -> str:
    if url.startswith("http://web.archive.org"):
        return "https://web.archive.org" + url[len("http://web.archive.org") :]
    return url


def _clean_key(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _spn2_snapshot_url(payload: dict[str, Any], fallback_url: str) -> str | None:
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    original_url = payload.get("original_url")
    target = (
        original_url if isinstance(original_url, str) and original_url else fallback_url
    )
    return _https_archive(f"https://web.archive.org/web/{timestamp}/{target}")


class WaybackClient:
    """Query existing snapshots and optionally request new captures."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        concurrency: int = DEFAULT_CONCURRENCY,
        ia_access_key: str | None = None,
        ia_secret_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        access = _clean_key(ia_access_key)
        secret = _clean_key(ia_secret_key)
        if bool(access) != bool(secret):
            raise ValueError("IA_ACCESS_KEY and IA_SECRET_KEY must both be provided")
        self.timeout = timeout
        self._ia_access_key = access
        self._ia_secret_key = secret
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

    def _auth_headers(self) -> dict[str, str] | None:
        if self._ia_access_key and self._ia_secret_key:
            return {"Authorization": f"LOW {self._ia_access_key}:{self._ia_secret_key}"}
        return None

    async def save(self, url: str) -> str | None:
        """Request a new capture, using SPN2 auth when keys are configured."""
        async with self._save_semaphore:
            auth = self._auth_headers()
            if auth is not None:
                return await self._save_spn2(url, auth)
            return await self._save_anonymous(url)

    async def _save_anonymous(self, url: str) -> str | None:
        try:
            response = await self.client.get(f"{SAVE_URL}{url}", timeout=SAVE_TIMEOUT)
        except httpx.HTTPError:
            return None
        if response.status_code == 429:
            return None
        return self._snapshot_from_response(response)

    async def _save_spn2(self, url: str, auth_headers: dict[str, str]) -> str | None:
        headers = {"Accept": "application/json", **auth_headers}
        try:
            response = await self.client.post(
                SPN2_SAVE_URL,
                data={"url": url},
                headers=headers,
                timeout=SAVE_TIMEOUT,
            )
        except httpx.HTTPError:
            return None
        payload = self._parse_spn2_response(response)
        if payload is None:
            return None
        if payload.get("status") == "success":
            return _spn2_snapshot_url(payload, url)
        if payload.get("status") == "error":
            self._log_spn2_error(payload)
            return None
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return None
        return await self._poll_spn2_job(job_id, url, headers)

    async def _poll_spn2_job(
        self, job_id: str, url: str, headers: dict[str, str]
    ) -> str | None:
        started = asyncio.get_running_loop().time()
        for attempt in range(SAVE_MAX_POLLS):
            try:
                response = await self.client.get(
                    f"{SPN2_STATUS_URL}{job_id}",
                    headers=headers,
                    timeout=SAVE_TIMEOUT,
                )
            except httpx.HTTPError:
                return None
            payload = self._parse_spn2_response(response)
            if payload is None:
                return None
            status = payload.get("status")
            if status == "success":
                return _spn2_snapshot_url(payload, url)
            if status == "error":
                self._log_spn2_error(payload)
                return None
            if status != "pending":
                return None
            if (
                attempt + 1 >= SAVE_MAX_POLLS
                or asyncio.get_running_loop().time() - started >= SAVE_TIMEOUT
            ):
                return None
            await self._sleep(SAVE_POLL_INTERVAL)
        return None

    @staticmethod
    def _parse_spn2_response(response: httpx.Response) -> dict[str, Any] | None:
        if response.status_code in {401, 403}:
            logger.warning(
                "Internet Archive SPN2 authentication failed (%s)",
                response.status_code,
            )
            return None
        if response.status_code == 429 or not 200 <= response.status_code < 300:
            return None
        try:
            payload: Any = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _log_spn2_error(payload: dict[str, Any]) -> None:
        detail = payload.get("message") or payload.get("status_ext") or "unknown error"
        logger.warning("Internet Archive SPN2 capture failed: %s", detail)

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
