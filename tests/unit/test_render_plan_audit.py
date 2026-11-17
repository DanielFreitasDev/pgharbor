"""Output shaping, credential scrubbing, plan reading and the audit log."""

import json
import os
from pathlib import Path

from pgharbor.audit import Audit
from pgharbor.config import AuditConfig
from pgharbor.plan import summarize_plan
from pgharbor.render import (
    clip,
    compile_column_patterns,
    render_csv,
    render_markdown,
    scrub,
    scrub_deep,
    to_json,
)


def test_scrub_removes_credentials_and_nothing_else() -> None:
    cases = {
        "host=db password=hunter2 user=x": "host=db password=[redacted] user=x",
        "host=db PASSWORD = 'hun ter2' user=x": "host=db PASSWORD=[redacted] user=x",
        "postgresql://app:hunter2@db:5432/x": "postgresql://app:[redacted]@db:5432/x",
        "ALTER ROLE a PASSWORD 'hunter2'": "ALTER ROLE a PASSWORD '[redacted]'",
        "create user a with encrypted password 'md5abc'": "create user a with encrypted password '[redacted]'",
        "sslpassword=abc sslmode=require": "sslpassword=[redacted] sslmode=require",
    }
    for text, expected in cases.items():
        assert scrub(text) == expected
    for harmless in ("SELECT password_hash FROM users", "https://example.com/a?b=c",
                     "the password column", "time: 12:30@home"):  # fmt: skip
        assert scrub(harmless) == harmless


def test_scrub_deep_walks_structures() -> None:
    value = {"a": ["password=x", {"b": "postgres://u:p@h/d"}], "n": 5, "none": None}
    assert scrub_deep(value) == {
        "a": ["password=[redacted]", {"b": "postgres://u:[redacted]@h/d"}],
        "n": 5,
        "none": None,
    }


def test_column_patterns() -> None:
    pattern = compile_column_patterns(["password", "(^|_)token($|_)", "senha"])
    assert pattern is not None
    for name in ("password", "user_password", "PASSWORD_HASH", "api_token", "token", "ftp_senha"):
        assert pattern.search(name), name
    for name in ("tokens_used", "bypass", "compass", "description"):
        assert not pattern.search(name), name
    assert compile_column_patterns([]) is None


def test_clip_and_json() -> None:
    assert clip("abc", 5) == ("abc", False)
    text, cut = clip("abcdefghij", 4)
    assert cut
    assert text.startswith("abcd…")
    assert "+6 chars" in text
    assert to_json({"a": [1, None, "é"], "b": {1, 2}}) == '{"a":[1,null,"é"],"b":[1,2]}'


def test_tables() -> None:
    markdown = render_markdown(["a", "b"], [[1, "x|y"], [None, "line\nbreak"]])
    assert markdown.splitlines() == ["| a | b |", "|---|---|", "| 1 | x\\|y |", "|  | line break |"]
    assert (
        render_csv(["a", "b"], [[1, "x,y"], [True, {"k": 1}]]) == 'a,b\n1,"x,y"\ntrue,"{""k"":1}"\n'
    )


PLAN = [
    {
        "Plan": {
            "Node Type": "Limit",
            "Total Cost": 123.5,
            "Plan Rows": 10,
            "Output": ['"left"((f.*)::text, 2001)'],
            "Plans": [
                {
                    "Node Type": "Nested Loop",
                    "Total Cost": 100,
                    "Plan Rows": 10,
                    "Join Filter": "(a.id = b.id)",
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "files",
                            "Schema": "vault",
                            "Alias": "f",
                            "Output": ["f.id", "f.*"],
                            "Filter": "(f.folder_id = 3)",
                        },
                        {
                            "Node Type": "Index Scan",
                            "Relation Name": "folders",
                            "Schema": "vault",
                            "Alias": "d",
                            "Output": ["d.path"],
                            "Index Cond": "(d.id = f.folder_id)",
                        },
                        {
                            "Node Type": "Index Only Scan",
                            "Relation Name": "orders",
                            "Schema": "shop",
                            "Alias": "o",
                            "Output": ["o.id"],
                        },
                        {
                            "Node Type": "Function Scan",
                            "Function Name": "generate_series",
                            "Schema": "pg_catalog",
                            "Function Call": "generate_series(1, 3)",
                        },
                    ],
                }
            ],
        }
    }
]


def test_plan_summary() -> None:
    summary = summarize_plan(json.dumps(PLAN))
    assert summary.total_cost == 123.5
    assert summary.relations() == ["vault.files", "vault.folders", "shop.orders"]
    files, folders, orders = summary.scans
    assert files.sequential
    assert files.reads_everything
    assert not folders.reads_everything  # it has an index condition
    assert orders.reads_everything  # an index scan with no condition walks everything
    assert summary.uses_whole_row(files)
    assert not summary.uses_whole_row(folders)
    assert summary.functions == ["pg_catalog.generate_series"]
    assert "generate_series(1, 3)" in summary.expressions


def test_audit_log(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    audit = Audit(AuditConfig(path=str(path)))
    audit.record(
        tool="pg_query",
        connection="dev",
        outcome="ok",
        sql="SELECT 1 -- password=hunter2",
        params=[1],
        rows=1,
        elapsed_ms=3.14159,
    )
    audit.record(
        tool="pg_query",
        connection="prod",
        outcome="refused",
        rule="function-denied",
        sql="SELECT pg_sleep(1)",
        detail="pg_sleep() is not allowed",
    )
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["sql"] == "SELECT 1 -- password=[redacted]"
    assert "params" not in lines[0]  # parameters are not logged unless asked
    assert lines[0]["ms"] == 3.1
    assert lines[1]["rule"] == "function-denied"
    if os.name == "posix":  # Windows has no permission bits to check
        assert path.stat().st_mode & 0o777 == 0o600
    assert [entry["connection"] for entry in audit.tail(10)] == ["dev", "prod"]
    assert [entry["connection"] for entry in audit.tail(1)] == ["prod"]

    quiet = Audit(AuditConfig(enabled=False, path=str(tmp_path / "none.jsonl")))
    quiet.record(tool="x", connection=None, outcome="ok")
    assert not (tmp_path / "none.jsonl").exists()

    no_sql = Audit(AuditConfig(path=str(tmp_path / "nosql.jsonl"), log_sql=False, log_params=True))
    no_sql.record(tool="x", connection="a", outcome="ok", sql="SELECT 1", params=[7])
    entry = json.loads((tmp_path / "nosql.jsonl").read_text())
    assert "sql" not in entry
    assert entry["params"] == [7]


def test_audit_never_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x")
    audit = Audit(AuditConfig(path=str(blocked / "audit.jsonl")))  # parent is a file
    audit.record(tool="x", connection=None, outcome="ok")
    assert audit.tail() == []
