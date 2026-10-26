"""CLI smoke tests for the initial package scaffold."""

from __future__ import annotations

import pytest

from idsignpost.cli import main

COMMANDS = ("extract", "verify", "export", "run")


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in COMMANDS:
        assert command in output


@pytest.mark.parametrize("command", COMMANDS)
def test_stub_commands_exit_nonzero(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command]) == 1
    assert "not implemented" in capsys.readouterr().err
