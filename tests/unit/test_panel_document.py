"""Editing the configuration file as text, judging a change, and the page's own files."""

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from pgharbor.config import (
    AuditConfig,
    Config,
    ConnectionConfig,
    CustomTool,
    CustomToolParam,
    ProtectedTable,
    ToolsConfig,
    load_config,
)
from pgharbor.errors import ConfigError
from pgharbor.ui.document import (
    MASK,
    DocumentError,
    Located,
    apply_ops,
    list_backups,
    mask_text,
    save_text,
    unified_diff,
)
from pgharbor.ui.fields import CONNECTION_GROUPS, NOT_IN_DEFAULTS, schema
from pgharbor.ui.risks import compare, needs_acknowledgement, posture

STATIC = Path(__file__).parents[2] / "src" / "pgharbor" / "ui" / "static"

SOURCE = """\
# Kept: the owner's first line.
default_connection = "dev"   # which one

[defaults]
statement_timeout = "30s"  # short on purpose

# Development
[connections.dev]
host = "localhost"   # here
password = "s3cret"
max_rows = 50

[[connections.dev.protect]]
table = "a.b"
deny_columns = ["x"]

[connections.dev.params]
sslmode = "require"
sslpassword = "passphrase"

# Production
[connections.prod]
environment = "production"
dsn = "postgresql://u:pw@h/db"

[custom_tools.t1]
description = "d"
sql = "select 1"
connections = ["dev"]
"""


def op(kind: str, path: str, **more: Any) -> dict[str, Any]:
    return {"op": kind, "path": path.split("."), **more}


# ------------------------------------------------------------------------------ editing


def test_an_edit_touches_only_what_it_names() -> None:
    out = apply_ops(SOURCE, [op("set", "connections.dev.max_rows", value=75)])
    before, after = SOURCE.splitlines(), out.splitlines()
    assert [a for a, b in zip(before, after, strict=True) if a != b] == ["max_rows = 50"]
    assert "max_rows = 75" in after


def test_comments_and_order_survive_every_kind_of_edit() -> None:
    out = apply_ops(
        SOURCE,
        [
            op("set", "defaults.statement_timeout", value="45s"),
            op("unset", "connections.dev.host"),
            op("set", "connections.dev.lock_timeout", value="2s"),
            op("set", "connections.dev.protect", value=[{"table": "c.*", "max_rows": 5}]),
            op("set", "connections.hml", value={"host": "h", "params": {"sslmode": "verify-full"}}),
            op("set", "tools.disabled", value=["pg_export_query"]),
        ],
    )
    for kept in (
        "# Kept: the owner's first line.",
        'default_connection = "dev"   # which one',
        'statement_timeout = "45s"  # short on purpose',
        "# Development\n[connections.dev]",
        "# Production\n[connections.prod]",
    ):
        assert kept in out
    data = tomllib.loads(out)
    assert list(data["connections"]) == ["dev", "prod", "hml"]
    assert data["connections"]["dev"]["protect"] == [{"table": "c.*", "max_rows": 5}]
    assert data["connections"]["hml"]["params"] == {"sslmode": "verify-full"}
    assert "\n\n[connections.hml]" in out  # a new table starts after a blank line
    assert "\n\n[tools]" in out


def test_a_rename_keeps_the_place_and_follows_the_references() -> None:
    out = apply_ops(SOURCE, [op("rename", "connections.dev", to="desenv")])
    assert "# Development\n[connections.desenv]" in out
    assert "[[connections.desenv.protect]]" in out
    assert "[connections.desenv.params]" in out
    data = tomllib.loads(out)
    assert list(data["connections"]) == ["desenv", "prod"]
    assert data["default_connection"] == "desenv"
    assert data["custom_tools"]["t1"]["connections"] == ["desenv"]
    assert data["connections"]["desenv"]["password"] == "s3cret"

    inline = 'default_connection = "a"\n[connections]\na = { host = "x" }\nb = { host = "y" }\n'
    renamed = tomllib.loads(apply_ops(inline, [op("rename", "connections.a", to="z")]))
    assert renamed == {
        "default_connection": "z",
        "connections": {"b": {"host": "y"}, "z": {"host": "x"}},
    }


def test_text_with_line_breaks_is_written_as_a_block_and_read_back_exactly() -> None:
    sql = "SELECT 1\n  FROM t\n WHERE a = $1 AND b = 'it''s \"q\"' -- \\ back\\slash\n"
    out = apply_ops(SOURCE, [op("set", "custom_tools.t2", value={"description": "x", "sql": sql})])
    assert 'sql = """\nSELECT 1\n' in out
    assert tomllib.loads(out)["custom_tools"]["t2"]["sql"] == sql


