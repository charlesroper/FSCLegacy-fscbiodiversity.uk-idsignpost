"""Tests for HTTP liveness checks and redirect analysis."""

from __future__ import annotations

import httpx
import pytest
import respx

from idsignpost.http_checker import (
    MAX_HTML_BYTES,
    OFFLINE_MIN_CHECKS,
    CheckResult,
    HTTPChecker,
    parse_page,
)

URL = "https://example.org/guide"


def html(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "text/html"}, content=body.encode("utf-8")
    )


async def check(
    mocked: respx.Route,
    url: str = URL,
    *,
    entry_title: str | None = None,
    responses: list[httpx.Response] | None = None,
) -> CheckResult:
    if responses is not None:
        mocked.mock(side_effect=responses)
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=10) as client:
        return await HTTPChecker(client=client).check(url, entry_title=entry_title)


@respx.mock
async def test_ok_extracts_title_and_heading() -> None:
    route = respx.get(URL).mock(
        return_value=html("<html><title> Bat guide </title><h1>Bats</h1></html>")
    )

    result = await check(route)

    assert result.status == "200"
    assert result.state == "ok"
    assert result.cacheable is True
    assert result.page_title == "Bat guide"
    assert result.page_h1 == "Bats"
    assert result.resolved_url is None
    assert result.is_suspicious_redirect is False


@respx.mock
async def test_non_html_body_is_not_read() -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"<title>PDF</title>",
        )
    )

    result = await check(route)

    assert result.state == "ok"
    assert result.page_title is None


@respx.mock
async def test_html_body_is_capped_at_sixteen_kilobytes() -> None:
    body = "<html>" + ("x" * 20_000) + "<title>Too late</title></html>"
    route = respx.get(URL).mock(return_value=html(body))

    result = await check(route)

    assert result.page_title is None
    assert MAX_HTML_BYTES == 16 * 1024


@pytest.mark.parametrize(
    ("code", "state", "cacheable"),
    [
        (200, "ok", True),
        (204, "ok", True),
        (301, "ok", True),
        (400, "blocked", True),
        (401, "blocked", True),
        (403, "blocked", True),
        (404, "broken", True),
        (405, "blocked", True),
        (410, "broken", True),
        (418, "blocked", True),
        (429, "blocked", False),
        (451, "blocked", True),
        (500, "blocked", False),
        (501, "blocked", False),
        (502, "blocked", False),
        (503, "blocked", False),
        (504, "blocked", False),
    ],
)
@respx.mock
async def test_status_classification(code: int, state: str, cacheable: bool) -> None:
    route = respx.get(URL).mock(return_value=html("", status=code))

    result = await check(route)

    assert result.status == str(code)
    assert result.state == state
    assert result.cacheable is cacheable


@respx.mock
async def test_403_is_retried_with_a_plain_get() -> None:
    route = respx.get(URL).mock(
        side_effect=[html("", status=403), html("<title>Guide</title>")]
    )

    result = await check(route)

    assert route.call_count == 2
    assert result.status == "200"
    assert result.state == "ok"
    assert result.page_title == "Guide"


@respx.mock
async def test_400_retry_keeps_the_second_verdict() -> None:
    route = respx.get(URL).mock(
        side_effect=[html("", status=400), html("", status=404)]
    )

    result = await check(route)

    assert route.call_count == 2
    assert result.status == "404"
    assert result.state == "broken"


@pytest.mark.parametrize(
    ("error", "status", "state", "cacheable"),
    [
        (httpx.TimeoutException("slow"), "timeout", "blocked", False),
        (httpx.ConnectError("down"), "connect_error", "broken", False),
        (httpx.TooManyRedirects("loop"), "too_many_redirects", "broken", True),
        (httpx.RemoteProtocolError("bad"), "protocol_error", "broken", True),
        (httpx.InvalidURL("bad"), "invalid_url", "broken", True),
        (httpx.ReadError("reset"), "http_error", "blocked", False),
    ],
)
@respx.mock
async def test_transport_errors_are_classified(
    error: httpx.HTTPError, status: str, state: str, cacheable: bool
) -> None:
    route = respx.get(URL).mock(side_effect=error)

    result = await check(route)

    assert result.status == status
    assert result.state == state
    assert result.cacheable is cacheable


