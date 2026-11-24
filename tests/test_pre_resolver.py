"""Tests for deterministic publisher pre-resolution."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from idsignpost.models import CatalogEntry
from idsignpost.pre_resolver import PublisherPreResolver


def make_entry(
    web_link: str | None,
    *,
    title: str = "Sample Guide",
    entry_id: str = "sample",
) -> CatalogEntry:
    return CatalogEntry(
        id=entry_id,
        filename=f"{entry_id}.html",
        title=title,
        web_link=web_link,
    )


@pytest.mark.parametrize(
    ("web_link", "expected_url"),
    [
        (
            "https://www.summerfieldbooks.com/sea-beans-and-nickar-nuts~263",
            "https://www.summerfieldbooks.com/product/sea-beans-and-nickar-nuts/",
        ),
        (
            "https://summerfieldbooks.com/colour-identification-guide-to-grasses%2C-sedges%2C-rushes-and-ferns~207",
            "https://www.summerfieldbooks.com/product/colour-identification-guide-to-grasses-sedges-rushes-and-ferns/",
        ),
        (
            "https://www.summerfieldbooks.com/seasearch%2C-observer%27s-guide-to-marine-life-of-britain-and-ireland%2C--2nd-edition~5960",
            "https://www.summerfieldbooks.com/product/seasearch-observers-guide-to-marine-life-of-britain-and-ireland-2nd-edition/",
        ),
        (
            "https://www.summerfieldbooks.com/hybrid-flora-of-the-british-isles.~4789",
            "https://www.summerfieldbooks.com/product/hybrid-flora-of-the-british-isles/",
        ),
        (
            "https://www.summerfieldbooks.com/les-myxomyc%C3%A8tes~3341",
            "https://www.summerfieldbooks.com/product/les-myxomycetes/",
        ),
        (
            "https://www.summerfieldbooks.com/myxomycetes-of-britain-and-ireland:-an-identification-handbook~504",
            "https://www.summerfieldbooks.com/product/myxomycetes-of-britain-and-ireland-an-identification-handbook/",
        ),
        (
            "https://www.summerfieldbooks.com/the-wild-flower-key%3A-how-to-identify-wild-flowers-trees-and-shrubs-in-britain-and-ireland~1578",
            "https://www.summerfieldbooks.com/product/the-wild-flower-key-how-to-identify-wild-flowers-trees-and-shrubs-in-britain-and-ireland/",
        ),
    ],
)
async def test_summerfield_legacy_urls_rewrite_deterministically(
    web_link: str, expected_url: str
) -> None:
    resolver = PublisherPreResolver()
    try:
        candidates = await resolver.resolve(make_entry(web_link))
    finally:
        await resolver.aclose()

    assert len(candidates) == 1
    assert candidates[0].url == expected_url
    assert candidates[0].probability == pytest.approx(0.85)
    assert (
        candidates[0].reasoning == "pre-resolved legacy Summerfield Books product URL"
    )


@pytest.mark.parametrize(
    "web_link",
    [
        None,
        "https://www.summerfieldbooks.com/product/sea-beans-and-nickar-nuts/",
        "https://www.summerfieldbooks.com/",
        "https://example.org/sea-beans-and-nickar-nuts~263",
    ],
)
async def test_summerfield_ignores_non_legacy_or_other_hosts(
    web_link: str | None,
) -> None:
    resolver = PublisherPreResolver()
    try:
        candidates = await resolver.resolve(make_entry(web_link))
    finally:
        await resolver.aclose()

    assert candidates == []


@respx.mock
async def test_royensoc_resolves_pdf_via_media_api_with_stem_normalization() -> None:
    media_url = "https://www.royensoc.co.uk/wp-json/wp/v2/media"
    media_route = respx.get(media_url).mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "slug": "vol01_part1213",
                        "title": {"rendered": "Vol01_Part1213"},
                        "mime_type": "application/pdf",
                        "source_url": "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part1213.pdf",
                    }
                ],
            ),
            httpx.Response(
                200,
                json=[
                    {
                        "slug": "vol01_part07_0",
                        "title": {"rendered": "Vol01_Part07_0"},
                        "mime_type": "application/pdf",
                        "source_url": "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part07_0.pdf",
                    }
                ],
            ),
        ]
    )

    resolver = PublisherPreResolver()
    try:
        plus_candidates = await resolver.resolve(
            make_entry(
                "https://www.royensoc.co.uk/sites/default/files/Vol01_Part12+13.pdf",
                title="RES Handbook Vol 1 Parts 12 & 13. Mecoptera Megaloptera Neuroptera",
                entry_id="res-12-13",
            )
        )
        archive_candidates = await resolver.resolve(
            make_entry(
                "https://www.royensoc.co.uk/sites/default/files/Archive%204/Vol01_Part07.pdf",
                title="RES Handbook Vol 1 Part 7. Psocoptera",
                entry_id="res-7",
            )
        )
    finally:
        await resolver.aclose()

    assert media_route.call_count == 2
    assert media_route.calls[0].request.url.params["search"] == "Vol01_Part1213"
    assert media_route.calls[1].request.url.params["search"] == "Vol01_Part07"
    assert [c.url for c in plus_candidates] == [
        "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part1213.pdf"
    ]
    assert [c.url for c in archive_candidates] == [
        "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part07_0.pdf"
    ]


@respx.mock
async def test_royensoc_falls_back_to_product_api_and_ranks_matches() -> None:
    media_route = respx.get("https://www.royensoc.co.uk/wp-json/wp/v2/media").mock(
        return_value=httpx.Response(200, json=[])
    )
    product_route = respx.get("https://www.royensoc.co.uk/wp-json/wp/v2/product").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "slug": "unrelated-book",
                    "title": {"rendered": "Annual Report"},
                    "link": "https://www.royensoc.co.uk/shop/publications/annual-report/",
                },
                {
                    "slug": "thysanoptera",
                    "title": {"rendered": "Thysanoptera"},
                    "link": "https://www.royensoc.co.uk/shop/publications/out-of-print-handbooks/thysanoptera/",
                },
                {
                    "slug": "thysanoptera-notes",
                    "title": {"rendered": "Thysanoptera Notes"},
                    "link": "https://www.royensoc.co.uk/shop/publications/handbooks/thysanoptera-notes/",
                },
            ],
        )
    )

    resolver = PublisherPreResolver()
    try:
        candidates = await resolver.resolve(
            make_entry(
                "https://www.royensoc.co.uk/sites/default/files/Vol01_Part11.pdf",
                title="RES Handbook Vol 1 Part 11. Thysanoptera",
                entry_id="res-11",
            )
        )
    finally:
        await resolver.aclose()

    assert media_route.call_count == 1
    assert product_route.call_count == 1
    assert product_route.calls[0].request.url.params["search"] == "Thysanoptera"
    assert candidates[0].url == (
        "https://www.royensoc.co.uk/shop/publications/out-of-print-handbooks/thysanoptera/"
    )
    assert all("unrelated-book" not in c.url for c in candidates)


@respx.mock
async def test_royensoc_caches_queries_across_concurrent_calls() -> None:
    media_route = respx.get("https://www.royensoc.co.uk/wp-json/wp/v2/media").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "slug": "vol01_part10",
                    "title": {"rendered": "Vol01_Part10"},
                    "mime_type": "application/pdf",
                    "source_url": "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part10.pdf",
                }
            ],
        )
    )

    resolver = PublisherPreResolver()
    try:
        first, second = await asyncio.gather(
            resolver.resolve(
                make_entry(
                    "https://www.royensoc.co.uk/sites/default/files/Vol01_Part10.pdf",
                    title="RES Handbook Vol 1 Part 10. Odonata",
                    entry_id="res-10-a",
                )
            ),
            resolver.resolve(
                make_entry(
                    "https://www.royensoc.co.uk/sites/default/files/Archive%202/Vol01_Part10.pdf",
                    title="RES Handbook Vol 1 Part 10. Odonata",
                    entry_id="res-10-b",
                )
            ),
        )
    finally:
        await resolver.aclose()

    assert media_route.call_count == 1
    assert [c.url for c in first] == [
        "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part10.pdf"
    ]
    assert [c.url for c in second] == [
        "https://www.royensoc.co.uk/wp-content/uploads/2021/12/Vol01_Part10.pdf"
    ]


@respx.mock
async def test_royensoc_returns_empty_on_http_and_json_errors() -> None:
    respx.get("https://www.royensoc.co.uk/wp-json/wp/v2/media").mock(
        return_value=httpx.Response(503)
    )
    respx.get("https://www.royensoc.co.uk/wp-json/wp/v2/product").mock(
        return_value=httpx.Response(200, text="not-json")
    )

    resolver = PublisherPreResolver()
    try:
        candidates = await resolver.resolve(
            make_entry(
                "https://www.royensoc.co.uk/sites/default/files/Vol01_Part10.pdf",
                title="RES Handbook Vol 1 Part 10. Odonata",
            )
        )
    finally:
        await resolver.aclose()

    assert candidates == []


@respx.mock
async def test_mikes_insect_keys_resolves_from_404_home_nav_and_ranks_leaf_above_section() -> (
    None
):
    home_html = """
    <html>
      <title>Mikes Insect Keys</title>
      <body>
        <nav>
          <a href="/view/mikes-insect-keys/mikes-insect-keys">Mike&#39;s Insect Keys</a>
          <a href="/view/mikes-insect-keys/mikes-insect-keys/keys-for-the-identification-of-british-beetles-coleoptera">Keys for the identification of British beetles (Coleoptera)</a>
          <a href="/view/mikes-insect-keys/mikes-insect-keys/keys-for-the-identification-of-british-beetles-coleoptera/key-for-the-identification-of-british-buprestidae?authuser=0">Key for the identification of British Buprestidae</a>
          <a href="https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys/keys-for-the-identification-of-british-beetles-coleoptera/keys-for-the-identification-of-british-elateridae">Keys for the identification of British Elateridae</a>
        </nav>
      </body>
    </html>
    """
    home_route = respx.get("https://sites.google.com/view/mikes-insect-keys/home").mock(
        return_value=httpx.Response(404, text=home_html)
    )

    resolver = PublisherPreResolver()
    try:
        buprestidae = await resolver.resolve(
            make_entry(
                "https://sites.google.com/site/mikesinsectkeys/Home/keys-to-coleoptera/buprestidae",
                title="A key to the British species of family Buprestidae (Coleoptera)",
                entry_id="mikes-buprestidae",
            )
        )
        elateridae = await resolver.resolve(
            make_entry(
                "https://sites.google.com/site/mikesinsectkeys2/home/nematocera/keys-for-the-identification-of-british-elateridae",
                title="Keys for the identification of British Elateridae",
                entry_id="mikes-elateridae",
            )
        )
    finally:
        await resolver.aclose()

    assert home_route.call_count == 1
    assert buprestidae[0].url == (
        "https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys/"
        "keys-for-the-identification-of-british-beetles-coleoptera/"
        "key-for-the-identification-of-british-buprestidae"
    )
    assert [c.url for c in elateridae] == [
        "https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys/"
        "keys-for-the-identification-of-british-beetles-coleoptera/"
        "keys-for-the-identification-of-british-elateridae"
    ]


@respx.mock
async def test_mikes_insect_keys_falls_back_to_root_and_taxon_subindex() -> None:
    home_route = respx.get("https://sites.google.com/view/mikes-insect-keys/home").mock(
        return_value=httpx.Response(404, text="<html><body>Not found</body></html>")
    )
    root_route = respx.get(
        "https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys"
    ).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<a href="/view/mikes-insect-keys/mikes-insect-keys/coleoptera">'
                "Coleoptera keys</a>"
            ),
        )
    )
    sub_route = respx.get(
        "https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys/coleoptera"
    ).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<a href="/view/mikes-insect-keys/mikes-insect-keys/coleoptera/'
                'key-for-the-identification-of-british-aderidae">'
                "Key for the identification of British Aderidae</a>"
            ),
        )
    )

    resolver = PublisherPreResolver()
    try:
        first, second = await asyncio.gather(
            resolver.resolve(
                make_entry(
                    "https://sites.google.com/site/mikesinsectkeys/Home/keys-to-coleoptera/aderidae",
                    title="Key to the British Species of Aderidae",
                    entry_id="mikes-aderidae-1",
                )
            ),
            resolver.resolve(
                make_entry(
                    "https://sites.google.com/site/mikesinsectkeys/Home/keys-to-coleoptera/aderidae",
                    title="Key to the British Species of Aderidae",
                    entry_id="mikes-aderidae-2",
                )
            ),
        )
    finally:
        await resolver.aclose()

    assert home_route.call_count == 1
    assert root_route.call_count == 1
    assert sub_route.call_count == 1
    expected = (
        "https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys/"
        "coleoptera/key-for-the-identification-of-british-aderidae"
    )
    assert [c.url for c in first] == [expected]
    assert [c.url for c in second] == [expected]


@respx.mock
async def test_mikes_insect_keys_ignores_other_sites_and_handles_errors() -> None:
    respx.get("https://sites.google.com/view/mikes-insect-keys/home").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys").mock(
        return_value=httpx.Response(503)
    )

    resolver = PublisherPreResolver()
    try:
        other = await resolver.resolve(
            make_entry("https://sites.google.com/site/otherproject/home")
        )
        already_migrated = await resolver.resolve(
            make_entry("https://sites.google.com/view/mikes-insect-keys/home")
        )
        failed = await resolver.resolve(
            make_entry(
                "https://sites.google.com/site/mikesinsectkeys/Home/keys-to-coleoptera/aderidae",
                title="Key to the British Species of Aderidae",
            )
        )
    finally:
        await resolver.aclose()

    assert other == []
    assert already_migrated == []
    assert failed == []