@pytest.mark.parametrize(
    "bad",
    [
        op("set", "connections", value={}),
        op("set", "unknown.key", value=1),
        op("set", "audit.enabled.deeper", value=True),
        op("set", "connections.dev.protect.table", value="x"),
        op("set", "connections.dev.max_rows", value=None),
        op("set", "connections.dev.max_rows", value=float("nan")),
        op("set", "connections.dev.x", value=[1, {"a": 1}]),
        op("set", "connections.dev", value="not a table"),
        op("set", "connections.dev", value={"bad key!": 1}),
        op("rename", "audit.enabled", to="x"),
        op("rename", "connections.dev", to="prod"),
        op("rename", "connections.dev", to="Bad Name"),
        op("rename", "connections.ghost", to="x"),
        {"op": "set", "path": ["connections", "a/b"], "value": {}},
        {"op": "set", "path": "connections.dev", "value": {}},
        {"op": "drop", "path": ["audit", "enabled"]},
    ],
)
def test_a_malformed_change_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(DocumentError):
        apply_ops(SOURCE, [bad])


def test_a_file_that_is_not_toml_cannot_be_edited() -> None:
    with pytest.raises(DocumentError) as refused:
        apply_ops("this is [not toml\n", [op("set", "audit.enabled", value=True)])
    assert refused.value.code == "toml"


# ------------------------------------------------------------------------------ showing


def test_masking_hides_every_credential_and_nothing_else() -> None:
    masked = mask_text(SOURCE)
    for secret in ("s3cret", "passphrase", "u:pw@h"):
        assert secret not in masked
    assert masked.count(MASK) == 3
    assert masked.replace(MASK, "") != SOURCE  # sanity: something was replaced
    assert 'host = "localhost"   # here' in masked


def test_credentials_are_masked_wherever_the_file_puts_them() -> None:
    text = """\
# password = "left-in-a-comment"
[defaults.params]
sslpassword = "in-defaults"

[connections]
a = { host = "h", password = "in-an-inline-table" }

[connections.b]
host = "h"
pasword = "behind-a-typo"

[connections.b.params]
sslmode = "require"
not_a_libpq_option = "unknown-parameter"

[something_else]
key = "unknown-table"
"""
    masked = mask_text(text)
    for secret in (
        "left-in-a-comment",
        "in-defaults",
        "in-an-inline-table",
        "behind-a-typo",
        "unknown-parameter",
        "unknown-table",
    ):
        assert secret not in masked, secret
    assert 'sslmode = "require"' in masked  # what is known and harmless stays readable


def test_a_comment_that_may_hold_a_credential_is_masked_and_others_are_kept() -> None:
    text = """\
# Production: read-only, approval in every session.
# the old senha was aaa-1
# db.internal:5432:app:agent:bbb-2
# see postgresql://agent:ccc-3@db.internal/app
[connections.prod]
host = "db.internal"  # password: ddd-4
description = "has a # in it"  # an ordinary note
"""
    masked = mask_text(text)
    for secret in ("aaa-1", "bbb-2", "ccc-3", "ddd-4"):
        assert secret not in masked, secret
    assert "# Production: read-only, approval in every session." in masked
    assert 'description = "has a # in it"  # an ordinary note' in masked
    assert 'host = "db.internal"' in masked


@pytest.mark.parametrize(
    "broken",
    [
        'dev = { password = "sec-ret" \n',
        'connections.dev.password = "sec-ret\n',
        'password = "before#sec-ret"\n[oops\n',
        'password = """\nsec-ret\n"""\n[oops\n',
        '# password = "sec-ret"\n[oops\n',
        "[oops\nsec-ret\n",
        'password = """\n[sec-ret]\nsec-ret = 1\n"""\n[oops\n',
    ],
)
def test_a_file_that_does_not_parse_shows_only_its_skeleton(broken: str) -> None:
    masked = mask_text(broken)
    assert "sec-ret" not in masked
    assert MASK in masked
    kept = mask_text('[connections.dev]\nhost = "h"\nthis is [not toml\n')
    assert kept.splitlines()[:2] == ["[connections.dev]", f'host = "{MASK}"']


