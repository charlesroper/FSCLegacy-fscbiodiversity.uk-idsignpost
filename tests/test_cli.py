"""CLI smoke tests for the package."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from idsignpost.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
STUB_COMMANDS = ("verify", "export", "run")


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("extract", *STUB_COMMANDS):
        assert command in output


@pytest.mark.parametrize("command", STUB_COMMANDS)
def test_stub_commands_exit_nonzero(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command]) == 1
    assert "not implemented" in capsys.readouterr().err


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