@respx.mock
async def test_same_host_redirect_records_resolved_url() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://example.org/new"}
        )
    )
    respx.get("https://example.org/new").mock(
        return_value=html("<title>Moved guide</title>")
    )

    result = await check(respx.routes[0])

    assert result.status == "200"
    assert result.resolved_url == "https://example.org/new"
    assert result.is_suspicious_redirect is False


@respx.mock
async def test_trailing_slash_only_redirect_is_not_resolved() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            301, headers={"location": "https://example.org/guide/"}
        )
    )
    respx.get("https://example.org/guide/").mock(
        return_value=html("<title>Guide</title>")
    )

    result = await check(respx.routes[0])

    assert result.resolved_url is None


@pytest.mark.parametrize(
    ("target", "page", "entry_title", "suspicious"),
    [
        ("https://other.example.org/guide", "<title>Guide</title>", "Bats", True),
        ("https://example.org/", "<title>Home</title>", "Bats", True),
        ("https://example.org/gone", "<title>Page not found</title>", "Bats", True),
        ("https://example.org/expired", "<title>Guide</title>", "Bats", True),
        ("https://example.org/shop", "<title>Shop</title>", "Bats in flight", True),
        ("https://example.org/shop", "<title>Shop</title>", None, False),
        ("https://example.org/login", "<title>Sign in</title>", "Bats", True),
        ("https://example.org/new", "<title>Bats guide</title>", "Bats", False),
    ],
)
@respx.mock
async def test_suspicious_redirect_detection(
    target: str, page: str, entry_title: str | None, suspicious: bool
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"location": target}))
    respx.get(target).mock(return_value=html(page))

    result = await check(respx.routes[0], entry_title=entry_title)

    assert result.resolved_url == target
    assert result.is_suspicious_redirect is suspicious


@respx.mock
async def test_soft_404_phrase_in_path_is_suspicious() -> None:
    target = "https://example.org/404.html"
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"location": target}))
    respx.get(target).mock(return_value=html("<title>Guide</title>"))

    result = await check(respx.routes[0])

    assert result.is_suspicious_redirect is True


@respx.mock
async def test_offline_guard_triggers_after_ten_network_failures() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        checker = HTTPChecker(client=client)
        for _ in range(OFFLINE_MIN_CHECKS - 1):
            await checker.check(URL)
        assert checker.should_abort() is False

        await checker.check(URL)
        assert checker.should_abort() is True


@respx.mock
async def test_offline_guard_clears_after_a_real_response() -> None:
    respx.get("https://example.org/down").mock(side_effect=httpx.ConnectError("down"))
    respx.get("https://example.org/live").mock(return_value=html("<title>OK</title>"))
    async with httpx.AsyncClient() as client:
        checker = HTTPChecker(client=client)
        for _ in range(OFFLINE_MIN_CHECKS):
            await checker.check("https://example.org/down")
        await checker.check("https://example.org/live")

        assert checker.should_abort() is False


@respx.mock
async def test_offline_guard_ignores_non_network_failures() -> None:
    respx.get(URL).mock(side_effect=httpx.RemoteProtocolError("bad"))
    async with httpx.AsyncClient() as client:
        checker = HTTPChecker(client=client)
        for _ in range(OFFLINE_MIN_CHECKS):
            await checker.check(URL)

        assert checker.should_abort() is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", (None, None, None)),
        (b"<html><body>no head</body></html>", (None, None, "no head")),
        (b"<title>  A   guide </title><h1>Bats</h1>", ("A guide", "Bats", None)),
        (
            b"<html><body><nav>menu</nav><main><h1>Bats</h1>"
            b"<p>Identify bats.</p></main><footer>foot</footer></body></html>",
            (None, "Bats", "Bats Identify bats."),
        ),
    ],
)
def test_parse_page(
    body: bytes, expected: tuple[str | None, str | None, str | None]
) -> None:
    assert parse_page(body) == expected
