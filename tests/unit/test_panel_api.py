"""The panel's API: reading everything, changing the configuration, pausing access.

What is checked for every change: nothing is written unless the server's own validator
accepts it, credentials never come back, a change that weakens the protection needs an
explicit yes, and every save leaves a backup and a line in the audit log.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from pgharbor.app import App
from pgharbor.audit import Audit
from pgharbor.config import AuditConfig, load_config
from pgharbor.pause import read_pause
from pgharbor.server import _register_custom_tools, enabled_tools, load_tool_modules

from .panel_client import CONFIG, SECRETS, PanelClient


def digest_of(panel_config: dict[str, Any]) -> str:
    return panel_config["file"]["digest"]


async def current(panel: PanelClient) -> dict[str, Any]:
    reply = await panel.get("/api/config")
    assert reply.status == 200, reply.text
    return reply.json()


def set_op(path: str, value: Any) -> dict[str, Any]:
    return {"op": "set", "path": path.split("."), "value": value}


def audit_lines(home: Path) -> list[dict[str, Any]]:
    file = home / "home" / "pgharbor" / "audit.jsonl"
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------------ reading


async def test_no_credential_ever_leaves_the_panel(panel: PanelClient, config_file: Path) -> None:
    config = await current(panel)
    base = digest_of(config)
    replies = [
        await panel.get("/api/config"),
        await panel.get("/api/config/text"),
        await panel.get("/api/meta"),
        await panel.post(
            "/api/config/preview",
            {"base": base, "ops": [set_op("connections.dev.max_rows", 10)]},
        ),
        await panel.post(
            "/api/config/apply",
            {"base": base, "ops": [set_op("connections.dev.max_rows", 10)]},
        ),
        await panel.get("/api/backups"),
    ]
    [backup] = replies[-1].json()["backups"]
    replies.append(await panel.get(f"/api/backups/{backup['name']}"))
    for reply in replies:
        assert reply.status == 200, reply.text
        for secret in SECRETS:
            assert secret not in reply.text
    # The file itself still has them: the panel masks, it does not strip.
    assert all(secret in config_file.read_text(encoding="utf-8") for secret in SECRETS)
    assert sorted(config["secrets"]) == [
        "connections.dev.params.sslpassword",
        "connections.dev.password",
        "connections.prod.dsn",
    ]
    assert "password" not in config["raw"]["connections"]["dev"]
    assert "password" not in config["connections"]["dev"]["effective"]


async def test_the_configuration_is_described_with_where_values_come_from(
    panel: PanelClient,
) -> None:
    config = await current(panel)
    assert config["valid"] is True
    assert config["file"]["origin"] == "file"
    assert config["default_connection"] == "dev"
    dev, prod = config["connections"]["dev"], config["connections"]["prod"]
    assert dev["effective"]["statement_timeout"] == 30_000  # from [defaults]
    assert dev["effective"]["max_rows"] == 200  # built-in
    assert "max_rows" not in config["raw"]["connections"]["dev"]
    assert prod["is_production"] is True
    assert prod["needs_approval"] is True
    assert prod["target"] == "DSN from the configuration file"
    assert {f["code"] for f in config["posture"]} >= {"secret-in-file"}


async def test_meta_describes_every_form(panel: PanelClient) -> None:
    meta = (await panel.get("/api/meta")).json()
    assert meta["read_only"] is False
    keys = {field["key"] for field in meta["schema"]["connection"]}
    assert {"access", "protect", "statement_timeout", "password"} <= keys
    assert "name" not in keys


# ------------------------------------------------------------------------------ editing


async def test_a_change_is_previewed_then_saved_with_a_backup_and_a_log_line(
    panel: PanelClient, config_file: Path, home: Path
) -> None:
    config = await current(panel)
    change = {
        "base": digest_of(config),
        "ops": [
            set_op("connections.dev.max_rows", 50),
            set_op("defaults.statement_timeout", "20s"),
        ],
    }
    preview = (await panel.post("/api/config/preview", change)).json()
    assert preview["valid"] is True
    assert preview["changed"] is True
    assert preview["needs_acknowledgement"] is False  # both changes tighten
    assert '+statement_timeout = "20s"  # short on purpose' in preview["diff"]
    assert config_file.read_text(encoding="utf-8") == CONFIG  # a preview writes nothing

    saved = await panel.post("/api/config/apply", change)
    assert saved.status == 200, saved.text
    text = config_file.read_text(encoding="utf-8")
    assert "# The owner's notes stay in the file." in text
    assert 'statement_timeout = "20s"  # short on purpose' in text
    loaded = load_config(config_file)
    assert loaded.connections["dev"].max_rows == 50
    assert loaded.connections["dev"].statement_timeout == 20_000
    assert saved.json()["connections"]["dev"]["effective"]["max_rows"] == 50

    backup = config_file.parent / "backups" / saved.json()["saved"]["backup"]
    assert backup.read_text(encoding="utf-8") == CONFIG
    if sys.platform != "win32":
        assert backup.stat().st_mode & 0o777 == 0o600
        assert config_file.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in config_file.parent.iterdir()] == ["config.toml", "backups"] or sorted(
        p.name for p in config_file.parent.iterdir()
    ) == ["backups", "config.toml"]

    [line] = [e for e in audit_lines(home) if e["tool"] == "panel.config"]
    assert line["session"] == "panel"
    # Names of keys, never values. (The rest of the line names the backup, whose file
    # name carries a time and a fingerprint: it is not compared.)
    changed = line["detail"].split("; backup")[0]
    assert changed == "saved connections.dev.max_rows, defaults.statement_timeout"


async def test_a_change_that_weakens_the_protection_needs_an_explicit_yes(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    change = {
        "base": digest_of(config),
        "ops": [set_op("connections.prod.access", "read-write")],
    }
    preview = (await panel.post("/api/config/preview", change)).json()
    assert preview["needs_acknowledgement"] is True
    codes = {risk["code"]: risk for risk in preview["risks"]}
    assert codes["access-raised"]["level"] == "danger"
    assert codes["access-raised"]["connection"] == "prod"

    refused = await panel.post("/api/config/apply", change)
    assert (refused.status, refused.error) == (409, "acknowledge")
    assert refused.json()["error"]["risks"]
    assert config_file.read_text(encoding="utf-8") == CONFIG

    for not_a_yes in ("true", 1, "yes", None):
        again = await panel.post("/api/config/apply", {**change, "acknowledged": not_a_yes})
        assert again.status == 409, not_a_yes

    accepted = await panel.post("/api/config/apply", {**change, "acknowledged": True})
    assert accepted.status == 200
    assert load_config(config_file).connections["prod"].access.value == "read-write"


@pytest.mark.parametrize(
    ("ops", "where"),
    [
        ([set_op("connections.dev.acess", "admin")], "connections.dev.acess"),  # a typo
        ([set_op("connections.dev.access", "root")], "connections.dev.access"),
        ([set_op("connections.dev.max_rows", 0)], "connections.dev.max_rows"),
        ([set_op("connections.dev.pool_size", 500)], "connections.dev.pool_size"),
        ([set_op("defaults.host", "elsewhere")], ""),
        ([set_op("default_connection", "nowhere")], ""),
        ([{"op": "unset", "path": ["connections", "dev"]}], ""),  # still the default
        ([set_op("connections.BAD NAME", {"host": "x"})], None),
    ],
)
async def test_what_the_validator_refuses_is_never_written(
    panel: PanelClient, config_file: Path, ops: list[dict[str, Any]], where: str | None
) -> None:
    config = await current(panel)
    change = {"base": digest_of(config), "ops": ops, "acknowledged": True}
    preview = await panel.post("/api/config/preview", change)
    refused = await panel.post("/api/config/apply", change)
    if where is None:  # not even a well-formed change
        assert preview.status == refused.status == 400
    else:
        assert preview.json()["valid"] is False
        assert where in [error["loc"] for error in preview.json()["errors"]]
        assert (refused.status, refused.error) == (422, "invalid")
        assert refused.json()["error"]["errors"]
    assert config_file.read_text(encoding="utf-8") == CONFIG
    assert sorted(p.name for p in config_file.parent.iterdir()) == ["config.toml"]  # no leftovers
    assert str(config_file.parent) not in refused.text or ".pgharbor-" not in refused.text


@pytest.mark.parametrize(
    "ops",
    [
        "not a list",
        [{"op": "set", "path": ["connections"], "value": {}}],
        [{"op": "set", "path": ["secrets"], "value": 1}],
        [{"op": "set", "path": ["connections", "dev", "protect", "table"], "value": "x"}],
        [{"op": "set", "path": ["custom_tools", "t", "params", "name"], "value": "x"}],
        [{"op": "set", "path": ["connections", "dev", "params", "a", "b"], "value": "x"}],
        [{"op": "set", "path": ["audit", "enabled", "x"], "value": True}],
        [{"op": "set", "path": ["connections", "../x"], "value": {}}],
        [{"op": "set", "path": ["connections", "dev", "max_rows"], "value": None}],
        [{"op": "exec", "path": ["audit", "enabled"]}],
        [{"op": "rename", "path": ["audit", "enabled"], "to": "x"}],
        [{"op": "rename", "path": ["connections", "dev"], "to": "prod"}],
        [{"op": "rename", "path": ["connections", "ghost"], "to": "x"}],
        ["set"],
    ],
)
async def test_a_malformed_change_is_refused(
    panel: PanelClient, config_file: Path, ops: Any
) -> None:
    config = await current(panel)
    refused = await panel.post("/api/config/apply", {"base": digest_of(config), "ops": ops})
    assert refused.status in (400, 404), refused.text
    assert config_file.read_text(encoding="utf-8") == CONFIG


async def test_a_file_changed_behind_the_panels_back_is_not_overwritten(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    edited_by_hand = CONFIG + "\n# edited in an editor meanwhile\n"
    config_file.write_text(edited_by_hand, encoding="utf-8")
    change = {"base": digest_of(config), "ops": [set_op("connections.dev.max_rows", 5)]}
    for route in ("/api/config/preview", "/api/config/apply"):
        refused = await panel.post(route, change)
        assert (refused.status, refused.error) == (409, "conflict")
    assert config_file.read_text(encoding="utf-8") == edited_by_hand


async def test_a_connection_is_created_renamed_and_removed(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    created = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(config),
            "ops": [
                set_op(
                    "connections.hml",
                    {
                        "host": "hml-db",
                        "dbname": "app",
                        "user_env": "HML_USER",
                        "password_env": "HML_PASSWORD",
                        "protect": [{"table": "app.users", "deny_columns": ["cpf"]}],
                        "params": {"sslmode": "verify-full"},
                    },
                )
            ],
            "acknowledged": True,
        },
    )
    assert created.status == 200, created.text
    hml = load_config(config_file).connections["hml"]
    assert hml.protect[0].deny_columns == ("cpf",)
    assert hml.params == {"sslmode": "verify-full"}
    variables = created.json()["connections"]["hml"]["environment_variables"]
    assert variables["password_env"] == {"name": "HML_PASSWORD", "set": False}

    renamed = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(created.json()),
            "ops": [{"op": "rename", "path": ["connections", "dev"], "to": "desenv"}],
        },
    )
    assert renamed.status == 200, renamed.text
    text = config_file.read_text(encoding="utf-8")
    assert "# Development\n[connections.desenv]" in text  # same place, same comment
    loaded = load_config(config_file)
    assert loaded.default_connection == "desenv"  # the reference followed the rename
    assert loaded.connections["desenv"].password == "hunter2-inline-secret"

    removed = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(renamed.json()),
            "ops": [{"op": "unset", "path": ["connections", "hml"]}],
        },
    )
    assert removed.status == 200, removed.text
    assert sorted(load_config(config_file).connections) == ["desenv", "prod"]


async def test_one_connection_parameter_changes_without_touching_the_hidden_one(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    assert config["raw"]["connections"]["dev"]["params"] == {"sslmode": "disable"}
    saved = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(config),
            "ops": [
                set_op("connections.dev.params.sslmode", "require"),
                set_op("connections.dev.params.keepalives", 1),
            ],
        },
    )
    assert saved.status == 200, saved.text
    params = load_config(config_file).connections["dev"].params
    assert params == {
        "sslmode": "require",
        "keepalives": "1",
        "sslpassword": "key-file-passphrase",  # never shown, and still there
    }
    emptied = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(saved.json()),
            "ops": [
                {"op": "unset", "path": ["connections", "prod", "max_plan_cost"]},
                *[
                    {"op": "unset", "path": ["connections", "dev", "params", key]}
                    for key in ("sslmode", "keepalives", "sslpassword")
                ],
            ],
            "acknowledged": True,
        },
    )
    assert emptied.status == 200, emptied.text
    assert "[connections.dev.params]" not in config_file.read_text(encoding="utf-8")


async def test_a_credential_can_be_set_but_is_never_echoed(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    saved = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(config),
            "ops": [set_op("connections.dev.password", "a-brand-new-password")],
        },
    )
    assert saved.status == 200
    assert "a-brand-new-password" not in saved.text
    assert load_config(config_file).connections["dev"].password == "a-brand-new-password"


async def test_custom_tools_and_tool_switches_are_edited(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    sql = "SELECT id, status\n  FROM shop.orders\n WHERE customer_id = $1\n"
    saved = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(config),
            "ops": [
                set_op(
                    "custom_tools.orders",
                    {
                        "description": "Orders of one customer.",
                        "sql": sql,
                        "connections": ["prod"],
                        "max_rows": 50,
                        "params": [{"name": "customer_id", "type": "integer"}],
                    },
                ),
                set_op("tools.disabled", ["pg_export_query"]),
                set_op("tools.disabled_groups", ["extensions"]),
            ],
            "acknowledged": True,
        },
    )
    assert saved.status == 200, saved.text
    loaded = load_config(config_file)
    assert loaded.custom_tools["orders"].sql == sql
    assert loaded.tools.disabled == ("pg_export_query",)
    assert 'sql = """\nSELECT id, status\n' in config_file.read_text(encoding="utf-8")

    listed = {tool["name"]: tool for tool in (await panel.get("/api/tools")).json()["tools"]}
    assert listed["pgx_orders"]["group"] == "custom"
    assert listed["pg_export_query"]["reason"] == "disabled"
    assert listed["pg_extensions"]["reason"] == "group-disabled"
    assert listed["pg_execute"]["reason"] == "no-writable-connection"


