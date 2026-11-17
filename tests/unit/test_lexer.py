"""The SQL scanner: token boundaries that matter for safety."""

import pytest

from pgharbor.lexer import Kind, LexError, fold_identifier, tokenize


def kinds(sql: str) -> list[tuple[str, str]]:
    return [(t.kind.value, t.value) for t in tokenize(sql)]


def names(sql: str) -> list[str]:
    return [t.value for t in tokenize(sql) if t.is_name]


def test_basic_statement() -> None:
    assert kinds("SELECT a.b, 1.5 FROM t WHERE x >= $1::int;") == [
        ("ident", "select"), ("ident", "a"), ("punct", "."), ("ident", "b"), ("punct", ","),
        ("number", "1.5"), ("ident", "from"), ("ident", "t"), ("ident", "where"),
        ("ident", "x"), ("op", ">="), ("param", "$1"), ("punct", "::"), ("ident", "int"),
        ("punct", ";"),
    ]  # fmt: skip


def test_identifiers_fold_like_the_server() -> None:
    assert names('SELECT FooBar, "FooBar", "a""b"') == ["select", "foobar", "FooBar", 'a"b']
    assert fold_identifier("ABCÉ") == "abcÉ"  # only ASCII is lowered in a UTF-8 database
    assert tokenize('"MiXed"')[0].kind is Kind.QIDENT
    assert tokenize('"MiXed"')[0].value == "MiXed"


def test_identifier_is_clipped_to_63_bytes() -> None:
    long_name = "a" * 80
    assert tokenize(long_name)[0].value == "a" * 63
    assert tokenize(f'"{long_name}"')[0].value == "a" * 63
    accented = "é" * 40  # 80 bytes in UTF-8
    assert len(tokenize(accented)[0].value.encode()) <= 63


def test_non_ascii_characters_belong_to_identifiers() -> None:
    # A no-break space is not whitespace for PostgreSQL: it is part of the name.
    assert names("pg_sleep (1)") == ["pg_sleep "]
    assert names("naïve_fn(1)") == ["naïve_fn"]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 'it''s' , f(1)", ["select", "f"]),
        ("SELECT 'a\\' , f(1) --'", ["select", "f"]),  # backslash is ordinary: string ends
        ("SELECT 'a\\'' , f(1) --'", ["select"]),  # the doubled quote keeps the string open
        ("SELECT E'a\\' , f(1) --', g(2)", ["select", "g"]),  # in E'' the backslash escapes
        ("SELECT $$ f(1) $$, g(2)", ["select", "g"]),
        ("SELECT $tag$ $$ f(1) $x$ $tag$, g(2)", ["select", "g"]),
        ("SELECT B'01', X'ff', g(2)", ["select", "g"]),
        ("SELECT N'x', g(2)", ["select", "n", "g"]),
        ("SELECT a$b$c, g(2)", ["select", "a$b$c", "g"]),  # $ inside an identifier
    ],
)
def test_strings_hide_and_reveal_the_right_names(sql: str, expected: list[str]) -> None:
    assert names(sql) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1 -- f(1)\n, g(2)", ["select", "g"]),
        ("SELECT 1 -- f(1)\r, g(2)", ["select", "g"]),
        ("SELECT /* f(1) */ g(2)", ["select", "g"]),
        ("SELECT /* a /* f(1) */ still comment */ g(2)", ["select", "g"]),
        ("SELECT 1 +-- f(1)\n g(2)", ["select", "g"]),
        ("SELECT 1 */* f(1) */ g(2)", ["select", "g"]),
        ("SELECT pg_sleep/**/(1)", ["select", "pg_sleep"]),
    ],
)
def test_comments(sql: str, expected: list[str]) -> None:
    assert names(sql) == expected


def test_call_position_survives_whitespace_and_comments() -> None:
    for sql in ("pg_sleep(1)", "pg_sleep (1)", "pg_sleep\n\t(1)", "pg_sleep/* x */(1)",
                '"pg_sleep"(1)', "pg_catalog.pg_sleep(1)", "PG_SLEEP(1)"):  # fmt: skip
        tokens = tokenize(sql)
        index = next(i for i, t in enumerate(tokens) if t.value == "pg_sleep")
        assert tokens[index + 1].is_punct("("), sql


def test_numbers_are_conservative() -> None:
    assert kinds("1e5 1.e5 .5 1..2") == [
        ("number", "1e5"), ("number", "1.e5"), ("number", ".5"),
        ("number", "1"), ("punct", ".."), ("number", "2"),
    ]  # fmt: skip
    # Newer literal forms leave an extra identifier behind: stricter, never looser.
    assert names("0x1F 1_000") == ["x1f", "_000"]


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT 'abc", "unterminated quoted string"),
        ("SELECT E'abc\\'", "unterminated quoted string"),
        ('SELECT "abc', "unterminated quoted identifier"),
        ('SELECT ""', "zero-length delimited identifier"),
        ("SELECT /* abc", "unterminated /* comment"),
        ("SELECT /* a /* b */", "unterminated /* comment"),
        ("SELECT $a$ abc", "unterminated dollar-quoted string"),
        ("SELECT $ 1", "unexpected character '$'"),
        ("SELECT $a b", "unexpected character '$'"),
        ("SELECT 1 \\g", "unexpected character"),
        ("SELECT {1}", "unexpected character"),
        ("SELECT U&'\\0041'", "Unicode escape"),
        ('SELECT u&"\\0041"', "Unicode escape"),
        ("SELECT 1\x00", "NUL character"),
    ],
)
def test_unsafe_text_is_refused(sql: str, reason: str) -> None:
    with pytest.raises(LexError) as caught:
        tokenize(sql)
    assert reason in str(caught.value)


def test_error_position_is_one_based() -> None:
    with pytest.raises(LexError) as caught:
        tokenize("SELECT {")
    assert caught.value.pos == 7
    assert "character 8" in str(caught.value)
