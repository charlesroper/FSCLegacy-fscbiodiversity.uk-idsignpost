"""Tests for Wayback lookups and Save Page Now captures."""

from __future__ import annotations

import httpx
import pytest
import respx

from idsignpost.wayback import (
    AVAILABILITY_URL,
    SAVE_TIMEOUT,
    SAVE_URL,
    WaybackClient,
)

URL = "https://example.org/guide"
SNAPSHOT = "https://web.archive.org/web/20240101000000/https://example.org/guide"

sleeps: list[float] = []


async def fake_sleep(seconds: float) -> None:
    sleeps.append(seconds)


@pytest.fixture(autouse=True)
def reset_sleeps() -> None:
    sleeps.clear()


async def make_client(concurrency: int = 2) -> WaybackClient:
    client = httpx.AsyncClient(follow_redirects=True)
    return WaybackClient(client=client, sleep=fake_sleep, concurrency=concurrency)


def availability(
    snapshot: str | None = SNAPSHOT, status: str = "200"
) -> httpx.Response:
    if snapshot is None:
        return httpx.Response(200, json={"archived_snapshots": {}})
    return httpx.Response(
        200,
        json={
            "archived_snapshots": {
                "closest": {
                    "status": status,
                    "url": snapshot,
                    "timestamp": "20240101000000",
                }
            }
        },
    )


@respx.mock
async def test_lookup_returns_https_snapshot() -> None:
    route = respx.get(AVAILABILITY_URL).mock(
        return_value=availability(
            "http://web.archive.org/web/20240101000000/https://example.org/guide"
        )
    )
    wayback = await make_client()

    snapshot = await wayback.lookup(URL)

    assert snapshot == SNAPSHOT
    assert route.calls[0].request.url.params["url"] == URL


@respx.mock
async def test_redirect_snapshots_are_ignored() -> None:
    respx.get(AVAILABILITY_URL).mock(return_value=availability(status="302"))

    assert await (await make_client()).lookup(URL) is None


@respx.mock
async def test_missing_snapshot_returns_none_without_retry() -> None:
    route = respx.get(AVAILABILITY_URL).mock(return_value=availability(None))

    assert await (await make_client()).lookup(URL) is None
    assert route.call_count == 1


@respx.mock
async def test_lookup_retries_temporary_failures_with_backoff() -> None:
    route = respx.get(AVAILABILITY_URL).mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("down"), availability()]
    )

    snapshot = await (await make_client()).lookup(URL)

    assert snapshot == SNAPSHOT
    assert route.call_count == 3
    assert sleeps == [2.0, 4.0]


@respx.mock
async def test_lookup_gives_up_after_three_attempts() -> None:
    route = respx.get(AVAILABILITY_URL).mock(return_value=httpx.Response(500))

    assert await (await make_client()).lookup(URL) is None
    assert route.call_count == 3
    assert sleeps == [2.0, 4.0]


@respx.mock
async def test_malformed_availability_payload_is_ignored() -> None:
    respx.get(AVAILABILITY_URL).mock(return_value=httpx.Response(200, text="not json"))

    assert await (await make_client()).lookup(URL) is None


@respx.mock
async def test_save_uses_sixty_second_timeout() -> None:
    route = respx.get(f"{SAVE_URL}{URL}").mock(
        return_value=httpx.Response(302, headers={"location": SNAPSHOT})
    )
    respx.get(SNAPSHOT).mock(return_value=httpx.Response(200))

    await (await make_client()).save(URL)

    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] == SAVE_TIMEOUT


@respx.mock
async def test_save_rate_limit_skips_the_run() -> None:
    route = respx.get(f"{SAVE_URL}{URL}").mock(return_value=httpx.Response(429))

    assert await (await make_client()).save(URL) is None
    assert route.call_count == 1


@respx.mock
async def test_save_reports_no_snapshot_without_archive_evidence() -> None:
    respx.get(f"{SAVE_URL}{URL}").mock(return_value=httpx.Response(200, text="queued"))

    assert await (await make_client()).save(URL) is None


@respx.mock
async def test_save_reads_relative_content_location() -> None:
    relative = "/web/20240101000000/https://example.org/guide"
    respx.get(f"{SAVE_URL}{URL}").mock(
        return_value=httpx.Response(200, headers={"content-location": relative})
    )

    assert await (await make_client()).save(URL) == SNAPSHOT


@respx.mock
async def test_save_transport_failure_returns_none() -> None:
    respx.get(f"{SAVE_URL}{URL}").mock(side_effect=httpx.TimeoutException("slow"))

    assert await (await make_client()).save(URL) is None
