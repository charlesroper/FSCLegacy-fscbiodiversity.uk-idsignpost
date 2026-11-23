"""CLI smoke tests for the package."""

from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any

import pytest

from idsignpost.cache import CatalogCache
from idsignpost.cli import _load_env_file, build_parser, main
from idsignpost.gemini_finder import Candidate, GenerationResult, RedirectVerdict
from idsignpost.http_checker import CheckResult
from idsignpost.models import CatalogEntry
from idsignpost.pipeline import VerifyDeps

FIXTURES = Path(__file__).parent / "fixtures"


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("extract", "verify", "export", "run", "cost"):
        assert command in output


def test_extract_limit_prints_sample_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extract", "--html-dir", str(FIXTURES), "--limit", "1"]) == 0

    entries = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in entries] == ["flickr-split"]


def test_extract_writes_catalog_file(tmp_path: Path) -> None:
    output = tmp_path / "idsignpost.json"

    assert main(["extract", "--html-dir", str(FIXTURES), "--output", str(output)]) == 0

    entries = json.loads(output.read_text(encoding="utf-8"))
    assert [entry["id"] for entry in entries] == ["flickr-split", "full-entry"]


def test_extract_limit_zero_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extract", "--html-dir", str(FIXTURES), "--limit", "0"]) == 2
    assert "--limit must be at least 1" in capsys.readouterr().err


def test_export_renders_three_formats_from_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "idsignpost.json"
    assert main(["extract", "--html-dir", str(FIXTURES), "--output", str(catalog)]) == 0

    csv_path = tmp_path / "idsignpost.csv"
    parquet_path = tmp_path / "idsignpost.parquet"
    md_path = tmp_path / "idsignpost.md"
    assert (
        main(
            [
                "export",
                "--input",
                str(catalog),
                "--csv",
                str(csv_path),
                "--parquet",
                str(parquet_path),
                "--md",
                str(md_path),
            ]
        )
        == 0
    )

    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("id,filename,title,")
    assert md_path.read_text(encoding="utf-8").startswith("## flickr-split.html\n")
    assert parquet_path.stat().st_size > 0


def make_entry(
    entry_id: str, state: str | None = None, **overrides: object
) -> CatalogEntry:
    data: dict[str, object] = {
        "id": entry_id,
        "filename": f"{entry_id}.html",
        "title": f"Guide {entry_id}",
        "web_link": f"https://example.org/{entry_id}",
        "web_link_state": state,
    }
    data.update(overrides)
    return CatalogEntry.model_validate(data)


def write_catalog(path: Path, entries: list[CatalogEntry]) -> None:
    path.write_text(
        json.dumps([entry.model_dump() for entry in entries], indent=2),
        encoding="utf-8",
    )


def read_catalog(path: Path) -> list[CatalogEntry]:
    return [
        CatalogEntry.model_validate(item)
        for item in json.loads(path.read_text(encoding="utf-8"))
    ]


def public_resolver(host: str, port: Any) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class FakeChecker:
    def __init__(
        self,
        results: dict[str, CheckResult] | None = None,
        *,
        abort: bool = False,
        default: CheckResult | None = None,
        interrupt_on: str | None = None,
    ) -> None:
        self.results = results or {}
        self.default = default or CheckResult("200", "ok")
        self.abort = abort
        self.interrupt_on = interrupt_on
        self.checked: list[str] = []

    async def check(self, url: str, *, entry_title: str | None = None) -> CheckResult:
        self.checked.append(url)
        if self.interrupt_on is not None and url == self.interrupt_on:
            raise KeyboardInterrupt
        return self.results.get(url, self.default)

    def should_abort(self) -> bool:
        return self.abort


class FakeFinder:
    async def verify_redirect(self, **kwargs: object) -> RedirectVerdict | None:
        return RedirectVerdict(True, "matches")

    async def discover(
        self, entry: CatalogEntry, *, hint: str | None = None
    ) -> list[Candidate] | None:
        return []

    async def confirm_alternative(
        self,
        entry: CatalogEntry,
        *,
        url: str,
        page_title: str | None,
        page_h1: str | None,
        page_snippet: str | None = None,
    ) -> RedirectVerdict | None:
        return RedirectVerdict(True, "matches")


class FakeArchive:
    async def lookup(self, url: str) -> str | None:
        return None

    async def save(self, url: str) -> str | None:
        return None


class FakeGenerator:
    def __init__(self, script: list[GenerationResult]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, bool]] = []

    async def generate(
        self, prompt: str, *, model: str, json_mode: bool, search: bool
    ) -> GenerationResult:
        self.calls.append((model, search))
        return self.script.pop(0)