def test_a_difference_never_shows_a_credential() -> None:
    changed = apply_ops(SOURCE, [op("set", "connections.dev.password", value="brand-new")])
    diff = "\n".join(unified_diff(SOURCE, changed))
    assert "s3cret" not in diff
    assert "brand-new" not in diff
    # Both sides are masked, so replacing a password with another shows no line at all.
    assert not [line for line in diff.splitlines() if line.startswith(("+p", "-p"))]


# ------------------------------------------------------------------------------- saving


def test_a_save_is_validated_backed_up_and_private(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(SOURCE, encoding="utf-8")
    located = Located(path=file, exists=True, explicit=True)
    digest = __import__("hashlib").sha256(SOURCE.encode()).hexdigest()

    with pytest.raises(DocumentError) as invalid:
        save_text(located, SOURCE + "\n[tools]\ntypo = 1\n", digest)
    assert invalid.value.code == "invalid"
    assert "tools.typo" in str(invalid.value)
    with pytest.raises(DocumentError) as stale:
        save_text(located, SOURCE, "0" * 64)
    assert stale.value.code == "conflict"
    assert file.read_text(encoding="utf-8") == SOURCE
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]  # no temporary file left

    better = apply_ops(SOURCE, [op("set", "connections.dev.max_rows", value=10)])
    saved = save_text(located, better, digest)
    assert load_config(file).connections["dev"].max_rows == 10
    assert (tmp_path / "backups" / saved["backup"]).read_text(encoding="utf-8") == SOURCE
    assert [b["name"] for b in list_backups(located)] == [saved["backup"]]
    if sys.platform != "win32":
        assert file.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_linked_file_is_rewritten_where_it_really_is(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "pgharbor.toml"
    real.parent.mkdir()
    real.write_text(SOURCE, encoding="utf-8")
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    located = Located(path=link, exists=True, explicit=True)
    digest = __import__("hashlib").sha256(SOURCE.encode()).hexdigest()
    save_text(located, apply_ops(SOURCE, [op("set", "connections.dev.max_rows", value=7)]), digest)
    assert link.is_symlink()  # the link is still a link
    assert load_config(real).connections["dev"].max_rows == 7


# -------------------------------------------------------------------------------- risks


def _config(**connections: dict[str, Any]) -> Config:
    return Config(
        connections={
            name: ConnectionConfig(name=name, **body) for name, body in connections.items()
        }
    )


PROD: dict[str, Any] = {
    "environment": "production",
    "host": "db.internal",
    "protect": (ProtectedTable(table="billing.cards", deny_columns=("cvv", "pan"), max_rows=10),),
    "params": {"sslmode": "verify-full"},
    "max_plan_cost": 1000.0,
    "deny_functions": ("public.danger",),
}


def codes(before: Config | None, after: Config) -> dict[str, str]:
    return {finding["code"]: finding["level"] for finding in compare(before, after)}


@pytest.mark.parametrize(
    ("change", "expected", "level"),
    [
        ({"access": "read-write"}, "access-raised", "danger"),
        ({"approval": "never"}, "approval-removed", "danger"),
        ({"environment": "staging"}, "environment-demoted", "warn"),
        ({"write_confirmation": "none"}, "write-confirmation-weakened", "danger"),
        ({"write_confirmation": "conversation"}, "write-confirmation-weakened", "warn"),
        ({"function_policy": "permissive"}, "function-policy-weakened", "danger"),
        ({"allow_functions": ("public.f",)}, "functions-allowed", "warn"),
        ({"expose_secret_catalogs": True}, "secret-catalogs-exposed", "danger"),
        ({"redact_columns": ()}, "redaction-reduced", "warn"),
        ({"allow_explain_analyze": True}, "explain-analyze-allowed", "warn"),
        ({"protect": ()}, "protection-removed", "danger"),
        (
            {
                "protect": (
                    ProtectedTable(table="billing.cards", deny_columns=("cvv",), max_rows=10),
                )
            },
            "columns-no-longer-denied",
            "danger",
        ),
        (
            {"protect": (ProtectedTable(table="billing.cards", deny_columns=("cvv", "pan")),)},
            "protected-rows-raised",
            "warn",
        ),
        ({"max_plan_cost": None}, "cost-ceiling-raised", "warn"),
        ({"statement_timeout": "5min"}, "limit-raised", "warn"),
        ({"lock_timeout": 0}, "limit-raised", "warn"),  # zero is "no limit" in PostgreSQL
        ({"idle_in_transaction_session_timeout": 0}, "limit-raised", "warn"),
        ({"role": "postgres"}, "role-changed", "warn"),  # SET ROLE to a stronger role
        ({"user": "postgres"}, "target-changed", "warn"),
        ({"params": {"sslmode": "verify-full", "hostaddr": "10.0.0.9"}}, "target-changed", "warn"),
        ({"params": {"sslmode": "verify-full", "service": "other"}}, "target-changed", "warn"),
        (
            {"params": {"sslmode": "verify-full", "sslrootcert": "other-ca.pem"}},
            "params-changed",
            "warn",
        ),
        ({"import_dir": "~"}, "import-dir-changed", "warn"),
        ({"export_dir": "~/.ssh"}, "export-dir-changed", "warn"),
        ({"deny_functions": ()}, "functions-no-longer-denied", "warn"),
        ({"params": {"sslmode": "prefer"}}, "ssl-weakened", "warn"),
        ({"params": {}}, "ssl-weakened", "warn"),  # no value is libpq's "prefer"
        ({"host": "elsewhere"}, "target-changed", "warn"),
    ],
)
def test_every_way_of_protecting_less_is_named(
    change: dict[str, Any], expected: str, level: str
) -> None:
    found = codes(_config(prod=PROD), _config(prod={**PROD, **change}))
    assert found.get(expected) == level, found


def rules(*items: dict[str, Any]) -> tuple[ProtectedTable, ...]:
    return tuple(ProtectedTable(**item) for item in items)


CARDS = {"table": "billing.cards", "deny_columns": ("cvv", "pan"), "max_rows": 10}


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        # A broad, empty rule placed first is the one in force: the columns are released.
        (rules({"table": "*.*"}, CARDS), "columns-no-longer-denied"),
        # The same table twice, the first copy empty.
        (rules({"table": "billing.cards"}, CARDS), "columns-no-longer-denied"),
        # A narrower, weaker rule slipped in before a pattern that used to cover it.
        (rules({"table": "billing.car*"}, CARDS), "columns-no-longer-denied"),
        (
            rules({"table": "billing.*", "deny_columns": ("cvv",)}, CARDS),
            "columns-no-longer-denied",
        ),
        (rules(), "protection-removed"),
        # Patterns that overlap without one naming the other: shown, not judged harmless.
        (rules({"table": "*.c?rds", "deny_columns": ("cvv", "pan"), "max_rows": 10}, CARDS), None),
        (rules({"table": "bil*.cards", "max_rows": 10, "deny_columns": ("cvv", "pan")}), None),
    ],
)
def test_the_rule_in_force_is_what_counts_for_a_protected_table(
    after: tuple[ProtectedTable, ...], expected: str | None
) -> None:
    before = _config(prod={**PROD, "protect": rules(CARDS)})
    found = compare(before, _config(prod={**PROD, "protect": after}))
    assert needs_acknowledgement(found), found
    if expected is not None:
        assert expected in {f["code"] for f in found}, found


