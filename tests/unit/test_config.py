"""Configuration loading and validation."""

import os
from pathlib import Path

import pytest

from pgharbor.config import (
    Access,
    Approval,
    Config,
    ConnectionConfig,
    FunctionPolicy,
    load_config,
    parse_duration_ms,
)
from pgharbor.errors import ConfigError

FULL = """
default_connection = "dev"

[defaults]
statement_timeout = "5s"
max_rows = 50

[connections.dev]
description = "Development"
host = "localhost"
port = 5433
dbname = "app"
user_env = "T_USER"
password_env = "T_PASSWORD"

[connections.dev.params]
sslmode = "require"

[connections.prod]
environment = "production"
dsn_env = "T_DSN"
statement_timeout = "10s"
max_plan_cost = 100000
function_policy = "strict"
allow_functions = ["public.fmt_*"]

[[connections.prod.protect]]
table = "billing.cards"
deny_columns = ["number", "cvv"]
require_index_filter = true
note = "Filter by id."

[audit]
enabled = false

[tools]
disabled = ["pg_export_query"]

[custom_tools.orders_of]
description = "Orders of one customer"
sql = "SELECT id FROM orders WHERE customer_id = $1"
connections = ["dev"]

[[custom_tools.orders_of.params]]
name = "customer_id"
type = "integer"
"""


def write(tmp_path: Path, text: str) -> Path:
    file = tmp_path / "config.toml"
    file.write_text(text)
    return file


def test_full_file(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, FULL))
    dev = config.connection("dev")
    assert dev.statement_timeout == 5000  # from [defaults]
    assert dev.max_rows == 50
    assert dev.access is Access.READ_ONLY
    assert dev.effective_approval is Approval.NEVER
    prod = config.connection("prod")
    assert prod.statement_timeout == 10_000  # the connection wins over [defaults]
    assert prod.is_production
    assert prod.effective_approval is Approval.SESSION
    assert prod.function_policy is FunctionPolicy.STRICT
    rule = prod.protection_for("billing", "cards")
    assert rule is not None
    assert rule.deny_columns == ("number", "cvv")
    assert prod.protection_for("billing", "other") is None
    assert config.connection(None).name == "dev"  # default_connection
    assert config.tools.disabled == ("pg_export_query",)
    assert config.custom_tools["orders_of"].params[0].type == "integer"
    assert config.audit.enabled is False


def test_secrets_are_read_from_the_environment_at_connect_time(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, FULL))
    dev = config.connection("dev")
    with pytest.raises(ConfigError, match="T_USER is not set"):
        dev.conninfo({})
    dsn, kwargs = dev.conninfo({"T_USER": "app", "T_PASSWORD": "s3cret"})
    assert dsn == ""
    assert kwargs["user"] == "app"
    assert kwargs["password"] == "s3cret"
    assert kwargs["sslmode"] == "require"
    assert kwargs["port"] == 5433
    assert "s3cret" not in dev.target()
    prod = config.connection("prod")
    dsn, _ = prod.conninfo({"T_DSN": "postgresql://u:p@h/db"})
    assert dsn == "postgresql://u:p@h/db"
    assert prod.target() == "DSN from $T_DSN"