def test_verify_requires_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a")])

    assert main(["verify", "--input", str(catalog), "--output", str(catalog)]) == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_save_archive_requires_ia_keys_unless_no_ia_keys_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.delenv("IA_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IA_SECRET_KEY", raising=False)
    catalog = tmp_path / "idsignpost.json"
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    write_catalog(catalog, [make_entry("a")])

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--save-archive",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "IA_ACCESS_KEY and IA_SECRET_KEY" in err
    assert "--no-ia-keys" in err

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(cache_file),
                "--cost-file",
                str(cost_file),
                "--save-archive",
                "--no-ia-keys",
            ],
            deps=VerifyDeps(
                checker=FakeChecker(),
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )

    monkeypatch.setenv("IA_ACCESS_KEY", "ia-access")
    monkeypatch.setenv("IA_SECRET_KEY", "ia-secret")
    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(cache_file),
                "--cost-file",
                str(cost_file),
                "--save-archive",
            ],
            deps=VerifyDeps(
                checker=FakeChecker(),
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )


def test_verify_merges_results_into_the_full_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    write_catalog(catalog, [make_entry("a"), make_entry("b")])

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(cache_file),
                "--cost-file",
                str(cost_file),
            ],
            deps=VerifyDeps(
                checker=FakeChecker(),
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )

    entries = read_catalog(catalog)
    assert [entry.id for entry in entries] == ["a", "b"]
    assert all(entry.web_link_state == "ok" for entry in entries)
    assert cache_file.exists()
    assert cost_file.exists()


def test_sample_verifies_a_subset_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(
        catalog,
        [make_entry(f"entry-{index}", "ok") for index in range(6)],
    )
    checker = FakeChecker()

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(tmp_path / "cache.json"),
                "--cost-file",
                str(tmp_path / "costs.json"),
                "--sample",
                "2",
            ],
            deps=VerifyDeps(
                checker=checker,
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )

    assert len(checker.checked) == 2
    assert len(read_catalog(catalog)) == 6


def test_only_broken_is_applied_before_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(
        catalog,
        [
            make_entry("ok-one", "ok"),
            make_entry("broken-one", "broken"),
            make_entry("broken-two", "broken"),
        ],
    )
    checker = FakeChecker()

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(tmp_path / "cache.json"),
                "--cost-file",
                str(tmp_path / "costs.json"),
                "--only-broken",
                "--limit",
                "1",
            ],
            deps=VerifyDeps(
                checker=checker,
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )

    assert checker.checked == ["https://example.org/broken-one"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--sample", "2", "--limit", "1"], "mutually exclusive"),
        (["--sample", "0"], "--sample must be at least 1"),
        (["--min-prob", "1.5"], "--min-prob"),
        (["--concurrency", "0"], "--concurrency"),
    ],
)
def test_verify_usage_errors_return_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    message: str,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a")])

    assert main(["verify", "--input", str(catalog), *extra]) == 2
    assert message in capsys.readouterr().err


def test_verify_offline_abort_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    write_catalog(catalog, [make_entry("a")])

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(cache_file),
                "--cost-file",
                str(cost_file),
            ],
            deps=VerifyDeps(
                checker=FakeChecker(abort=True),
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 1
    )
    assert "aborted" in capsys.readouterr().err
    assert not cache_file.exists()
    assert not cost_file.exists()


def test_verify_rejects_a_legacy_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    catalog.write_text(json.dumps([{"title": "legacy"}]), encoding="utf-8")

    assert main(["verify", "--input", str(catalog), "--output", str(catalog)]) == 1
    assert "cannot load" in capsys.readouterr().err