def test_adding_or_tightening_a_rule_asks_for_nothing() -> None:
    before = _config(prod={**PROD, "protect": rules(CARDS)})
    appended = rules(CARDS, {"table": "audit.*", "deny_columns": ("ip",)})
    tighter = rules({**CARDS, "deny_columns": ("cvv", "pan", "holder"), "max_rows": 5})
    for after in (appended, tighter):
        assert compare(before, _config(prod={**PROD, "protect": after})) == []


def test_a_rename_is_compared_with_what_the_connection_was() -> None:
    before = _config(prod=PROD)
    plain = compare(before, _config(production=PROD))
    assert [(f["code"], f["level"]) for f in plain] == [("connection-renamed", "note")]

    stripped = {**PROD, "environment": "staging", "protect": (), "max_plan_cost": None}
    found = {f["code"]: f["level"] for f in compare(before, _config(stage=stripped))}
    assert found["approval-removed"] == "danger"
    assert found["protection-removed"] == "danger"
    assert found["cost-ceiling-raised"] == "warn"


def test_no_sslmode_counts_as_prefer_on_both_sides() -> None:
    unset = {**PROD, "params": {}}
    off = {**PROD, "params": {"sslmode": "disable"}}
    assert codes(_config(prod=unset), _config(prod=off)) == {"ssl-weakened": "warn"}
    assert (
        compare(_config(prod=unset), _config(prod={**PROD, "params": {"sslmode": "prefer"}})) == []
    )
    assert compare(_config(prod=off), _config(prod=unset)) == []  # this one tightens


