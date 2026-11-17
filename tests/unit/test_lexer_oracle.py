"""The scanner against PostgreSQL's own scanner.

``pglast`` embeds the real PostgreSQL scanner. It is a test-only dependency (it is
GPL-licensed, so it is never installed with pgharbor): ``uv sync --group oracle``.

The property checked is the one safety depends on. For any text both scanners accept:
every identifier the real scanner sees, and every identifier it sees directly before an
opening parenthesis (a possible function call), is also seen by pgharbor's scanner, in
the same order. pgharbor may see *more* (it is allowed to be stricter), never less.
"""

import random

import pytest

from pgharbor.lexer import LexError, fold_identifier, tokenize

pglast_parser = pytest.importorskip("pglast.parser")

_KEYWORD_KINDS = {
    "UNRESERVED_KEYWORD", "COL_NAME_KEYWORD", "TYPE_FUNC_NAME_KEYWORD", "RESERVED_KEYWORD",
}  # fmt: skip

FRAGMENTS = [
    "'", "''", '"', '""', "\\", "\\'", "$", "$$", "$a$", "$b$", "$1", "--", "/*", "*/", "\n",
    "\r", " ", "\t", "E'", "e'", "B'", "x'", "N'", "(", ")", ",", ";", ".", "..", "::", ":",
    "+", "-", "*", "/", "=", "<>", "@", "#", "|", "~", "!", "%", "^", "&", "?", "`",
    "pg_sleep", "select", "a", "b_1", "Foo", "é", "ñ$x", "x$", "_", "1", "2.5", "1e5", "1e",
    ".5", "0x1F", "1_0", "abc", "f", "from", "t", "where", "as", "É", " ", "　", "]",
    "[", "$a", "a$b$c", "'x'", "$$x$$", "/* c */", "-- c\n", "e", "E", "b", "x", "n", "\f",
]  # fmt: skip


def real(sql: str) -> list[object] | None:
    try:
        tokens = pglast_parser.scan(sql)
    except Exception:
        return None
    out: list[object] = []
    for token in tokens:
        if token.name in ("C_COMMENT", "SQL_COMMENT"):
            continue  # the scanner reports comments; the parser never sees them
        text = sql[token.start : token.end + 1]
        if token.name == "IDENT" or token.kind in _KEYWORD_KINDS:
            if text.startswith('"'):
                out.append(("id", text[1:-1].replace('""', '"')))
            else:
                out.append(("id", fold_identifier(text)))
        elif token.name == "ASCII_40":
            out.append("(")
        else:
            out.append("other")
    return out


def mine(sql: str) -> list[object] | None:
    try:
        tokens = tokenize(sql)
    except LexError:
        return None
    out: list[object] = []
    for token in tokens:
        if token.is_name:
            out.append(("id", token.value))
        elif token.is_punct("("):
            out.append("(")
        else:
            out.append("other")
    return out


def identifiers(stream: list[object]) -> list[object]:
    return [item for item in stream if isinstance(item, tuple)]


def calls(stream: list[object]) -> list[object]:
    return [
        stream[i]
        for i in range(len(stream) - 1)
        if isinstance(stream[i], tuple) and stream[i + 1] == "("
    ]


def is_subsequence(needle: list[object], haystack: list[object]) -> bool:
    iterator = iter(haystack)
    return all(item in iterator for item in needle)


@pytest.mark.parametrize("seed", range(6))
def test_never_sees_less_than_postgresql(seed: int) -> None:
    generator = random.Random(seed)
    compared = 0
    for _ in range(40_000):
        sql = "".join(generator.choice(FRAGMENTS) for _ in range(generator.randint(1, 14)))
        theirs = real(sql)
        ours = mine(sql)
        if theirs is None or ours is None:
            continue  # refused by one side: the statement never runs
        compared += 1
        assert is_subsequence(identifiers(theirs), identifiers(ours)), repr(sql)
        assert is_subsequence(calls(theirs), calls(ours)), repr(sql)
    assert compared > 5_000


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(1)",
        'SELECT "pg_sleep" /* c */ (1), a.b.c(2) FROM t -- x',
        "SELECT 'a\\'' , pg_sleep(1) -- '",
        "SELECT E'a\\'' , pg_sleep(1) -- '",
        "SELECT $q$ pg_sleep(1) $q$, lower('X')",
        "WITH x AS (SELECT 1) SELECT * FROM x WHERE a OPERATOR(pg_catalog.+) 1 > 0",
        "SELECT count(*) FILTER (WHERE a) OVER (PARTITION BY b) FROM t",
    ],
)
def test_same_view_on_realistic_statements(sql: str) -> None:
    theirs, ours = real(sql), mine(sql)
    assert theirs is not None
    assert ours is not None
    assert identifiers(theirs) == identifiers(ours)
    assert calls(theirs) == calls(ours)