def test_password_file(tmp_path: Path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    profile = ConnectionConfig(name="a", host="h", password_file=str(secret))
    assert profile.conninfo({})[1]["password"] == "from-file"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[connections.Bad_Name]\nhost = 'x'", "lowercase"),
        ("[connections.a]\naccess = 'root'", "access"),
        ("[connections.a]\nunknown_key = 1", "unknown_key"),
        ("[connections.a]\ndsn = 'x'\ndsn_env = 'Y'", "either dsn or dsn_env"),
        ("[connections.a]\npassword = 'x'\npassword_env = 'Y'", "only one of"),
        ("[connections.a]\nmax_rows = 10\nmax_rows_limit = 5", "max_rows cannot be greater"),
        ("[connections.a]\nstatement_timeout = 'soon'", "invalid duration"),
        ("[connections.a.params]\noptions = '-c x=1'", "unsupported connection parameter"),
        ("[connections.a]\nredact_columns = ['(']", "invalid regular expression"),
        ("[connections.a]\n[[connections.a.protect]]\ntable = 'nodot'", "schema.table"),
        ("[defaults]\nhost = 'x'", "cannot set host"),
        ("default_connection = 'nope'\n[connections.a]\nhost = 'x'", "is not defined"),
        ("[surprise]\na = 1", "unknown top-level key"),
        (
            "[connections.a]\nhost = 'x'\n[custom_tools.tt]\ndescription='d'\nsql='s'\nconnections=['z']",
            "unknown connection",
        ),
        ("not toml [", "invalid TOML"),
    ],
)
def test_invalid_files_are_explained(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(write(tmp_path, text))
    assert message in str(caught.value)


def test_missing_explicit_file() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/pgharbor.toml")


def test_environment_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no file there
    monkeypatch.delenv("PGHARBOR_CONFIG", raising=False)
    for name in ("PGHARBOR_DSN", "DATABASE_URL", "PGHOST", "PGDATABASE", "PGSERVICE", "PGUSER"):
        monkeypatch.delenv(name, raising=False)
    empty = load_config()
    assert empty.connections == {}
    with pytest.raises(ConfigError, match="No connection is configured"):
        empty.connection(None)

    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
    config = load_config()
    profile = config.connection(None)
    assert profile.name == "default"
    assert profile.read_only
    assert profile.dsn_env == "DATABASE_URL"

    monkeypatch.setenv("PGHARBOR_DSN", "postgresql://u@h/other")
    monkeypatch.setenv("PGHARBOR_ACCESS", "read-write")
    monkeypatch.setenv("PGHARBOR_ENVIRONMENT", "production")
    profile = load_config().connection(None)
    assert profile.dsn_env == "PGHARBOR_DSN"
    assert profile.access is Access.READ_WRITE
    assert profile.effective_approval is Approval.SESSION


def test_a_file_in_the_current_directory_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("PGHARBOR_CONFIG", raising=False)
    for name in ("PGHARBOR_DSN", "DATABASE_URL", "PGHOST", "PGDATABASE", "PGSERVICE", "PGUSER"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "pgharbor.toml").write_text(
        "[connections.evil]\nhost = 'attacker'\naccess = 'admin'"
    )
    (tmp_path / "config.toml").write_text("[connections.evil]\nhost = 'attacker'\naccess = 'admin'")
    monkeypatch.chdir(tmp_path)
    assert load_config().connections == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_readable_file_with_a_password_gets_a_warning(tmp_path: Path) -> None:
    file = write(tmp_path, "[connections.a]\nhost = 'h'\npassword = 'plain'")
    file.chmod(0o644)
    assert any("chmod 600" in warning for warning in load_config(file).warnings)
    file.chmod(0o600)
    assert load_config(file).warnings == ()


def test_several_connections_need_an_explicit_choice() -> None:
    config = Config.model_validate({"connections": {"a": {"name": "a"}, "b": {"name": "b"}}})
    with pytest.raises(ConfigError, match="Several connections"):
        config.connection(None)
    with pytest.raises(ConfigError, match="Available: a, b"):
        config.connection("c")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1500, 1500),
        ("1500", 1500),
        ("30s", 30_000),
        ("500ms", 500),
        ("2min", 120_000),
        ("1.5s", 1500),
        ("1h", 3_600_000),
    ],
)
def test_durations(value: object, expected: int) -> None:
    assert parse_duration_ms(value) == expected


@pytest.mark.parametrize("value", ["soon", "-5s", True, None, "10 days"])
def test_bad_durations(value: object) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration_ms(value)


def test_protection_patterns() -> None:
    profile = ConnectionConfig(
        name="a",
        protect=[{"table": "audit.*", "deny_columns": ["payload"]}],  # type: ignore[list-item]
    )
    assert profile.protection_for("audit", "events") is not None
    assert profile.protection_for("public", "events") is None