def test_a_rename_does_not_look_like_a_wider_custom_tool() -> None:
    tool = CustomTool(name="tt", description="d", sql="select 1", connections=("prod",), max_rows=5)
    before = Config(connections=_config(prod=PROD).connections, custom_tools={"tt": tool})
    follows = tool.model_copy(update={"connections": ("production",)})
    after = Config(connections=_config(production=PROD).connections, custom_tools={"tt": follows})
    assert codes(before, after) == {"connection-renamed": "note"}
    more = Config(
        connections=before.connections,
        custom_tools={"tt": tool.model_copy(update={"max_rows": None})},
    )
    assert codes(before, more) == {"custom-tool-rows-raised": "note"}


def test_a_new_connection_always_needs_a_yes() -> None:
    """A "development" connection may point anywhere, the production server included."""
    after = _config(prod=PROD, dev={"host": "db.internal", "environment": "development"})
    found = compare(_config(prod=PROD), after)
    assert [(f["code"], f["level"]) for f in found] == [("connection-added", "warn")]


def test_moving_the_audit_log_is_noticed() -> None:
    before, after = Config(), Config(audit=AuditConfig(path="/dev/null"))
    assert codes(before, after) == {"audit-path-changed": "warn"}


def test_tuning_the_transport_asks_for_nothing() -> None:
    tuned = {
        **PROD,
        "params": {"sslmode": "verify-full", "keepalives": "1", "connect_timeout": "5"},
    }
    assert compare(_config(prod=PROD), _config(prod=tuned)) == []


def test_tightening_asks_for_nothing() -> None:
    loose = {**PROD, "access": "admin", "approval": "never", "function_policy": "permissive"}
    tight = {**PROD, "statement_timeout": "5s", "max_rows": 10, "deny_relations": ("a.*",)}
    assert compare(_config(prod=loose), _config(prod=tight)) == []
    assert compare(_config(prod=PROD), _config(prod=PROD)) == []
    assert compare(_config(prod=PROD, dev={}), _config(prod=PROD)) == []  # a connection removed


def test_global_changes_are_judged_too() -> None:
    before = Config(tools=ToolsConfig(disabled=("pg_query",), disabled_groups=("admin",)))
    tool = CustomTool(name="tt", description="d", sql="select 1", connections=("prod",))
    after = Config(
        audit=AuditConfig(enabled=False),
        custom_tools={"tt": tool},
        connections={"prod": ConnectionConfig(name="prod", **PROD)},
        default_connection="prod",
    )
    found = codes(before, after)
    assert found["audit-disabled"] == "danger"
    assert found["tool-enabled"] == "warn"
    assert found["tool-group-enabled"] == "danger"
    assert found["custom-tool-added"] == "warn"
    assert found["default-is-production"] == "warn"
    wider = Config(custom_tools={"tt": tool.model_copy(update={"connections": ()})})
    assert codes(Config(custom_tools={"tt": tool}), wider) == {"custom-tool-scope-widened": "warn"}
    changed = Config(custom_tools={"tt": tool.model_copy(update={"sql": "select 2"})})
    assert codes(Config(custom_tools={"tt": tool}), changed) == {"custom-tool-sql-changed": "warn"}


def test_without_something_valid_to_compare_with_everything_is_new() -> None:
    found = compare(None, _config(prod={**PROD, "access": "read-write"}))
    assert found[0]["level"] == "danger"
    assert {f["code"] for f in found} >= {"no-baseline", "new-connection-writes"}
    assert needs_acknowledgement(found)
    assert not needs_acknowledgement([{"code": "limit-raised", "level": "note"}])


def test_posture_names_what_a_careful_owner_would_fix() -> None:
    careless = _config(
        prod={
            "environment": "production",
            "host": "db.internal",
            "access": "admin",
            "approval": "never",
            "write_confirmation": "none",
            "function_policy": "permissive",
            "expose_secret_catalogs": True,
            "redact_columns": (),
        }
    )
    found = {f["code"]: f["level"] for f in posture(careless, file_mode="644", secrets=("x",))}
    for code in (
        "file-readable-by-others",
        "production-writes",
        "production-without-approval",
        "functions-permissive",
        "secret-catalogs-exposed",
        "writes-without-dialog",
    ):
        assert found[code] == "danger", code
    assert found["production-without-cost-ceiling"] == "warn"
    assert found["no-tls-required"] == "warn"
    careful = _config(prod={**PROD, "approval_fallback": "deny"})
    assert [f["code"] for f in posture(careful, file_mode="600")] == ["all-good"]


# --------------------------------------------------------------- forms and dictionaries


