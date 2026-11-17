"""Lexical analysis of statements (no database involved)."""

import pytest

from pgharbor.errors import Rejected
from pgharbor.guard import (
    analyze,
    name_variants,
    parse_explain,
    parse_show,
    quote_ident,
    quote_literal,
    quote_qualified,
)


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT 1", "query"),
        ("  select 1 ;", "query"),
        ("(SELECT 1) UNION (SELECT 2)", "query"),
        ("VALUES (1)", "query"),
        ("TABLE t", "query"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "query"),
        ("WITH x AS (SELECT 1), y AS (SELECT 2) SELECT * FROM x, y", "query"),
        ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "dml"),
        ("WITH x AS (SELECT 1) UPDATE t SET a = 1", "dml"),
        ("WITH x AS (SELECT 1) DELETE FROM t", "dml"),
        ("WITH RECURSIVE x(n) AS (SELECT 1) SELECT * FROM x", "query"),
        ("INSERT INTO t VALUES (1)", "dml"),
        ("MERGE INTO t USING s ON true WHEN MATCHED THEN DELETE", "dml"),
        ("CREATE TABLE t (a int)", "ddl"),
        ("DROP TABLE t", "ddl"),
        ("TRUNCATE t", "ddl"),
        ("GRANT SELECT ON t TO r", "ddl"),
        ("VACUUM t", "maintenance"),
        ("ANALYZE", "maintenance"),
        ("SET x = 1", "session"),
        ("BEGIN", "session"),
        ("COMMIT", "session"),
        ("COPY t TO STDOUT", "session"),
        ("DO $$ BEGIN END $$", "session"),
        ("CALL p()", "session"),
        ("LOCK TABLE t", "session"),
        ("SHOW work_mem", "show"),
        ("EXPLAIN SELECT 1", "explain"),
        ("FOOBAR", "other"),
    ],
)
def test_statement_kind(sql: str, kind: str) -> None:
    assert analyze(sql).kind == kind


def test_trailing_semicolons_are_removed_from_the_statement() -> None:
    analysis = analyze("SELECT 1 ;; ")
    assert analysis.sql == "SELECT 1"


@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        ("SELECT 1; SELECT 2", "multiple-statements"),
        ("SELECT 1;;SELECT 2", "multiple-statements"),
        ("", "empty-statement"),
        ("  -- nothing\n", "empty-statement"),
        (";", "empty-statement"),
        ("SELECT (1", "unbalanced-parentheses"),
        ("SELECT 1)", "unbalanced-parentheses"),
        ("SELECT a[1)", "unbalanced-parentheses"),
        ("SELECT 'x", "lexical"),
        ("SELECT " + "1," * 60_000 + "1", "sql-too-long"),
    ],
)
def test_statements_refused_before_reaching_the_server(sql: str, rule: str) -> None:
    with pytest.raises(Rejected) as caught:
        analyze(sql)
    assert caught.value.rule == rule


def test_a_semicolon_inside_a_string_or_comment_is_not_a_separator() -> None:
    assert analyze("SELECT ';' AS a -- ; \n").kind == "query"
    assert analyze("SELECT $$ ; $$ /* ; */").kind == "query"


def test_call_candidates() -> None:
    analysis = analyze(
        'SELECT lower(a), s.f(1), "Mixed"(2), count(*) FILTER (WHERE x) '
        "FROM t WHERE b IN (1, 2) AND EXISTS (SELECT 1)"
    )
    calls = {(c.schema, c.name, c.quoted) for c in analysis.calls}
    assert (None, "lower", False) in calls
    assert ("s", "f", False) in calls
    assert (None, "Mixed", True) in calls
    assert (None, "count", False) in calls
    # Keywords followed by "(" are reported too: the catalog lookup sorts them out.
    assert {"in", "exists", "filter"} <= analysis.call_names
    assert "Mixed" in analysis.call_names
    assert "mixed" not in analysis.call_names