async def test_the_tool_list_agrees_with_what_a_server_would_expose(
    panel: PanelClient, config_file: Path
) -> None:
    variants = [
        CONFIG,
        CONFIG.replace('host = "127.0.0.1"', 'host = "127.0.0.1"\naccess = "read-write"'),
        CONFIG.replace('host = "127.0.0.1"', 'host = "127.0.0.1"\naccess = "admin"'),
        CONFIG + '\n[tools]\ndisabled = ["pg_query"]\ndisabled_groups = ["health", "admin"]\n',
    ]
    load_tool_modules()
    for text in variants:
        config_file.write_text(text, encoding="utf-8")
        config = load_config(config_file)
        _register_custom_tools(config)
        expected = {spec.name for spec in enabled_tools(config)}
        shown = (await panel.get("/api/tools")).json()["tools"]
        assert {tool["name"] for tool in shown if tool["state"] == "on"} == expected


# ---------------------------------------------------------------- broken and missing files


async def test_a_broken_file_is_still_shown_and_can_be_restored(
    panel: PanelClient, config_file: Path
) -> None:
    config = await current(panel)
    saved = await panel.post(
        "/api/config/apply",
        {"base": digest_of(config), "ops": [set_op("connections.dev.max_rows", 9)]},
    )
    backup = saved.json()["saved"]["backup"]

    config_file.write_text("this is [not toml\n", encoding="utf-8")
    broken = await current(panel)
    assert broken["valid"] is False
    assert broken["file"]["is_toml"] is False
    assert "invalid TOML" in broken["errors"][0]["message"]
    assert str(config_file) not in broken["errors"][0]["message"]
    cannot_edit = await panel.post(
        "/api/config/apply",
        {"base": digest_of(broken), "ops": [set_op("audit.enabled", True)]},
    )
    assert (cannot_edit.status, cannot_edit.error) == (422, "toml")

    shown = (await panel.get(f"/api/backups/{backup}")).json()
    assert "# The owner's notes stay in the file." in shown["text"]
    route = f"/api/backups/{backup}/restore"
    preview = (
        await panel.post(f"/api/backups/{backup}/preview", {"base": digest_of(broken)})
    ).json()
    assert preview["valid"] is True
    assert preview["needs_acknowledgement"] is True
    assert any(line.startswith("+# The owner's notes") for line in preview["diff"])
    assert (config_file.read_text(encoding="utf-8")) == "this is [not toml\n"  # nothing written
    unseen = await panel.post(route, {"base": digest_of(broken)})
    # Nothing valid to compare with, so the whole file counts as new and needs a yes.
    assert (unseen.status, unseen.error) == (409, "acknowledge")
    assert unseen.json()["error"]["risks"][0]["code"] == "no-baseline"
    restored = await panel.post(route, {"base": digest_of(broken), "acknowledged": True})
    assert restored.status == 200, restored.text
    assert config_file.read_text(encoding="utf-8") == CONFIG