def test_run_extracts_verifies_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    output = tmp_path / "idsignpost.json"
    csv_path = tmp_path / "idsignpost.csv"
    parquet_path = tmp_path / "idsignpost.parquet"
    md_path = tmp_path / "idsignpost.md"

    assert (
        main(
            [
                "run",
                "--html-dir",
                str(FIXTURES),
                "--output",
                str(output),
                "--cache-file",
                str(tmp_path / "cache.json"),
                "--cost-file",
                str(tmp_path / "costs.json"),
                "--csv",
                str(csv_path),
                "--parquet",
                str(parquet_path),
                "--md",
                str(md_path),
                "--limit",
                "1",
            ],
            deps=VerifyDeps(
                checker=FakeChecker(),
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )

    entries = read_catalog(output)
    assert [entry.id for entry in entries] == ["flickr-split", "full-entry"]
    assert sum(entry.web_link_state == "ok" for entry in entries) == 1
    assert csv_path.exists() and parquet_path.exists() and md_path.exists()


def test_extract_replaces_a_legacy_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "idsignpost.json"
    output.write_text(json.dumps([{"Title": "legacy"}]), encoding="utf-8")

    assert main(["extract", "--html-dir", str(FIXTURES), "--output", str(output)]) == 0

    assert "not a canonical catalog" in capsys.readouterr().err
    entries = read_catalog(output)
    assert [entry.id for entry in entries] == ["flickr-split", "full-entry"]


def test_run_replaces_a_legacy_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    output = tmp_path / "idsignpost.json"
    output.write_text(json.dumps([{"Title": "legacy"}]), encoding="utf-8")

    assert (
        main(
            [
                "run",
                "--html-dir",
                str(FIXTURES),
                "--output",
                str(output),
                "--cache-file",
                str(tmp_path / "cache.json"),
                "--cost-file",
                str(tmp_path / "costs.json"),
                "--csv",
                str(tmp_path / "out.csv"),
                "--parquet",
                str(tmp_path / "out.parquet"),
                "--md",
                str(tmp_path / "out.md"),
            ],
            deps=VerifyDeps(
                checker=FakeChecker(),
                finder=FakeFinder(),
                wayback=FakeArchive(),
            ),
        )
        == 0
    )

    assert "not a canonical catalog" in capsys.readouterr().err
    assert [entry.id for entry in read_catalog(output)] == [
        "flickr-split",
        "full-entry",
    ]


def test_cost_command_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cost_file = tmp_path / "costs.json"
    assert main(["cost", "--cost-file", str(cost_file)]) == 0
    assert "No recorded runs in the current session." in capsys.readouterr().out


def test_cost_command_with_runs_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from idsignpost.cost import SessionCostTracker

    cost_file = tmp_path / "costs.json"
    tracker = SessionCostTracker(path=cost_file)
    tracker.record_call(
        operation="candidate_discovery",
        model="gemini-3.1-flash-lite",
        prompt_tokens=800,
        output_tokens=150,
        search_queries=1,
    )
    tracker.finish_run(command="verify")

    # Aligned table output
    assert main(["cost", "--cost-file", str(cost_file)]) == 0
    table_out = capsys.readouterr().out
    assert "Session started:" in table_out
    assert "verify" in table_out

    # JSON export output
    assert main(["cost", "--cost-file", str(cost_file), "--json"]) == 0
    json_out = capsys.readouterr().out
    data = json.loads(json_out)
    assert data["version"] == 1
    assert len(data["runs"]) == 1
    assert data["runs"][0]["command"] == "verify"


def test_cost_command_reset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from idsignpost.cost import SessionCostTracker

    cost_file = tmp_path / "costs.json"
    tracker = SessionCostTracker(path=cost_file)
    tracker.record_call(
        operation="redirect_verification",
        model="gemini-3.1-flash-lite",
        prompt_tokens=400,
        output_tokens=40,
    )
    tracker.finish_run(command="verify")

    assert main(["cost", "--cost-file", str(cost_file), "--reset"]) == 0
    assert "session history reset" in capsys.readouterr().err

    # Table is now empty
    assert main(["cost", "--cost-file", str(cost_file)]) == 0
    assert "No recorded runs in the current session." in capsys.readouterr().out


def test_verify_max_searches_negative_returns_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a")])

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cost-file",
                str(tmp_path / "costs.json"),
                "--max-searches",
                "-1",
            ]
        )
        == 2
    )
    assert "--max-searches must not be negative" in capsys.readouterr().err