def test_every_configuration_key_has_a_place_in_the_forms() -> None:
    described = schema()
    grouped = [key for keys in CONNECTION_GROUPS.values() for key in keys]
    assert sorted(grouped) == sorted(set(ConnectionConfig.model_fields) - {"name"})
    assert len(grouped) == len(set(grouped))
    assert {f["key"] for f in described["protect"]} == set(ProtectedTable.model_fields)
    assert {f["key"] for f in described["audit"]} == set(AuditConfig.model_fields)
    assert {f["key"] for f in described["tools"]} == set(ToolsConfig.model_fields)
    assert {f["key"] for f in described["custom_tool_param"]} == set(CustomToolParam.model_fields)
    access = next(f for f in described["connection"] if f["key"] == "access")
    assert (access["kind"], access["options"]) == ("choice", ["read-only", "read-write", "admin"])
    pool = next(f for f in described["connection"] if f["key"] == "pool_size")
    assert (pool["min"], pool["max"], pool["default"]) == (1, 20, 2)
    json.dumps(described)  # all of it is plain JSON


def test_the_keys_refused_under_defaults_are_the_ones_the_loader_refuses(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    body = "\n".join(f'{key} = "x"' for key in sorted(NOT_IN_DEFAULTS))
    file.write_text(f"[defaults]\n{body}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as refused:
        load_config(file)
    listed = str(refused.value).split("cannot set ")[1].split(";")[0].split(", ")
    assert sorted(listed) == sorted(NOT_IN_DEFAULTS)


def dictionaries() -> dict[str, dict[str, str]]:
    return {
        code: json.loads((STATIC / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        for code in ("en", "pt-BR", "es")
    }


def test_the_three_languages_say_the_same_things() -> None:
    texts = dictionaries()
    english = texts["en"]
    for code in ("pt-BR", "es"):
        other = texts[code]
        assert list(other) == list(english), code
        for key, text in english.items():
            assert other[key].strip(), (code, key)
            marks = sorted(set(re.findall(r"\{\w+\}", text)))
            assert sorted(set(re.findall(r"\{\w+\}", other[key]))) == marks, (code, key)
            # A language may need a plural where English has none, never the other way.
            assert other[key].count("|") <= 1
            assert other[key].count("|") >= text.count("|"), (code, key)


def test_the_page_never_asks_for_a_text_that_does_not_exist() -> None:
    english = dictionaries()["en"]
    scripts = "\n".join(p.read_text(encoding="utf-8") for p in STATIC.glob("js/**/*.js"))
    literal = set(re.findall(r"""\bt\(\s*["']([A-Za-z0-9_.-]+)["']""", scripts))
    assert literal
    assert not sorted(literal - set(english))
    for name in set(re.findall(r"""field(?:Label|Help)\(\s*"([a-z_]+)"\)""", scripts)):
        assert f"field.{name}.label" in english, name

    described = schema()
    for section in ("connection", "protect", "audit"):
        for entry in described[section]:
            key = (
                "protect_max_rows"
                if (section, entry["key"]) == ("protect", "max_rows")
                else entry["key"]
            )
            assert f"field.{key}.label" in english, key
            for option in entry.get("options", []):
                assert f"option.{entry['key']}.{option}" in english, (key, option)
    for entry in described["connection"]:
        assert f"field.{entry['key']}.help" in english, entry["key"]
    for group in described["connection_groups"]:
        assert f"group.{group}" in english
        assert f"group.{group}.text" in english
    for group in described["tool_groups"]:
        assert f"toolgroup.{group}" in english

    source = (STATIC.parent / "risks.py").read_text(encoding="utf-8")
    for code in set(re.findall(r"""(?:_finding|add)\(\s*"([a-z-]+)\"""", source)):
        assert f"risk.{code}" in english or f"posture.{code}" in english, code


def test_the_page_is_made_only_of_its_own_files() -> None:
    """What the security policy of the page promises, checked in the files themselves."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<style" not in html
    assert " style=" not in html
    assert not re.findall(r"<script(?![^>]*\bsrc=)", html)  # no inline script
    assert not re.findall(r"""(?:src|href)=["'](?:https?:)?//""", html)  # nothing from elsewhere
    for script in STATIC.glob("js/**/*.js"):
        text = script.read_text(encoding="utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert banned not in text, (script.name, banned)
        assert "localStorage.setItem(TOKEN" not in text
    for sheet in STATIC.glob("css/*.css"):
        assert "url(" not in sheet.read_text(encoding="utf-8"), sheet.name
