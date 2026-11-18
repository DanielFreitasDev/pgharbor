"""The command line."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ..conftest import Server, write_config_file

pytestmark = pytest.mark.integration


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pgharbor.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )


def test_version_and_help() -> None:
    assert run("--version").stdout.startswith("pgharbor ")
    help_text = run("--help").stdout
    for command in ("serve", "init", "check", "tools", "ui", "config-path"):
        assert command in help_text


def test_init_writes_a_private_file_once(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.toml"
    first = run("--config", str(target), "init")
    assert first.returncode == 0, first.stderr
    assert target.is_file()
    assert target.stat().st_mode & 0o777 == 0o600
    assert "[connections.dev]" in target.read_text()
    second = run("--config", str(target), "init")
    assert second.returncode == 1
    assert "already exists" in second.stderr
    assert run("--config", str(target), "init", "--force").returncode == 0


def test_check_reports_each_connection(one_server: Server, tmp_path: Path) -> None:
    good = write_config_file(tmp_path / "good.toml", one_server)
    result = run("--config", str(good), "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok    ro" in result.stdout
    assert f"PostgreSQL {one_server.label}" in result.stdout
    assert "SUPERUSER" in result.stdout
    # Production is not opened just because someone ran a check.
    assert "skip  prod" in result.stdout
    assert "pgharbor check prod" in result.stdout
    named = run("--config", str(good), "check", "prod")
    assert "ok    prod" in named.stdout
    assert "ok    ro" not in named.stdout
    everything = run("--config", str(good), "check", "--all")
    assert "ok    prod" in everything.stdout
    assert "ok    ro" in everything.stdout
    unknown = run("--config", str(good), "check", "nope")
    assert unknown.returncode == 2
    assert "Unknown connection" in unknown.stderr
    # The test file holds a plain password and is group-readable: check says so.
    assert "chmod 600" in result.stdout

    bad = write_config_file(
        tmp_path / "bad.toml", one_server, broken={"password": "wrong", "connect_timeout": "2s"}
    )
    failed = run("--config", str(bad), "check")
    assert failed.returncode == 1
    assert "FAIL  broken" in failed.stdout
    assert "wrong" not in failed.stdout


def test_check_with_invalid_configuration(tmp_path: Path) -> None:
    file = tmp_path / "invalid.toml"
    file.write_text("[connections.a]\naccess = 'everything'\n")
    result = run("--config", str(file), "check")
    assert result.returncode == 2
    assert "invalid configuration" in result.stderr


def test_tools_lists_what_the_configuration_exposes(one_server: Server, tmp_path: Path) -> None:
    config = write_config_file(tmp_path / "config.toml", one_server)
    listing = run("--config", str(config), "tools").stdout
    assert run("tools", "--config", str(config)).stdout == listing  # either position works
    assert "pg_query" in listing
    assert "pg_execute" in listing
    assert "(writes)" in listing
    read_only = tmp_path / "ro.toml"
    read_only.write_text("[connections.a]\nhost = 'localhost'\n")
    listing = run("--config", str(read_only), "tools").stdout
    assert "pg_query" in listing
    assert "pg_execute" not in listing
