"""CLI smoke tests for the package."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from idsignpost import cli
from idsignpost.cli import main
from idsignpost.models import CatalogEntry
from idsignpost.pipeline import OfflineRunError, VerifyStats

FIXTURES = Path(__file__).parent / "fixtures"


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("extract", "verify", "export", "run"):
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


def fake_verification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seen: list[list[str]] | None = None,
    error: Exception | None = None,
) -> None:
    def hook(
        args: object, targets: list[CatalogEntry]
    ) -> tuple[list[CatalogEntry], VerifyStats]:
        if seen is not None:
            seen.append([entry.id for entry in targets])
        if error is not None:
            raise error
        updated = [
            entry.model_copy(update={"web_link_state": "ok", "web_link_status": "200"})
            for entry in targets
        ]
        return updated, VerifyStats(checked=len(updated), states={"ok": len(updated)})

    monkeypatch.setattr(cli, "_verify_catalog", hook)


def test_verify_requires_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a")])

    assert main(["verify", "--input", str(catalog), "--output", str(catalog)]) == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_verify_merges_results_into_the_full_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a"), make_entry("b")])
    fake_verification(monkeypatch)

    assert main(["verify", "--input", str(catalog), "--output", str(catalog)]) == 0

    entries = read_catalog(catalog)
    assert [entry.id for entry in entries] == ["a", "b"]
    assert all(entry.web_link_state == "ok" for entry in entries)


def test_sample_verifies_a_subset_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(
        catalog,
        [make_entry(f"entry-{index}", "ok") for index in range(6)],
    )
    seen: list[list[str]] = []
    fake_verification(monkeypatch, seen=seen)

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--sample",
                "2",
            ]
        )
        == 0
    )

    assert len(seen[0]) == 2
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
    seen: list[list[str]] = []
    fake_verification(monkeypatch, seen=seen)

    assert (
        main(
            [
                "verify",
                "--input",
                str(catalog),
                "--output",
                str(catalog),
                "--only-broken",
                "--limit",
                "1",
            ]
        )
        == 0
    )

    assert seen == [["broken-one"]]


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
    write_catalog(catalog, [make_entry("a")])
    fake_verification(
        monkeypatch, error=OfflineRunError("every check failed at the network level")
    )

    assert main(["verify", "--input", str(catalog), "--output", str(catalog)]) == 1
    assert "aborted" in capsys.readouterr().err


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
    fake_verification(monkeypatch)
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
                "--csv",
                str(csv_path),
                "--parquet",
                str(parquet_path),
                "--md",
                str(md_path),
                "--limit",
                "1",
            ]
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
    fake_verification(monkeypatch)
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
                "--csv",
                str(tmp_path / "out.csv"),
                "--parquet",
                str(tmp_path / "out.parquet"),
                "--md",
                str(tmp_path / "out.md"),
            ]
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


def test_verify_model_arguments_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    catalog = tmp_path / "idsignpost.json"
    write_catalog(catalog, [make_entry("a")])
    fake_verification(monkeypatch)

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
                "--verification-model",
                "gemini-3.1-flash-lite",
                "--discovery-model",
                "gemini-3.8-flash",
                "--max-searches",
                "10",
            ]
        )
        == 0
    )