def test_tablesample_method_is_not_a_call() -> None:
    analysis = analyze("SELECT a FROM t TABLESAMPLE SYSTEM (10) REPEATABLE (42) WHERE f(a) > 0")
    assert "system" not in analysis.call_names
    assert "f" in analysis.call_names
    assert "system" in analysis.names


def test_attribute_names_and_all_names() -> None:
    analysis = analyze("SELECT t.col, (t).other, x FROM sch.tab t")
    assert {"col", "other", "tab"} <= analysis.attribute_names
    assert {"t", "col", "other", "x", "sch", "tab", "select", "from"} <= analysis.names


def test_unquoted_names_are_checked_in_both_foldings() -> None:
    assert name_variants("FUNÇÃO", quoted=False) == {"FUNÇÃO", "função"}
    assert name_variants("Função", quoted=True) == {"Função"}
    analysis = analyze("SELECT funÇÃo(1)")  # as the lexer folds it in a UTF-8 database
    assert {"funÇÃo", "função"} <= analysis.call_names


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t", [None]),
        ("SELECT t.* FROM t", ["t"]),
        ("SELECT a, * FROM t", [None]),
        ("SELECT DISTINCT * FROM t", [None]),
        ("SELECT DISTINCT ON (a) * FROM t", [None]),
        ("SELECT count(*) FROM t", []),
        ("SELECT a * b FROM t", []),
        ("SELECT 2 * 3", []),
        ("SELECT (a) * 2 FROM t", []),
        ("SELECT (t.*) FROM t", ["t"]),
        ("SELECT count(*), s.* FROM s", ["s"]),
    ],
)
def test_star_detection(sql: str, expected: list[str | None]) -> None:
    assert [qualifier for _index, qualifier in analyze(sql).stars] == expected


def test_parameters_are_counted() -> None:
    assert analyze("SELECT $1, $3, $2").param_count == 3
    assert analyze("SELECT '$5'").param_count == 0


@pytest.mark.parametrize(
    ("sql", "setting"),
    [
        ("SHOW work_mem", "work_mem"),
        ("SHOW ALL", "all"),
        ("show time zone", "timezone"),
        ("SHOW TRANSACTION ISOLATION LEVEL", "transaction_isolation"),
        ("SHOW SESSION AUTHORIZATION", "session_authorization"),
        ("SHOW pg_stat_statements.max", "pg_stat_statements.max"),
    ],
)
def test_show(sql: str, setting: str) -> None:
    assert parse_show(analyze(sql)) == setting


def test_show_refuses_what_it_does_not_know() -> None:
    with pytest.raises(Rejected):
        parse_show(analyze("SHOW a b c d"))


def test_explain_options_are_parsed() -> None:
    plain = parse_explain(analyze("EXPLAIN SELECT 1"))
    assert plain.options == {}
    assert plain.statement == "SELECT 1"
    old = parse_explain(analyze("EXPLAIN ANALYZE VERBOSE SELECT 1"))
    assert old.options == {"analyze": True, "verbose": True}
    new = parse_explain(
        analyze("EXPLAIN (ANALYSE, BUFFERS false, FORMAT json, costs off) SELECT a FROM t")
    )
    assert new.options == {"analyze": True, "buffers": False, "format": "json", "costs": False}
    assert new.statement == "SELECT a FROM t"
    with pytest.raises(Rejected):
        parse_explain(analyze("EXPLAIN (nonsense) SELECT 1"))
    with pytest.raises(Rejected):
        parse_explain(analyze("EXPLAIN"))


def test_quoting() -> None:
    assert quote_ident("simple") == '"simple"'
    assert quote_ident('we"ird') == '"we""ird"'
    assert quote_qualified("s", "t") == '"s"."t"'
    assert quote_qualified(None, "t") == '"t"'
    assert quote_literal("it's") == "'it''s'"
    with pytest.raises(ValueError, match="NUL"):
        quote_ident("a\x00b")