def test_verify_wires_models_max_searches_cache_and_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    alt_url = "https://replacement.example/guide"
    write_catalog(
        catalog,
        [
            make_entry("redir", web_link="https://example.org/moved"),
            make_entry("broken-one", web_link="https://example.org/broken-1"),
            make_entry("broken-two", web_link="https://example.org/broken-2"),
        ],
    )
    checker = FakeChecker(
        {
            "https://example.org/moved": CheckResult(
                "200",
                "ok",
                resolved_url="https://other.example/guide",
                is_suspicious_redirect=True,
            ),
            "https://example.org/broken-1": CheckResult("404", "broken"),
            "https://example.org/broken-2": CheckResult("404", "broken"),
            alt_url: CheckResult("200", "ok"),
        }
    )
    generator = FakeGenerator(
        [
            GenerationResult(
                text='{"is_match": true, "reasoning": "Same guide."}',
                prompt_tokens=100,
                output_tokens=20,
            ),
            GenerationResult(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "url": alt_url,
                                "probability": 0.9,
                                "reasoning": "Official page.",
                            }
                        ]
                    }
                ),
                prompt_tokens=200,
                output_tokens=40,
                search_queries=1,
            ),
            GenerationResult(
                text='{"is_match": true, "reasoning": "Confirmed."}',
                prompt_tokens=150,
                output_tokens=30,
            ),
        ]
    )

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--cache-file",
                str(cache_file),
                "--cost-file",
                str(cost_file),
                "--concurrency",
                "1",
                "--verification-model",
                "gemini-3.1-flash-lite",
                "--discovery-model",
                "gemini-3.8-flash",
                "--max-searches",
                "1",
            ],
            deps=VerifyDeps(
                checker=checker,
                wayback=FakeArchive(),
                generator=generator,
                resolver=public_resolver,
            ),
        )
        == 0
    )

    assert generator.calls == [
        ("gemini-3.1-flash-lite", False),
        ("gemini-3.8-flash", True),
        ("gemini-3.1-flash-lite", False),
    ]
    entries = {entry.id: entry for entry in read_catalog(catalog)}
    assert entries["redir"].web_link_state == "ok"
    assert entries["broken-one"].alternative_link == alt_url
    assert entries["broken-two"].alternative_link is None
    assert entries["broken-two"].alternative_note == "search failed"
    assert len(CatalogCache.load(cache_file).urls) == 4
    cost_data = json.loads(cost_file.read_text(encoding="utf-8"))
    assert cost_data["runs"][0]["calls"] == 3
    assert cost_data["runs"][0]["search_queries"] == 1
    assert "idsignpost verify cost breakdown:" in capsys.readouterr().err


def test_verify_keyboard_interrupt_saves_cache_and_returns_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    write_catalog(
        catalog,
        [
            make_entry("first", web_link="https://example.org/first"),
            make_entry("second", web_link="https://example.org/second"),
        ],
    )
    checker = FakeChecker(interrupt_on="https://example.org/second")

    code = main(
        [
            "verify",
            "--input",
            str(catalog),
            "--output",
            str(catalog),
            "--cache-file",
            str(cache_file),
            "--cost-file",
            str(cost_file),
            "--concurrency",
            "1",
        ],
        deps=VerifyDeps(
            checker=checker,
            finder=FakeFinder(),
            wayback=FakeArchive(),
        ),
    )

    assert code == 130
    err = capsys.readouterr().err
    assert "idsignpost verify: interrupted, URL cache saved" in err
    assert "Traceback" not in err
    assert cache_file.exists()
    assert (
        CatalogCache.load(cache_file).get_check("https://example.org/first") is not None
    )


def test_run_keyboard_interrupt_saves_cache_and_returns_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    csv_path = tmp_path / "out.csv"
    parquet_path = tmp_path / "out.parquet"
    md_path = tmp_path / "out.md"
    checker = FakeChecker(
        interrupt_on="https://www.fscbiodiversity.uk/resources/guide.pdf"
    )

    code = main(
        [
            "run",
            "--html-dir",
            str(FIXTURES),
            "--output",
            str(tmp_path / "idsignpost.json"),
            "--cache-file",
            str(cache_file),
            "--cost-file",
            str(cost_file),
            "--csv",
            str(csv_path),
            "--parquet",
            str(parquet_path),
            "--md",
            str(md_path),
            "--concurrency",
            "1",
        ],
        deps=VerifyDeps(
            checker=checker,
            finder=FakeFinder(),
            wayback=FakeArchive(),
        ),
    )

    assert code == 130
    assert "idsignpost run: interrupted, URL cache saved" in capsys.readouterr().err
    assert cache_file.exists()
    assert not csv_path.exists()
    assert not parquet_path.exists()
    assert not md_path.exists()


def test_export_missing_and_corrupt_files_return_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"
    assert main(["export", "--input", str(missing)]) == 1
    assert "idsignpost export:" in capsys.readouterr().err

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    assert main(["export", "--input", str(corrupt)]) == 1
    assert "idsignpost export:" in capsys.readouterr().err


def test_export_unwritable_destination_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a")])
    existing_dir = tmp_path / "out_dir"
    existing_dir.mkdir()

    code = main(
        [
            "export",
            "--input",
            str(catalog),
            "--csv",
            str(existing_dir),
            "--parquet",
            str(tmp_path / "out.parquet"),
            "--md",
            str(tmp_path / "out.md"),
        ]
    )

    assert code == 1
    assert "idsignpost export:" in capsys.readouterr().err


