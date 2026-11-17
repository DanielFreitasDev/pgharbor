"""A PostgreSQL lexical scanner in pure Python.

This module splits a SQL string into tokens following the rules of PostgreSQL's own
scanner (``src/backend/parser/scan.l``). It exists for one security purpose: pgharbor
must know *which names a statement can call* before the statement reaches the server.

Design rules
------------
* **Fail closed.** Anything the scanner is not sure about (an unterminated string, a
  stray character, ``U&`` Unicode escapes) raises :class:`LexError`. The statement is
  then refused instead of guessed at.
* **No grammar.** The scanner never decides what a statement *means*. That is the
  server's job: pgharbor sends read queries wrapped as a sub-select through the extended
  query protocol, so the server's parser is the judge of statement type and count.
* **Same assumptions as the server.** Plain ``'...'`` strings are scanned with
  ``standard_conforming_strings = on``. pgharbor forces and verifies that setting in
  every transaction, because a server running with it ``off`` would read ``'\\''``
  differently and code could hide inside what looks like a string literal here.

The faithfulness of this scanner is tested against the real PostgreSQL scanner
(see ``tests/unit/test_lexer_oracle.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["Kind", "LexError", "Token", "fold_identifier", "tokenize"]

# PostgreSQL truncates identifiers to NAMEDATALEN - 1 bytes.
_MAX_IDENTIFIER_BYTES = 63

_WHITESPACE = frozenset(" \t\n\r\f\v")
_OPERATOR_CHARS = frozenset("~!@#^&|`?+-*/%<>=")
_SINGLE_PUNCT = frozenset("()[],;")
_DIGITS = frozenset("0123456789")
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


class Kind(str, Enum):
    """Token categories. Whitespace and comments never become tokens."""

    IDENT = "ident"  # unquoted identifier or keyword; ``value`` is case-folded
    QIDENT = "qident"  # "quoted identifier"; ``value`` is the exact name
    STRING = "string"  # any string constant: '..', E'..', B'..', X'..', $tag$..$tag$
    NUMBER = "number"
    PARAM = "param"  # $1, $2, ...
    OP = "op"  # a run of operator characters
    PUNCT = "punct"  # ( ) [ ] , ; . : :: := ..


@dataclass(frozen=True, slots=True)
class Token:
    kind: Kind
    text: str  # exact source text
    value: str  # normalized form (see Kind)
    pos: int  # offset of the first character in the source

    def is_word(self, *words: str) -> bool:
        """True for an unquoted identifier equal to one of ``words`` (lowercase)."""
        return self.kind is Kind.IDENT and self.value in words

    def is_punct(self, *symbols: str) -> bool:
        return self.kind is Kind.PUNCT and self.text in symbols

    @property
    def is_name(self) -> bool:
        return self.kind is Kind.IDENT or self.kind is Kind.QIDENT


class LexError(ValueError):
    """The SQL text cannot be scanned safely."""

    def __init__(self, message: str, pos: int) -> None:
        super().__init__(f"{message} (at character {pos + 1})")
        self.reason = message
        self.pos = pos


def _truncate(name: str) -> str:
    """Clip an identifier to 63 bytes on a character boundary, like the server does."""
    raw = name.encode("utf-8", errors="surrogatepass")
    if len(raw) <= _MAX_IDENTIFIER_BYTES:
        return name
    return raw[:_MAX_IDENTIFIER_BYTES].decode("utf-8", errors="ignore")


def fold_identifier(name: str) -> str:
    """Case-fold an unquoted identifier the way PostgreSQL does in a UTF-8 database.

    Only ASCII letters are lowered. Servers with a single-byte encoding may also lower
    non-ASCII letters; callers that compare names for safety should also try
    ``name.lower()`` (see ``guard.name_variants``).
    """
    return _truncate("".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in name))


def _is_ident_start(c: str) -> bool:
    return c in _ASCII_LETTERS or c == "_" or ord(c) >= 128


def _is_ident_cont(c: str) -> bool:
    return c in _ASCII_LETTERS or c in _DIGITS or c == "_" or c == "$" or ord(c) >= 128


def tokenize(sql: str) -> list[Token]:
    """Split ``sql`` into tokens. Raises :class:`LexError` when it cannot do so safely."""
    if "\x00" in sql:
        raise LexError("NUL character in SQL", sql.index("\x00"))

    tokens: list[Token] = []
    n = len(sql)
    i = 0
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if c in _WHITESPACE:
            i += 1
            continue

        if c == "-" and nxt == "-":
            i = _skip_line_comment(sql, i)
            continue

        if c == "/" and nxt == "*":
            i = _skip_block_comment(sql, i)
            continue

        if c == "'":
            end = _scan_quoted(sql, i, i + 1, backslash_escapes=False, doubled_quotes=True)
            tokens.append(Token(Kind.STRING, sql[i:end], sql[i:end], i))
            i = end
            continue

        if c == '"':
            end, name = _scan_quoted_identifier(sql, i)
            tokens.append(Token(Kind.QIDENT, sql[i:end], name, i))
            i = end
            continue

        if c == "$":
            if nxt in _DIGITS:
                j = i + 1
                while j < n and sql[j] in _DIGITS:
                    j += 1
                tokens.append(Token(Kind.PARAM, sql[i:j], sql[i:j], i))
                i = j
                continue
            end = _scan_dollar_quoted(sql, i)
            tokens.append(Token(Kind.STRING, sql[i:end], sql[i:end], i))
            i = end
            continue

        if _is_ident_start(c):
            if nxt == "'" and c in "eE":
                end = _scan_quoted(sql, i, i + 2, backslash_escapes=True, doubled_quotes=True)
                tokens.append(Token(Kind.STRING, sql[i:end], sql[i:end], i))
                i = end
                continue
            if nxt == "'" and c in "bBxX":
                # Bit and hex strings end at the first quote: '' is not an escape there.
                end = _scan_quoted(sql, i, i + 2, backslash_escapes=False, doubled_quotes=False)
                tokens.append(Token(Kind.STRING, sql[i:end], sql[i:end], i))
                i = end
                continue
            if c in "uU" and nxt == "&" and i + 2 < n and sql[i + 2] in "'\"":
                raise LexError(
                    "Unicode escape syntax (U&'...' or U&\"...\") is not accepted; "
                    "write the characters directly",
                    i,
                )
            j = i + 1
            while j < n and _is_ident_cont(sql[j]):
                j += 1
            text = sql[i:j]
            tokens.append(Token(Kind.IDENT, text, fold_identifier(text), i))
            i = j
            continue

        if c in _DIGITS or (c == "." and nxt in _DIGITS):
            j = _scan_number(sql, i)
            tokens.append(Token(Kind.NUMBER, sql[i:j], sql[i:j], i))
            i = j
            continue

        if c == ":":
            width = 2 if nxt in (":", "=") else 1
            tokens.append(Token(Kind.PUNCT, sql[i : i + width], sql[i : i + width], i))
            i += width
            continue

        if c == ".":
            width = 2 if nxt == "." else 1
            tokens.append(Token(Kind.PUNCT, sql[i : i + width], sql[i : i + width], i))
            i += width
            continue

        if c in _SINGLE_PUNCT:
            tokens.append(Token(Kind.PUNCT, c, c, i))
            i += 1
            continue

        if c in _OPERATOR_CHARS:
            j = i + 1
            while j < n and sql[j] in _OPERATOR_CHARS:
                # An operator never swallows the start of a comment.
                if sql[j] == "-" and sql[j + 1 : j + 2] == "-":
                    break
                if sql[j] == "/" and sql[j + 1 : j + 2] == "*":
                    break
                j += 1
            tokens.append(Token(Kind.OP, sql[i:j], sql[i:j], i))
            i = j
            continue

        raise LexError(f"unexpected character {c!r}", i)

    return tokens


def _skip_line_comment(sql: str, start: int) -> int:
    """Return the index of the line break that ends a ``--`` comment (or end of text)."""
    n = len(sql)
    j = start + 2
    while j < n and sql[j] not in "\n\r":
        j += 1
    return j


def _skip_block_comment(sql: str, start: int) -> int:
    """Skip a ``/* ... */`` comment. PostgreSQL block comments nest."""
    n = len(sql)
    depth = 1
    j = start + 2
    while j < n:
        two = sql[j : j + 2]
        if two == "/*":
            depth += 1
            j += 2
        elif two == "*/":
            depth -= 1
            j += 2
            if depth == 0:
                return j
        else:
            j += 1
    raise LexError("unterminated /* comment", start)


def _scan_quoted(
    sql: str, start: int, body: int, *, backslash_escapes: bool, doubled_quotes: bool
) -> int:
    """Scan a single-quoted constant whose content starts at ``body``; return its end."""
    n = len(sql)
    j = body
    while j < n:
        c = sql[j]
        if backslash_escapes and c == "\\":
            j += 2
            continue
        if c == "'":
            if doubled_quotes and sql[j + 1 : j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    raise LexError("unterminated quoted string", start)


def _scan_quoted_identifier(sql: str, start: int) -> tuple[int, str]:
    n = len(sql)
    j = start + 1
    parts: list[str] = []
    while j < n:
        c = sql[j]
        if c == '"':
            if sql[j + 1 : j + 2] == '"':
                parts.append('"')
                j += 2
                continue
            name = "".join(parts)
            if not name:
                raise LexError("zero-length delimited identifier", start)
            return j + 1, _truncate(name)
        parts.append(c)
        j += 1
    raise LexError("unterminated quoted identifier", start)


def _scan_dollar_quoted(sql: str, start: int) -> int:
    """Scan ``$tag$ ... $tag$``. ``start`` points at the first ``$``."""
    n = len(sql)
    j = start + 1
    if j < n and sql[j] != "$":
        if not (sql[j] in _ASCII_LETTERS or sql[j] == "_" or ord(sql[j]) >= 128):
            raise LexError("unexpected character '$'", start)
        j += 1
        while j < n and (
            sql[j] in _ASCII_LETTERS or sql[j] in _DIGITS or sql[j] == "_" or ord(sql[j]) >= 128
        ):
            j += 1
    if j >= n or sql[j] != "$":
        raise LexError("unexpected character '$'", start)
    delimiter = sql[start : j + 1]
    close = sql.find(delimiter, j + 1)
    if close < 0:
        raise LexError("unterminated dollar-quoted string", start)
    return close + len(delimiter)


def _scan_number(sql: str, start: int) -> int:
    """Scan a numeric constant conservatively.

    Newer servers also accept ``0x1F``, ``0b101`` and ``1_000``. Those extra characters
    are left to be scanned as an identifier: a few more tokens can only make the safety
    checks stricter, never looser.
    """
    n = len(sql)
    j = start
    while j < n and sql[j] in _DIGITS:
        j += 1
    if j < n and sql[j] == "." and sql[j + 1 : j + 2] != ".":
        j += 1
        while j < n and sql[j] in _DIGITS:
            j += 1
    if j < n and sql[j] in "eE":
        k = j + 1
        if k < n and sql[k] in "+-":
            k += 1
        if k < n and sql[k] in _DIGITS:
            while k < n and sql[k] in _DIGITS:
                k += 1
            j = k
    return j
