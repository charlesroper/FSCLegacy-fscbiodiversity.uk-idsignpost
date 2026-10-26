"""Async HTTP liveness checks, redirect analysis, and page metadata extraction."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from idsignpost.models import LinkState
from idsignpost.urls import clean_host, sanitize_url, urls_equal

MAX_HTML_BYTES = 16 * 1024
MAX_REDIRECTS = 10
DEFAULT_TIMEOUT = 10.0
OFFLINE_MIN_CHECKS = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
PLAIN_HEADERS = {"User-Agent": USER_AGENT}

BROKEN_CODES = frozenset({404, 410})
BLOCKED_CODES = frozenset({400, 401, 403, 405, 451})
TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_CODES = frozenset({400, 403})

SOFT_404_PHRASES = ("404", "not found", "page not found", "error", "expired")
GENERIC_HUBS = frozenset(
    {
        "/shop",
        "/shop/publications",
        "/product-category",
        "/resources",
        "/search",
        "/category",
    }
)
LOGIN_PHRASES = ("login", "signin", "register", "subscribe", "consent", "cookie")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one URL check, shaped for the URL cache tier."""

    status: str
    state: LinkState
    resolved_url: str | None = None
    is_suspicious_redirect: bool = False
    page_title: str | None = None
    page_h1: str | None = None
    cacheable: bool = True


def _classify_code(code: int) -> tuple[LinkState, bool]:
    if 200 <= code < 400:
        return "ok", True
    if code in BROKEN_CODES:
        return "broken", True
    if code in BLOCKED_CODES:
        return "blocked", True
    if code in TRANSIENT_CODES:
        return "blocked", False
    if 400 <= code < 500:
        return "blocked", True
    return "blocked", False


def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text or None


def parse_page(body: bytes) -> tuple[str | None, str | None]:
    """Extract the page title and first heading from HTML bytes."""
    if not body:
        return None, None
    soup = BeautifulSoup(body, "html.parser")
    return _text(soup.find("title")), _text(soup.find("h1"))


class HTTPChecker:
    """Run bounded streaming checks and flag suspicious redirects."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        self._client = client
        self.checks = 0
        self.network_failures = 0
        self.responses_seen = 0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def should_abort(self) -> bool:
        """Detect a run where every check failed at the network level."""
        return (
            self.checks >= OFFLINE_MIN_CHECKS
            and self.responses_seen == 0
            and self.network_failures >= self.checks
        )

    async def check(self, url: str, *, entry_title: str | None = None) -> CheckResult:
        """Check one URL and classify the outcome."""
        self.checks += 1
        try:
            response, body = await self._streaming_get(url, BROWSER_HEADERS)
            if response.status_code in RETRY_CODES:
                response, body = await self._plain_get(url)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return self._from_exception(exc)

        code = response.status_code
        if 200 <= code < 300 or 400 <= code < 500:
            self.responses_seen += 1

        state, cacheable = _classify_code(code)
        final_url = sanitize_url(str(response.url))
        resolved_url = None if urls_equal(url, final_url) else final_url
        page_title, page_h1 = parse_page(body)
        suspicious = resolved_url is not None and _is_suspicious(
            url, final_url, page_title, entry_title
        )
        return CheckResult(
            status=str(code),
            state=state,
            resolved_url=resolved_url,
            is_suspicious_redirect=suspicious,
            page_title=page_title,
            page_h1=page_h1,
            cacheable=cacheable,
        )

    def _from_exception(self, exc: httpx.HTTPError | httpx.InvalidURL) -> CheckResult:
        if isinstance(exc, httpx.TimeoutException):
            self.network_failures += 1
            return CheckResult("timeout", "blocked", cacheable=False)
        if isinstance(exc, httpx.ConnectError):
            self.network_failures += 1
            return CheckResult("connect_error", "broken", cacheable=False)
        if isinstance(exc, httpx.TooManyRedirects):
            return CheckResult("too_many_redirects", "broken")
        if isinstance(exc, httpx.RemoteProtocolError):
            return CheckResult("protocol_error", "broken")
        if isinstance(exc, httpx.InvalidURL):
            return CheckResult("invalid_url", "broken")
        return CheckResult("http_error", "blocked", cacheable=False)

    async def _streaming_get(
        self, url: str, headers: dict[str, str]
    ) -> tuple[httpx.Response, bytes]:
        async with self.client.stream("GET", url, headers=headers) as response:
            body = await self._read_html(response)
        return response, body

    async def _plain_get(self, url: str) -> tuple[httpx.Response, bytes]:
        response = await self.client.get(url, headers=PLAIN_HEADERS)
        content_type = response.headers.get("content-type", "").lower()
        body = response.content[:MAX_HTML_BYTES] if "text/html" in content_type else b""
        return response, body

    @staticmethod
    async def _read_html(response: httpx.Response) -> bytes:
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            return b""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_HTML_BYTES:
                break
        return b"".join(chunks)[:MAX_HTML_BYTES]


def _is_suspicious(
    original_url: str,
    final_url: str,
    page_title: str | None,
    entry_title: str | None,
) -> bool:
    original_path = urlsplit(original_url).path or "/"
    final_path = urlsplit(final_url).path or "/"

    if clean_host(original_url) != clean_host(final_url):
        return True
    if original_path not in {"", "/"} and final_path in {"", "/"}:
        return True

    haystack = f"{final_path} {page_title or ''}".lower()
    if any(phrase in haystack for phrase in SOFT_404_PHRASES):
        return True
    if entry_title and final_path.rstrip("/").lower() in GENERIC_HUBS:
        return True
    return any(phrase in haystack for phrase in LOGIN_PHRASES)