async def test_a_file_that_parses_but_is_not_valid_can_be_fixed_in_the_panel(
    panel: PanelClient, config_file: Path
) -> None:
    config_file.write_text(CONFIG + "\n[tools]\ndisabld = []\n", encoding="utf-8")
    broken = await current(panel)
    assert broken["valid"] is False
    assert broken["errors"][0]["loc"] == "tools.disabld"
    assert broken["raw"]["tools"] == {}  # the value of a key nobody knows is not shown
    fixed = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(broken),
            "ops": [{"op": "unset", "path": ["tools", "disabld"]}],
            "acknowledged": True,
        },
    )
    assert fixed.status == 200, fixed.text
    assert fixed.json()["valid"] is True


@pytest.mark.parametrize(
    "name",
    ["../config.toml", "..%2Fconfig.toml", "config.toml", "x.toml", "config-.toml/..", ".toml"],
)
async def test_a_backup_is_only_ever_read_from_the_backup_directory(
    panel: PanelClient, config_file: Path, name: str
) -> None:
    (config_file.parent / "x.toml").write_text("stolen = true\n", encoding="utf-8")
    config = await current(panel)
    shown = await panel.get(f"/api/backups/{name}")
    restored = await panel.post(f"/api/backups/{name}/restore", {"base": digest_of(config)})
    previewed = await panel.post(f"/api/backups/{name}/preview", {"base": digest_of(config)})
    assert shown.status == 404
    assert restored.status in (404, 405)
    assert previewed.status in (404, 405)
    assert "stolen" not in shown.text
    assert config_file.read_text(encoding="utf-8") == CONFIG


