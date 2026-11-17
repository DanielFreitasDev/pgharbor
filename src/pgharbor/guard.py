"""Lexical analysis of one SQL statement.

Nothing here talks to the database. :func:`analyze` turns SQL text into an
:class:`Analysis`: what kind of statement it looks like and every name it could call or
read. The decisions that need the catalog (is this function safe? is this a view?) live
in :mod:`pgharbor.inspect`; the decision about what the statement *is* belongs to the
server (see :mod:`pgharbor.runner`).

The analysis is deliberately conservative: a name that only *might* be a function call
is reported as one. Extra candidates cost a catalog lookup; a missed one could cost a
side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Rejected
from .lexer import Kind, LexError, Token, tokenize

__all__ = [
    "Analysis",
    "CallRef",
    "ExplainRequest",
    "analyze",
    "name_variants",
    "parse_explain",
    "parse_show",
    "quote_ident",
    "quote_literal",
    "quote_qualified",
]

# First keyword -> statement family.
_QUERY_WORDS = frozenset({"select", "values", "table"})
_DML_WORDS = frozenset({"insert", "update", "delete", "merge"})
_DDL_WORDS = frozenset(
    {
        "create",
        "alter",
        "drop",
        "truncate",
        "comment",
        "grant",
        "revoke",
        "security",
        "reassign",
        "import",
        "refresh",
    }
)
_MAINTENANCE_WORDS = frozenset({"vacuum", "analyze", "analyse", "reindex", "cluster", "checkpoint"})
# Statements that change the session or the transaction. No tool runs these as free SQL.
_SESSION_WORDS = frozenset(
    {
        "begin",
        "start",
        "commit",
        "end",
        "rollback",
        "abort",
        "savepoint",
        "release",
        "prepare",
        "execute",
        "deallocate",
        "set",
        "reset",
        "discard",
        "listen",
        "unlisten",
        "notify",
        "lock",
        "declare",
        "fetch",
        "move",
        "close",
        "do",
        "call",
        "load",
        "copy",
    }
)

# Keywords after which a ``*`` is "all columns", not a multiplication.
_STAR_CONTEXT_WORDS = frozenset({"select", "distinct", "all", "returning"})


@dataclass(frozen=True, slots=True)
class CallRef:
    """A name in call position: ``schema.name(`` or ``name(``."""

    schema: str | None
    name: str
    quoted: bool
    pos: int


@dataclass(slots=True)
class Analysis:
    sql: str  # the statement without trailing semicolons
    tokens: list[Token]
    kind: str  # query | show | explain | dml | ddl | maintenance | session | other
    verb: str  # first keyword, upper case, for messages
    calls: list[CallRef] = field(default_factory=list)
    attribute_names: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)  # every identifier, all variants
    stars: list[tuple[int, str | None]] = field(default_factory=list)  # (token index, qualifier)
    operators: set[str] = field(default_factory=set)
    param_count: int = 0

    @property
    def call_names(self) -> set[str]:
        out: set[str] = set()
        for call in self.calls:
            out |= name_variants(call.name, call.quoted)
        return out


def name_variants(name: str, quoted: bool) -> set[str]:
    """Every catalog spelling an identifier may resolve to.

    An unquoted identifier is lowered by the server. In a UTF-8 database only ASCII is
    lowered; in a single-byte encoding accented letters are lowered too. Both spellings
    are checked so a name can never dodge a lookup through case folding.
    """
    if quoted:
        return {name}
    return {name, name.lower()}


def quote_ident(name: str) -> str:
    """Quote an identifier for use inside SQL text."""
    if "\x00" in name:
        raise ValueError("NUL character in identifier")
    return '"' + name.replace('"', '""') + '"'


def quote_qualified(schema: str | None, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}" if schema else quote_ident(name)


def quote_literal(value: str) -> str:
    """Quote a string constant (valid with standard_conforming_strings = on)."""
    if "\x00" in value:
        raise ValueError("NUL character in literal")
    return "'" + value.replace("'", "''") + "'"


def analyze(sql: str, *, max_chars: int = 100_000) -> Analysis:
    """Scan ``sql`` and describe it. Raises :class:`Rejected` when it cannot be trusted."""
    if len(sql) > max_chars:
        raise Rejected(
            f"the statement has {len(sql)} characters; the limit is {max_chars}",
            rule="sql-too-long",
        )
    try:
        tokens = tokenize(sql)
    except LexError as exc:
        raise Rejected(
            str(exc),
            rule="lexical",
            hint="Check quotes, comments and dollar-quoted strings.",
        ) from exc

    # A statement may end with semicolons; anywhere else they separate statements.
    end = len(sql)
    while tokens and tokens[-1].is_punct(";"):
        end = tokens[-1].pos
        tokens.pop()
    if not tokens:
        raise Rejected("the statement is empty", rule="empty-statement")
    for token in tokens:
        if token.is_punct(";"):
            raise Rejected(
                "more than one statement was sent",
                rule="multiple-statements",
                hint="Send one statement per call.",
            )

    _check_balance(tokens)

    kind, verb = _classify(tokens)
    analysis = Analysis(sql=sql[:end].rstrip(), tokens=tokens, kind=kind, verb=verb)
    _collect(analysis)
    return analysis


def _check_balance(tokens: list[Token]) -> None:
    stack: list[str] = []
    pairs = {")": "(", "]": "["}
    for token in tokens:
        if token.kind is not Kind.PUNCT:
            continue
        if token.text in "([":
            stack.append(token.text)
        elif token.text in pairs:
            if not stack or stack[-1] != pairs[token.text]:
                raise Rejected(
                    f"unbalanced {token.text!r} at character {token.pos + 1}",
                    rule="unbalanced-parentheses",
                )
            stack.pop()
    if stack:
        raise Rejected(f"unclosed {stack[-1]!r}", rule="unbalanced-parentheses")


def _classify(tokens: list[Token]) -> tuple[str, str]:
    first = tokens[0]
    if first.is_punct("("):
        return "query", "SELECT"
    if first.kind is not Kind.IDENT:
        return "other", first.text[:20].upper()
    word = first.value
    if word in _QUERY_WORDS:
        return "query", word.upper()
    if word == "with":
        main = _main_verb_after_with(tokens)
        if main in _DML_WORDS:
            return "dml", f"WITH ... {main.upper()}"
        return "query", "WITH"
    if word == "show":
        return "show", "SHOW"
    if word == "explain":
        return "explain", "EXPLAIN"
    if word in _DML_WORDS:
        return "dml", word.upper()
    if word in _DDL_WORDS:
        return "ddl", word.upper()
    if word in _MAINTENANCE_WORDS:
        return "maintenance", word.upper()
    if word in _SESSION_WORDS:
        return "session", word.upper()
    return "other", word.upper()


def _main_verb_after_with(tokens: list[Token]) -> str | None:
    """Find the statement keyword that follows the list of common table expressions."""
    depth = 0
    closed_one = False
    for token in tokens[1:]:
        if token.kind is Kind.PUNCT and token.text in "([":
            depth += 1
        elif token.kind is Kind.PUNCT and token.text in ")]":
            depth -= 1
            if depth == 0:
                closed_one = True
        elif depth == 0 and closed_one and token.kind is Kind.IDENT:
            if token.value in _QUERY_WORDS or token.value in _DML_WORDS:
                return token.value
    return None


def _collect(analysis: Analysis) -> None:
    tokens = analysis.tokens
    last = len(tokens) - 1
    for i, token in enumerate(tokens):
        if token.kind is Kind.PARAM:
            analysis.param_count = max(analysis.param_count, int(token.text[1:]))
            continue
        if token.kind is Kind.OP:
            analysis.operators.add(token.text)
            if token.text == "*" and _is_star(tokens, i):
                qualifier = None
                if i >= 2 and tokens[i - 1].is_punct(".") and tokens[i - 2].is_name:
                    qualifier = tokens[i - 2].value
                analysis.stars.append((i, qualifier))
            continue
        if not token.is_name:
            continue

        quoted = token.kind is Kind.QIDENT
        analysis.names |= name_variants(token.value, quoted)
        after_dot = i >= 1 and tokens[i - 1].is_punct(".")
        if i < last and tokens[i + 1].is_punct("("):
            schema = None
            if after_dot and i >= 2 and tokens[i - 2].is_name:
                schema = tokens[i - 2].value
            analysis.calls.append(CallRef(schema, token.value, quoted, token.pos))
        elif after_dot:
            analysis.attribute_names |= name_variants(token.value, quoted)


def _is_star(tokens: list[Token], i: int) -> bool:
    """True when the ``*`` at ``i`` means "all columns"."""
    if i == 0:
        return True
    prev = tokens[i - 1]
    if prev.is_punct(".", ","):
        return True
    if prev.kind is Kind.IDENT and prev.value in _STAR_CONTEXT_WORDS:
        return True
    if prev.is_punct("("):
        # f(*) is an aggregate call such as count(*), not a column list.
        is_call = i >= 2 and tokens[i - 2].is_name
        closes = i + 1 < len(tokens) and tokens[i + 1].is_punct(")")
        return not (is_call and closes)
    if prev.is_punct(")"):
        # SELECT DISTINCT ON (a, b) * ...
        j = _matching_open(tokens, i - 1)
        return j >= 2 and tokens[j - 1].is_word("on") and tokens[j - 2].is_word("distinct")
    return False


def _matching_open(tokens: list[Token], close_index: int) -> int:
    depth = 0
    for j in range(close_index, -1, -1):
        if tokens[j].is_punct(")"):
            depth += 1
        elif tokens[j].is_punct("("):
            depth -= 1
            if depth == 0:
                return j
    return -1


# --------------------------------------------------------------------------------------- SHOW


def parse_show(analysis: Analysis) -> str:
    """Return the setting name asked for by a ``SHOW`` statement, or ``"all"``."""
    words = analysis.tokens[1:]
    if not words:
        raise Rejected("SHOW needs a setting name", rule="invalid-show")
    lowered = [t.value if t.is_name else t.text for t in words]
    special = {
        ("time", "zone"): "timezone",
        ("transaction", "isolation", "level"): "transaction_isolation",
        ("session", "authorization"): "session_authorization",
    }
    if tuple(lowered) in special:
        return special[tuple(lowered)]
    if len(words) == 1 and words[0].is_name:
        return words[0].value
    if len(words) == 3 and words[0].is_name and words[1].is_punct(".") and words[2].is_name:
        return f"{words[0].value}.{words[2].value}"
    raise Rejected("this SHOW form is not supported", rule="invalid-show")


# ------------------------------------------------------------------------------------ EXPLAIN

_EXPLAIN_BOOLEAN_OPTIONS = frozenset(
    {
        "analyze",
        "analyse",
        "verbose",
        "costs",
        "settings",
        "generic_plan",
        "buffers",
        "wal",
        "timing",
        "summary",
        "memory",
        "serialize",
    }
)
_TRUE_WORDS = frozenset({"true", "on", "1"})
_FALSE_WORDS = frozenset({"false", "off", "0"})


@dataclass(slots=True)
class ExplainRequest:
    options: dict[str, str | bool]
    statement: str  # the SQL being explained


def parse_explain(analysis: Analysis) -> ExplainRequest:
    """Split ``EXPLAIN [options] statement`` into its options and the inner statement."""
    tokens = analysis.tokens
    options: dict[str, str | bool] = {}
    i = 1
    if i < len(tokens) and tokens[i].is_punct("("):
        i += 1
        while i < len(tokens) and not tokens[i].is_punct(")"):
            if not tokens[i].is_name:
                raise Rejected("malformed EXPLAIN option list", rule="invalid-explain")
            name = tokens[i].value
            i += 1
            value: str | bool = True
            if i < len(tokens) and not tokens[i].is_punct(",", ")"):
                raw = tokens[i].value if tokens[i].is_name else tokens[i].text.strip("'").lower()
                if raw in _TRUE_WORDS:
                    value = True
                elif raw in _FALSE_WORDS:
                    value = False
                else:
                    value = raw
                i += 1
            options[name] = value
            if i < len(tokens) and tokens[i].is_punct(","):
                i += 1
        i += 1  # the closing parenthesis
    else:
        while i < len(tokens) and tokens[i].is_word("analyze", "analyse", "verbose"):
            options[tokens[i].value] = True
            i += 1
    if "analyse" in options:
        options["analyze"] = options.pop("analyse")
    if i >= len(tokens):
        raise Rejected("EXPLAIN needs a statement", rule="invalid-explain")
    unknown = set(options) - _EXPLAIN_BOOLEAN_OPTIONS - {"format"}
    if unknown:
        raise Rejected(
            f"unknown EXPLAIN option: {', '.join(sorted(unknown))}", rule="invalid-explain"
        )
    return ExplainRequest(options=options, statement=analysis.sql[tokens[i].pos :])