def test_cost_reset_oserror_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing_dir = tmp_path / "cost_dir"
    existing_dir.mkdir()

    code = main(["cost", "--cost-file", str(existing_dir), "--reset"])

    assert code == 1
    assert "idsignpost cost:" in capsys.readouterr().err


def test_cost_inspect_corrupt_and_directory_files_return_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    assert main(["cost", "--cost-file", str(corrupt)]) == 1
    assert "idsignpost cost:" in capsys.readouterr().err

    assert main(["cost", "--cost-file", str(corrupt), "--json"]) == 1
    assert "idsignpost cost:" in capsys.readouterr().err

    non_dict = tmp_path / "non_dict.json"
    non_dict.write_text("[1, 2, 3]", encoding="utf-8")
    assert main(["cost", "--cost-file", str(non_dict)]) == 1
    assert "idsignpost cost:" in capsys.readouterr().err

    existing_dir = tmp_path / "cost_dir"
    existing_dir.mkdir()
    assert main(["cost", "--cost-file", str(existing_dir)]) == 1
    assert "idsignpost cost:" in capsys.readouterr().err

    assert main(["cost", "--cost-file", str(corrupt), "--reset"]) == 0
    assert "session history reset" in capsys.readouterr().err


def test_load_env_file_supports_export_and_inline_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "GEMINI_API_KEY",
        "UNQUOTED_COMMENT",
        "UNQUOTED_HASH",
        "DOUBLE_QUOTED",
        "SINGLE_QUOTED",
        "EXPORT_LIKE",
        "export_prefix",
        "EXISTING_VAR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXISTING_VAR", "already-set")

    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# full line comment",
                "",
                "export GEMINI_API_KEY=secret-key # trailing comment",
                "  export   UNQUOTED_COMMENT = plain-value # another comment",
                "UNQUOTED_HASH=bare#comment",
                'DOUBLE_QUOTED="abc # 123" # trailing comment',
                "SINGLE_QUOTED='xyz#456' # trailing comment",
                "EXPORT_LIKE=kept",
                "export_prefix=also-kept",
                "EXISTING_VAR=new-value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    import os

    _load_env_file()
    assert os.environ["GEMINI_API_KEY"] == "secret-key"
    assert os.environ["UNQUOTED_COMMENT"] == "plain-value"
    assert os.environ["UNQUOTED_HASH"] == "bare"
    assert os.environ["DOUBLE_QUOTED"] == "abc # 123"
    assert os.environ["SINGLE_QUOTED"] == "xyz#456"
    assert os.environ["EXPORT_LIKE"] == "kept"
    assert os.environ["export_prefix"] == "also-kept"
    assert os.environ["EXISTING_VAR"] == "already-set"


@pytest.mark.parametrize(
    ("command", "expected_flags"),
    [
        ("extract", ["--html-dir", "--output", "--limit"]),
        (
            "verify",
            [
                "--input",
                "--output",
                "--cache-file",
                "--cache-ttl-days",
                "--cost-file",
                "--sample",
                "--seed",
                "--limit",
                "--only-broken",
                "--save-archive",
                "--no-ia-keys",
                "--refresh",
                "--concurrency",
                "--wayback-concurrency",
                "--gemini-concurrency",
                "--timeout",
                "--min-prob",
                "--model",
                "--verification-model",
                "--discovery-model",
                "--max-searches",
                "--verbose",
            ],
        ),
        ("export", ["--input", "--csv", "--parquet", "--md"]),
        (
            "run",
            [
                "--html-dir",
                "--output",
                "--cache-file",
                "--cache-ttl-days",
                "--cost-file",
                "--sample",
                "--seed",
                "--limit",
                "--only-broken",
                "--save-archive",
                "--no-ia-keys",
                "--refresh",
                "--concurrency",
                "--wayback-concurrency",
                "--gemini-concurrency",
                "--timeout",
                "--min-prob",
                "--model",
                "--verification-model",
                "--discovery-model",
                "--max-searches",
                "--verbose",
                "--csv",
                "--parquet",
                "--md",
            ],
        ),
        ("cost", ["--cost-file", "--reset", "--json"]),
    ],
)
def test_subcommand_help_describes_all_flags(
    command: str, expected_flags: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
    )
    subparser = subparsers_action.choices[command]
    for action in subparser._actions:
        assert action.help and action.help.strip(), (
            f"{command} option {action.option_strings} is missing help text"
        )

    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for flag in expected_flags:
        assert flag in output