async def test_without_a_file_the_panel_offers_to_create_one(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PGHARBOR_CONFIG")
    panel = PanelClient()
    empty = await current(panel)
    assert empty["file"]["exists"] is False
    assert empty["file"]["origin"] == "none"
    assert empty["connections"] == {}
    cannot_edit = await panel.post(
        "/api/config/apply", {"base": None, "ops": [set_op("audit.enabled", True)]}
    )
    assert (cannot_edit.status, cannot_edit.error) == (409, "no-file")

    created = await panel.post("/api/config/create")
    assert created.status == 200, created.text
    assert list(created.json()["connections"]) == ["dev"]
    file = Path(created.json()["file"]["path"])
    assert file.is_file()
    again = await panel.post("/api/config/create")
    assert (again.status, again.error) == (409, "conflict")


async def test_a_connection_from_the_environment_is_shown(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PGHARBOR_CONFIG")
    monkeypatch.setenv("PGHARBOR_DSN", "postgresql://u:env-secret@h/db")
    config = await current(PanelClient())
    assert config["file"]["origin"] == "environment"
    assert config["connections"]["default"]["target"] == "DSN from $PGHARBOR_DSN"
    assert "env-secret" not in json.dumps(config)


# ---------------------------------------------------------------------- read-only mode


async def test_read_only_mode_changes_nothing(home: Path, config_file: Path) -> None:
    panel = PanelClient(read_only=True)
    config = await current(panel)
    assert (await panel.get("/api/meta")).json()["read_only"] is True
    change = {"base": digest_of(config), "ops": [set_op("audit.enabled", False)]}
    for route, data in [
        ("/api/config/preview", change),
        ("/api/config/apply", {**change, "acknowledged": True}),
        ("/api/config/create", {}),
        ("/api/pause", {"paused": True}),
        ("/api/backups/x.toml/restore", {"base": digest_of(config)}),
        ("/api/backups/x.toml/preview", {"base": digest_of(config)}),
    ]:
        refused = await panel.post(route, data)
        assert (refused.status, refused.error) == (403, "read-only"), route
    assert config_file.read_text(encoding="utf-8") == CONFIG
    assert read_pause() is None


# ------------------------------------------------------------------------------- pause


async def test_the_pause_stops_running_servers_at_once(panel: PanelClient, home: Path) -> None:
    app = App(load_config())  # a server that started before the pause
    dev = app.profile("dev")
    app.require_approval(dev)

    paused = await panel.post(
        "/api/pause", {"paused": True, "reason": "incident 42", "connections": ["dev"]}
    )
    assert paused.json()["paused"] is True
    assert paused.json()["connections"] == ["dev"]
    with pytest.raises(Exception, match="paused"):
        app.require_approval(dev)
    assert (await panel.get("/api/pulse")).json()["pause"]["reason"] == "incident 42"

    lifted = await panel.post("/api/pause", {"paused": False})
    assert lifted.json() == {"paused": False}
    app.require_approval(dev)
    details = [e["detail"] for e in audit_lines(home) if e["tool"] == "panel.pause"]
    assert details == ["paused dev: incident 42", "pause lifted"]

    for bad in ({}, {"paused": "yes"}, {"paused": True, "connections": "dev"},
                {"paused": True, "connections": ["../x"]}):  # fmt: skip
        assert (await panel.post("/api/pause", bad)).status == 400
    assert read_pause() is None


# ---------------------------------------------------------------------- what is going on


async def test_sessions_show_who_is_connected_and_who_runs_an_old_configuration(
    panel: PanelClient, config_file: Path
) -> None:
    app = App(load_config())
    app.presence.start()
    app.presence.note_client("claude-code", "2.1.0", "2026-07-28")
    app.audit.record(tool="pg_query", connection="dev", outcome="ok", sql="SELECT 1")
    app.audit.record(tool="pg_query", connection="dev", outcome="refused", rule="function")

    [session] = (await panel.get("/api/sessions")).json()["sessions"]
    assert session["state"] == "active"
    assert session["client"] == {"name": "claude-code", "version": "2.1.0"}
    assert session["outdated"] is False
    assert session["activity"]["calls"] == 2
    assert session["activity"]["refused"] == 1
    assert (await panel.get("/api/pulse")).json()["active_sessions"] == 1

    config = await current(panel)
    await panel.post(
        "/api/config/apply",
        {"base": digest_of(config), "ops": [set_op("connections.dev.max_rows", 7)]},
    )
    [session] = (await panel.get("/api/sessions")).json()["sessions"]
    assert session["outdated"] is True  # it still runs with the file it read at start

    app.presence.stop()
    [session] = (await panel.get("/api/sessions")).json()["sessions"]
    assert (session["state"], session["outdated"]) == ("ended", False)


async def test_the_audit_log_is_paged_filtered_and_counted(panel: PanelClient, home: Path) -> None:
    hostile = "<img src=x onerror=alert(1)>'; DROP TABLE users; --"
    audit = Audit(AuditConfig(), session="aaaa1111")
    for index in range(30):
        audit.record(
            tool="pg_query" if index % 3 else "pg_explain",
            connection="dev" if index % 2 else "prod",
            outcome="refused" if index % 10 == 0 else "ok",
            rule="function" if index % 10 == 0 else None,
            sql=hostile if index == 29 else f"SELECT {index}",
            elapsed_ms=float(index),
        )

    first = (await panel.get("/api/audit?limit=10")).json()
    assert first["total"] == 30
    assert first["has_more"] is True
    assert [e["n"] for e in first["entries"]] == list(range(30, 20, -1))
    assert first["entries"][0]["sql"] == hostile  # returned as data, to be shown as text
    assert first["facets"]["connection"] == ["dev", "prod"]

    older = (await panel.get(f"/api/audit?limit=10&before={first['entries'][-1]['n']}")).json()
    assert [e["n"] for e in older["entries"]] == list(range(20, 10, -1))

    refused = (await panel.get("/api/audit?outcome=refused")).json()["entries"]
    assert [e["rule"] for e in refused] == ["function"] * 3
    by_text = (await panel.get("/api/audit?q=drop%20table")).json()["entries"]
    assert len(by_text) == 1
    both = (await panel.get("/api/audit?connection=prod&tool=pg_explain")).json()["entries"]
    assert both
    assert all(e["connection"] == "prod" and e["tool"] == "pg_explain" for e in both)
    assert (await panel.get("/api/audit?session=nobody")).json()["entries"] == []

    audit.record(tool="pg_query", connection="dev", outcome="ok")
    newer = (await panel.get(f"/api/audit?after={first['last']}")).json()
    assert [e["n"] for e in newer["entries"]] == [31]

    stats = (await panel.get("/api/audit/stats?range=24h&tz=-180")).json()
    assert stats["totals"]["calls"] == 31
    assert stats["totals"]["refused"] == 3
    assert sum(bucket["ok"] + bucket["refused"] for bucket in stats["buckets"]) == 31
    assert len(stats["buckets"]) == 24
    assert stats["by_rule"] == [{"name": "function", "count": 3}]
    assert stats["by_tool"][0]["name"] == "pg_query"
    assert stats["latency"]["max"] == 29.0
    assert sum(stats["by_hour"]) == 31
    assert (await panel.get("/api/audit/stats?range=forever")).status == 400
    assert (await panel.get("/api/audit?limit=abc")).status == 400


async def test_a_log_that_is_cut_or_replaced_is_read_again(panel: PanelClient, home: Path) -> None:
    audit = Audit(AuditConfig())
    for _ in range(5):
        audit.record(tool="pg_query", connection="dev", outcome="ok")
    assert (await panel.get("/api/audit")).json()["total"] == 5
    audit.path.write_text("", encoding="utf-8")
    assert (await panel.get("/api/audit")).json()["total"] == 0
    audit.path.write_text(
        '{"ts":"2026-11-18T10:00:00+00:00","tool":"x","outcome":"ok"}\nnot json\n'
    )
    time.sleep(0.01)
    page = (await panel.get("/api/audit")).json()
    assert [e["tool"] for e in page["entries"]] == ["x"]  # the broken line is skipped
    os.remove(audit.path)
    assert (await panel.get("/api/audit")).json()["total"] == 0

    # Rewritten in place, as long as before: only its beginning tells it is another log.
    line = '{"ts":"2026-11-18T10:00:00+00:00","tool":"first","outcome":"ok"}\n'
    audit.path.write_text(line * 3, encoding="utf-8")
    assert [e["tool"] for e in (await panel.get("/api/audit")).json()["entries"]] == ["first"] * 3
    audit.path.write_text(line.replace("first", "other") * 3, encoding="utf-8")
    assert [e["tool"] for e in (await panel.get("/api/audit")).json()["entries"]] == ["other"] * 3


async def test_a_connection_test_reports_instead_of_failing(panel: PanelClient, home: Path) -> None:
    unreachable = (await panel.post("/api/connections/dev/test")).json()
    assert unreachable["ok"] is False
    assert "Could not reach the database" in unreachable["error"]
    assert not any(secret in json.dumps(unreachable) for secret in SECRETS)
    assert (await panel.post("/api/connections/ghost/test")).status == 404
    [line] = [e for e in audit_lines(home) if e["tool"] == "panel.test"]
    assert (line["connection"], line["outcome"]) == ("dev", "error")


# ----------------------------------------------- what the independent review found


async def test_a_credential_under_defaults_is_hidden_and_counted(
    panel: PanelClient, config_file: Path
) -> None:
    config_file.write_text(
        CONFIG + '\n[defaults.params]\nsslpassword = "defaults-passphrase"\n', encoding="utf-8"
    )
    config = await current(panel)
    assert config["valid"] is True
    assert "defaults.params.sslpassword" in config["secrets"]
    text = (await panel.get("/api/config/text")).text
    assert "defaults-passphrase" not in json.dumps(config)
    assert "defaults-passphrase" not in text


async def test_a_mistyped_secret_or_an_unparsable_file_shows_no_value(
    panel: PanelClient, config_file: Path
) -> None:
    config_file.write_text(CONFIG.replace('\npassword = "h', '\npasword = "h'), encoding="utf-8")
    typo = await current(panel)
    assert typo["valid"] is False
    assert typo["errors"][0]["loc"] == "connections.dev.pasword"
    for reply in (await panel.get("/api/config"), await panel.get("/api/config/text")):
        assert "hunter2-inline-secret" not in reply.text

    config_file.write_text(CONFIG + "\nthis is [not toml\n", encoding="utf-8")
    for reply in (await panel.get("/api/config"), await panel.get("/api/config/text")):
        assert not any(secret in reply.text for secret in SECRETS)


async def test_a_connection_string_libpq_cannot_read_is_not_repeated(
    panel: PanelClient, config_file: Path, home: Path
) -> None:
    config_file.write_text(
        CONFIG.replace(
            'dsn = "postgresql://agent:dsn-secret-value@db.internal/app"',
            'dsn = "host=127.0.0.1 password=abc QQSECRETQQ"',
        ),
        encoding="utf-8",
    )
    tested = await panel.post("/api/connections/prod/test")
    assert tested.json()["ok"] is False
    assert "QQSECRETQQ" not in tested.text
    assert "could not be read by libpq" in tested.json()["error"]
    assert "QQSECRETQQ" not in json.dumps(audit_lines(home))

    # A password with an "@" that was not percent-encoded: libpq takes part of it for
    # the host and quotes it when the host does not resolve.
    config_file.write_text(
        CONFIG.replace(
            'dsn = "postgresql://agent:dsn-secret-value@db.internal/app"',
            'dsn = "postgresql://agent:ab@ZZSECRETZZ@127.0.0.1:1/app"',
        ),
        encoding="utf-8",
    )
    unresolved = await panel.post("/api/connections/prod/test")
    assert unresolved.json()["ok"] is False
    assert "ZZSECRETZZ" not in unresolved.text
    assert "ZZSECRETZZ" not in json.dumps(audit_lines(home))


async def test_a_weakening_hidden_in_the_shape_of_the_change_still_needs_a_yes(
    panel: PanelClient, config_file: Path
) -> None:
    """Each of these was once saved without a word."""
    config = await current(panel)
    base = digest_of(config)
    cards = {
        "table": "billing.cards",
        "deny_columns": ["card_number", "cvv"],
        "require_index_filter": True,
    }
    changes = [
        [set_op("connections.prod.protect", [{"table": "*.*"}, cards])],
        [set_op("connections.prod.protect", [{"table": "billing.cards"}, cards])],
        [
            {"op": "rename", "path": ["connections", "prod"], "to": "stage"},
            set_op("connections.stage.environment", "staging"),
            {"op": "unset", "path": ["connections", "stage", "protect"]},
            {"op": "unset", "path": ["connections", "stage", "max_plan_cost"]},
        ],
        [set_op("connections.fresh", {"host": "db.internal", "dbname": "app"})],
        [set_op("audit.path", "/dev/null")],
        [set_op("connections.dev.role", "postgres")],
        [set_op("connections.dev.params.hostaddr", "10.0.0.9")],
        [set_op("connections.dev.import_dir", "~")],
        [set_op("connections.dev.export_dir", "~/.ssh")],
        [set_op("connections.dev.lock_timeout", 0)],
    ]
    for ops in changes:
        preview = (await panel.post("/api/config/preview", {"base": base, "ops": ops})).json()
        assert preview["valid"] is True, (ops, preview["errors"])
        assert preview["needs_acknowledgement"] is True, ops
        refused = await panel.post("/api/config/apply", {"base": base, "ops": ops})
        assert (refused.status, refused.error) == (409, "acknowledge"), ops
    assert config_file.read_text(encoding="utf-8") == CONFIG


async def test_the_browser_is_never_given_the_key_on_a_command_line(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pgharbor.ui import server

    gate = PanelClient().gate
    opened: list[str] = []
    seen: dict[str, object] = {}

    def fake_open(address: str) -> bool:
        opened.append(address)
        page = Path(url2pathname(urlparse(address).path))  # also right for C:\\ paths
        seen["content"] = page.read_text(encoding="utf-8")
        seen["mode"] = page.stat().st_mode & 0o777
        seen["page"] = page
        return True

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(server.webbrowser, "open", fake_open)
    monkeypatch.setattr(server.asyncio, "sleep", no_wait)
    await server._open_browser(gate)

    [address] = opened
    assert address.startswith("file://")
    assert gate.token not in address  # what a process list would show
    assert gate.url in str(seen["content"])  # the key travels inside the private file
    if sys.platform != "win32":
        assert seen["mode"] == 0o600
    assert not Path(str(seen["page"])).exists()  # and the file is gone afterwards


async def test_a_pause_follows_a_renamed_connection(panel: PanelClient, config_file: Path) -> None:
    await panel.post("/api/pause", {"paused": True, "connections": ["dev"]})
    config = await current(panel)
    renamed = await panel.post(
        "/api/config/apply",
        {
            "base": digest_of(config),
            "ops": [{"op": "rename", "path": ["connections", "dev"], "to": "desenv"}],
        },
    )
    assert renamed.status == 200, renamed.text
    pause = read_pause()
    assert pause is not None
    # Both names: sessions that are running still know the connection by the old one.
    assert set(pause.connections) == {"dev", "desenv"}
