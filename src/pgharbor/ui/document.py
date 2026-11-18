"""The configuration file as a document the panel can read and change.

Three rules shape this module:

- **The validator of the server is the only judge.** A candidate text is written to a
  temporary file beside the real one and loaded with ``load_config``, the same function
  the MCP server uses. Only a file that passed is moved into place, so the bytes that
  were judged are the bytes that are kept.
- **Secrets go in, never out.** A password or a connection string can be set from the
  panel; nothing here ever returns one. They are left out of the structured view and
  masked in every text shown.
- **The owner's file stays the owner's file.** Edits go through ``tomlkit``, which keeps
  comments, order and layout; every save leaves a backup; a save is refused when the
  file changed on disk since the panel read it.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import re
import secrets
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import String, StringType, Trivia

from ..config import (
    _LIBPQ_PARAMS,
    AuditConfig,
    Config,
    ConnectionConfig,
    CustomTool,
    CustomToolParam,
    ProtectedTable,
    ToolsConfig,
    default_config_path,
    load_config,
)
from ..errors import ConfigError, PgHarborError
from ..render import scrub

__all__ = [
    "MASK",
    "Checked",
    "DocumentError",
    "Located",
    "apply_ops",
    "backup_text",
    "check_file",
    "check_text",
    "create_starter",
    "digest_of",
    "list_backups",
    "locate",
    "mask_text",
    "read_document",
    "save_text",
    "unified_diff",
]

MASK = "********"
MAX_BACKUPS = 30
MAX_TEXT_BYTES = 1_000_000

# Keys whose value is, or may contain, a credential.
SECRET_KEYS = frozenset({"password", "dsn"})
SECRET_PARAMS = frozenset({"sslpassword"})

_TOP_TABLES = frozenset({"defaults", "connections", "audit", "tools", "custom_tools"})
_NAMED = frozenset({"connections", "custom_tools"})  # tables whose children have names
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BACKUP_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.toml$")
_HEADER_RE = re.compile(r"""^\s*\[\[?[\w.\-"' ]+\]\]?\s*$""")
_BARE_KEY_RE = re.compile(r"""^\s*[\w.\-"']+\s*$""")
# A comment that may carry a credential: it names one, holds an address with a user
# and a password, or looks like a line of a password file (host:port:db:user:password).
_RISKY_COMMENT_RE = re.compile(
    r"(?i)pass|pwd|senha|secret|segredo|token|dsn|cred|api_?key|private_?key|access_?key"
    r"|\w+://[^\s/@]*:[^\s/@]*@"
    r"|(?:[^\s:#]+:){4}\S"
)

# The keys the panel knows, by place in the file. The value of a key it does not know is
# never shown: a mistyped `pasword` holds a password all the same.
_CONNECTION_KEYS = frozenset(ConnectionConfig.model_fields) - {"name"}
_PROTECT_KEYS = frozenset(ProtectedTable.model_fields)
_AUDIT_KEYS = frozenset(AuditConfig.model_fields)
_TOOLS_KEYS = frozenset(ToolsConfig.model_fields)
_CUSTOM_KEYS = frozenset(CustomTool.model_fields) - {"name"}
_PARAM_KEYS = frozenset(CustomToolParam.model_fields)


class DocumentError(PgHarborError):
    """The panel cannot do what was asked with the file. ``code`` is stable; the message
    is for a person."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Located:
    """Which file the servers read: the same search order as ``load_config``."""

    path: Path
    exists: bool
    explicit: bool  # named by --config or PGHARBOR_CONFIG


@dataclass(frozen=True, slots=True)
class Checked:
    config: Config | None
    errors: tuple[dict[str, str], ...] = ()

    @property
    def valid(self) -> bool:
        return self.config is not None


@dataclass(frozen=True, slots=True)
class Document:
    located: Located
    text: str = ""
    digest: str | None = None
    raw: dict[str, Any] | None = None  # None: not valid TOML
    secrets: tuple[str, ...] = ()
    checked: Checked = field(default_factory=lambda: Checked(None))
    mode: str | None = None
    modified: float | None = None


def locate() -> Located:
    explicit = os.environ.get("PGHARBOR_CONFIG")
    path = Path(explicit).expanduser() if explicit else default_config_path()
    return Located(path=path, exists=path.is_file(), explicit=bool(explicit))


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------------- reading


def read_document(located: Located | None = None) -> Document:
    """The file as it is now: text, structure without secrets, and the validator's verdict."""
    located = located or locate()
    if not located.exists:
        return Document(located=located, checked=_check_absent(located))
    try:
        data = located.path.read_bytes()
        info = located.path.stat()
    except OSError as exc:
        raise DocumentError("io", f"cannot read {located.path}: {exc.strerror}") from exc
    if len(data) > MAX_TEXT_BYTES:
        raise DocumentError("too-large", f"{located.path} is too large for the panel")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError("io", f"{located.path} is not valid UTF-8 ({exc.reason})") from exc
    raw: dict[str, Any] | None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raw = None
    found: list[str] = []
    if raw is not None:
        raw = _without_secrets(raw, found)
    mode = None if sys.platform == "win32" else f"{info.st_mode & 0o777:o}"
    return Document(
        located=located,
        text=text,
        digest=digest_of(data),
        raw=raw,
        secrets=tuple(found),
        checked=check_file(located.path),
        mode=mode,
        modified=info.st_mtime,
    )


def _check_absent(located: Located) -> Checked:
    if located.explicit:
        message = f"configuration file not found: {located.path}"
        return Checked(None, ({"loc": "", "message": message},))
    try:
        return Checked(load_config())  # the connection built from the environment, if any
    except ConfigError as exc:
        return Checked(None, _errors_of(exc, hide=""))


def _without_secrets(raw: dict[str, Any], found: list[str]) -> dict[str, Any]:
    """A copy of the parsed file with every credential removed; ``found`` gets their paths."""
    clean: dict[str, Any] = _plain(raw)
    _hide(clean, found, drop=True)
    return clean


def _is_table(value: Any) -> bool:
    return hasattr(value, "keys")


def _hide(doc: Any, secrets_found: list[str], *, drop: bool) -> None:
    """Take out of ``doc`` (parsed TOML, plain or ``tomlkit``) everything that must not
    be shown: credentials, wherever the file puts them, and the value of any key that is
    not a known setting. Removed when ``drop``, replaced by a mask otherwise."""

    def out(table: Any, key: str, path: str, *, secret: bool) -> None:
        if secret:
            secrets_found.append(path)
        if drop:
            del table[key]
        else:
            table[key] = MASK

    def tables(value: Any, known: frozenset[str], path: str) -> None:
        """An array of tables (protected tables, parameters of a custom tool)."""
        if not isinstance(value, list):
            return
        for index, item in enumerate(value):
            if _is_table(item):
                for key in [k for k in item.keys() if k not in known]:  # noqa: SIM118
                    out(item, key, f"{path}.{index}.{key}", secret=False)

    def connection(body: Any, path: str) -> None:
        for key in list(body.keys()):
            value, here = body[key], f"{path}.{key}"
            if key in SECRET_KEYS:
                out(body, key, here, secret=True)
            elif key not in _CONNECTION_KEYS:
                out(body, key, here, secret=False)
            elif key == "params":
                if not _is_table(value):
                    out(body, key, here, secret=False)
                    continue
                for name in list(value.keys()):
                    if name in SECRET_PARAMS:
                        out(value, name, f"{here}.{name}", secret=True)
                    elif name not in _LIBPQ_PARAMS:
                        out(value, name, f"{here}.{name}", secret=False)
            elif key == "protect":
                tables(value, _PROTECT_KEYS, here)

    def flat(body: Any, known: frozenset[str], path: str) -> None:
        for key in [k for k in body.keys() if k not in known]:  # noqa: SIM118
            out(body, key, f"{path}.{key}", secret=False)

    for top in list(doc.keys()):
        value = doc[top]
        if top == "default_connection":
            continue
        if top not in _TOP_TABLES or not _is_table(value):
            out(doc, top, top, secret=False)
        elif top == "defaults":
            connection(value, top)
        elif top == "connections":
            for name in list(value.keys()):
                if _is_table(value[name]):
                    connection(value[name], f"{top}.{name}")
                else:
                    out(value, name, f"{top}.{name}", secret=False)
        elif top == "audit":
            flat(value, _AUDIT_KEYS, top)
        elif top == "tools":
            flat(value, _TOOLS_KEYS, top)
        elif top == "custom_tools":
            for name in list(value.keys()):
                tool = value[name]
                if not _is_table(tool):
                    out(value, name, f"{top}.{name}", secret=False)
                    continue
                flat(tool, _CUSTOM_KEYS, f"{top}.{name}")
                if "params" in tool:
                    tables(tool["params"], _PARAM_KEYS, f"{top}.{name}.params")


def _plain(value: Any) -> Any:
    """JSON-ready copy: TOML dates and times become text."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------- validating


def check_file(path: Path) -> Checked:
    try:
        return Checked(load_config(path))
    except ConfigError as exc:
        return Checked(None, _errors_of(exc, hide=str(path)))


def check_text(text: str, directory: Path) -> Checked:
    """Judge a candidate text exactly as the server would judge the file."""
    temporary = _write_temporary(text, directory)
    try:
        return check_file(temporary)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _errors_of(exc: ConfigError, *, hide: str) -> tuple[dict[str, str], ...]:
    """Turn the validator's message into ``{loc, message}`` items, without the path of the
    temporary file it was given."""
    message = str(exc)
    if hide:
        message = message.replace(f"{hide}: ", "").replace(hide, "the file")
    head, _, rest = message.partition("\n")
    if not rest:
        return ({"loc": "", "message": head},)
    errors = []
    for line in rest.splitlines():
        loc, separator, detail = line.strip().partition(": ")
        if separator and re.fullmatch(r"[\w.\-]+", loc):
            errors.append({"loc": loc, "message": detail})
        elif line.strip():
            errors.append({"loc": "", "message": line.strip()})
    return tuple(errors)


# ------------------------------------------------------------------------------- editing


def apply_ops(text: str, ops: list[dict[str, Any]]) -> str:
    """Apply the panel's changes to the text of the file and return the new text.

    An operation is ``{"op": "set", "path": [...], "value": ...}``, ``{"op": "unset",
    "path": [...]}`` or ``{"op": "rename", "path": ["connections", "old"], "to": "new"}``.
    Paths start at a known top-level key and are at most three keys deep (four for one
    libpq parameter of a connection); whether the result makes sense is for the validator
    to say, not for this function.
    """
    if not isinstance(ops, list) or len(ops) > 500:
        raise DocumentError("invalid-op", "the list of changes is not valid")
    try:
        doc = tomlkit.parse(text)
    except TOMLKitError as exc:
        raise DocumentError(
            "toml", f"the file is not valid TOML and cannot be edited here: {exc}"
        ) from exc
    for op in ops:
        if not isinstance(op, dict):
            raise DocumentError("invalid-op", "a change is not an object")
        path = _checked_path(op.get("path"))
        kind = op.get("op")
        if kind == "set":
            _set(doc, path, op.get("value"))
        elif kind == "unset":
            _unset(doc, path)
        elif kind == "rename":
            doc = _rename(doc, path, op.get("to"))
        else:
            raise DocumentError("invalid-op", f"unknown change {kind!r}")
    return _tidy(tomlkit.dumps(doc), doc)


def _checked_path(path: Any) -> list[str]:
    if (
        not isinstance(path, list)
        or not 1 <= len(path) <= 4
        or not all(isinstance(part, str) and _KEY_RE.match(part) for part in path)
    ):
        raise DocumentError("invalid-op", f"invalid path {path!r}")
    head = path[0]
    if head == "default_connection":
        if len(path) != 1:
            raise DocumentError("invalid-op", "default_connection is a single value")
    elif head not in _TOP_TABLES:
        raise DocumentError("invalid-op", f"unknown section {head!r}")
    elif len(path) == 1:
        raise DocumentError("invalid-op", f"the section {head!r} cannot be replaced whole")
    elif len(path) == 3 and head not in _NAMED:
        raise DocumentError("invalid-op", f"path too deep for {head!r}")
    elif len(path) == 4 and (head != "connections" or path[2] != "params"):
        # One libpq parameter at a time, so that changing `sslmode` never has to
        # rewrite (and lose) an `sslpassword` the panel was never shown.
        raise DocumentError("invalid-op", f"path too deep: {'.'.join(path)}")
    return path


def _container(doc: Any, path: list[str], *, create: bool) -> Any:
    """The table that holds the last key of ``path``."""
    current = doc
    for depth, part in enumerate(path[:-1]):
        if part not in current:
            if not create:
                return None
            current[part] = tomlkit.table(is_super_table=depth == 0 and part in _NAMED)
        current = current[part]
        if not hasattr(current, "keys"):
            raise DocumentError("invalid-op", f"{'.'.join(path[: depth + 1])} is not a table")
    return current


def _set(doc: Any, path: list[str], value: Any) -> None:
    whole_table = len(path) == 2 and path[0] in _NAMED
    if whole_table and not isinstance(value, dict):
        raise DocumentError("invalid-op", f"{'.'.join(path)} must be a table")
    container = _container(doc, path, create=True)
    container[path[-1]] = _item(value, where=".".join(path))


def _unset(doc: Any, path: list[str]) -> None:
    container = _container(doc, path, create=False)
    if container is not None and path[-1] in container:
        del container[path[-1]]
        if len(path) == 4 and not container:
            _unset(doc, path[:-1])  # the last parameter went: no empty table is left


def _rename(doc: Any, path: list[str], new: Any) -> Any:
    """Rename a connection or a custom tool and return the document to go on with."""
    if len(path) != 2 or path[0] not in _NAMED:
        raise DocumentError("invalid-op", "only a connection or a custom tool can be renamed")
    if not isinstance(new, str) or not _KEY_RE.match(new):
        raise DocumentError("invalid-op", f"invalid new name {new!r}")
    section, old = path
    table = doc.get(section)
    if table is None or old not in table:
        raise DocumentError("not-found", f"{section}.{old} does not exist")
    if new == old:
        return doc
    if new in table:
        raise DocumentError("invalid-op", f"{section}.{new} already exists")
    doc = _renamed_in_place(doc, section, old, new)
    if section == "connections":
        # A connection is referred to by name in two places; follow the rename there.
        if doc.get("default_connection") == old:
            doc["default_connection"] = new
        for tool in (doc.get("custom_tools") or {}).values():
            names = tool.get("connections") if hasattr(tool, "get") else None
            if isinstance(names, list) and old in names:
                tool["connections"] = _item([new if n == old else n for n in names], where="")
    return doc


def _renamed_in_place(doc: Any, section: str, old: str, new: str) -> Any:
    """The table under its new name, where the owner wrote it and with its comments.

    The headers are rewritten in the text, which keeps everything around them, and the
    result is accepted only if it parses to the same content under the new name. A file
    laid out some other way (inline tables, dotted keys) falls back to moving the table.
    """
    expected = doc.unwrap()
    expected[section] = {
        (new if key == old else key): value for key, value in expected[section].items()
    }
    name = re.escape(old)
    header = re.compile(
        rf"^(\s*\[\[?\s*{section}\s*\.\s*)(?:{name}|\"{name}\"|'{name}')(\s*[.\]])", re.MULTILINE
    )
    try:
        rewritten = tomlkit.parse(header.sub(rf"\g<1>{new}\g<2>", tomlkit.dumps(doc)))
        if rewritten.unwrap() == expected:
            return rewritten
    except TOMLKitError:
        pass
    table = doc[section]
    table[new] = table[old]
    del table[old]
    return doc


def _item(value: Any, *, where: str) -> Any:
    """A JSON value as a TOML item, laid out the way a person would write it."""
    if isinstance(value, (bool, int, float)):
        if isinstance(value, float) and value != value:  # NaN is not a setting
            raise DocumentError("invalid-op", f"{where}: not a number")
        return tomlkit.item(value)
    if isinstance(value, str):
        if len(value) > 200_000:
            raise DocumentError("invalid-op", f"{where}: text too long")
        return _multiline(value) if "\n" in value else tomlkit.item(value)
    if isinstance(value, list):
        if value and all(isinstance(entry, dict) for entry in value):
            tables = tomlkit.aot()
            for entry in value:
                tables.append(_table(entry, where=where))
            return tables
        if any(isinstance(entry, (dict, list)) for entry in value):
            raise DocumentError("invalid-op", f"{where}: a list cannot mix tables and values")
        array = tomlkit.array()
        for entry in value:
            array.append(_item(entry, where=where))
        if len(value) > 4:
            array.multiline(True)
        return array
    if isinstance(value, dict):
        return _table(value, where=where)
    raise DocumentError("invalid-op", f"{where}: a value cannot be empty; remove the key instead")


def _table(value: dict[str, Any], *, where: str) -> Any:
    table = tomlkit.table()
    for key, entry in value.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise DocumentError("invalid-op", f"{where}: invalid key {key!r}")
        table[key] = _item(entry, where=f"{where}.{key}")
    return table


def _multiline(value: str) -> String:
    """Text with line breaks (a custom tool's SQL) as a triple-quoted string that starts
    on its own line."""
    escaped = String.from_raw(value, StringType.MLB).as_string()[3:-3]
    return String(StringType.MLB, value, "\n" + escaped, Trivia())


def _tidy(text: str, doc: Any) -> str:
    """Put a blank line before a table header that lost it (tomlkit glues a new table to
    whatever came before). Kept only if the result still means the same."""
    lines = text.split("\n")
    out: list[str] = []
    in_string = False
    for line in lines:
        starts_table = not in_string and line.startswith("[")
        if starts_table and out and out[-1].strip() and not out[-1].lstrip().startswith("#"):
            out.append("")
        out.append(line)
        if (line.count('"""') + line.count("'''")) % 2:
            in_string = not in_string
    tidy = "\n".join(out)
    if not tidy.endswith("\n"):
        tidy += "\n"
    try:
        same = tomlkit.parse(tidy).unwrap() == doc.unwrap()
    except TOMLKitError:
        same = False
    return tidy if same else text


# ------------------------------------------------------------------------------- showing


def mask_text(text: str) -> str:
    """The text of a configuration with every credential replaced, safe to display.

    A file that parses is masked by its structure. A file that does not parse cannot be:
    a value may span lines or hide in an inline table, so only its skeleton is shown
    (table headers and key names), which is what one needs to find a syntax error.
    """
    try:
        doc = tomlkit.parse(text)
    except TOMLKitError:
        return _skeleton(text)
    _hide(doc, [], drop=False)
    return scrub(_mask_comments(tomlkit.dumps(doc)))


def _mask_comments(text: str) -> str:
    """Replace every comment that may carry a credential (an old line commented out, a
    note with a password in it), whether it stands alone or ends a line. Comments are
    found by reading the line as TOML does, so a ``#`` inside a value is not one."""
    out: list[str] = []
    block: str | None = None  # the delimiter of the multi-line string we are inside
    for line in text.split("\n"):
        index, quote, comment = 0, "", -1
        while index < len(line):
            if block:
                closing = line.find(block, index)
                if closing < 0:
                    break
                index, block = closing + 3, None
            elif quote:
                if line[index] == "\\" and quote == '"':
                    index += 1
                elif line[index] == quote:
                    quote = ""
                index += 1
            elif line.startswith(('"""', "'''"), index):
                block, index = line[index : index + 3], index + 3
            elif line[index] in "\"'":
                quote, index = line[index], index + 1
            elif line[index] == "#":
                comment = index
                break
            else:
                index += 1
        if comment >= 0 and _RISKY_COMMENT_RE.search(line[comment:]):
            line = f"{line[:comment]}# {MASK}"
        out.append(line)
    return "\n".join(out)


def _skeleton(text: str) -> str:
    out = []
    inside = False  # in what looks like a multi-line string: nothing of it is shown
    for line in text.split("\n"):
        key, equals, _ = line.partition("=")
        opens = (line.count('"""') + line.count("'''")) % 2 == 1
        if inside:
            out.append(MASK)
        elif not line.strip():
            out.append("")
        elif _HEADER_RE.match(line):
            out.append(line.rstrip())
        elif equals and _BARE_KEY_RE.match(key):
            out.append(f'{key.rstrip()} = "{MASK}"')
        else:
            out.append(MASK)
        if opens:
            inside = not inside
    return "\n".join(out)


def unified_diff(before: str, after: str) -> list[str]:
    """What a save would change, credentials masked on both sides."""
    return list(
        difflib.unified_diff(
            mask_text(before).splitlines(),
            mask_text(after).splitlines(),
            fromfile="current",
            tofile="proposed",
            lineterm="",
            n=2,
        )
    )


# -------------------------------------------------------------------------------- saving


def _target(located: Located) -> Path:
    """The file to rewrite: the real one when the configured path is a link to it."""
    return located.path.resolve() if located.exists else located.path


def _write_temporary(text: str, directory: Path) -> Path:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = directory / f".pgharbor-{secrets.token_hex(6)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise DocumentError("io", f"cannot write in {directory}: {exc.strerror}") from exc
    return temporary


def save_text(located: Located, text: str, base_digest: str | None) -> dict[str, Any]:
    """Validate ``text`` and make it the configuration file.

    ``base_digest`` is the digest of the file the change was prepared against (``None``
    when there was no file). A different file on disk means someone else changed it in
    the meantime, and nothing is written. Returns the name of the backup that was kept.
    """
    target = _target(located)
    current: bytes | None
    try:
        current = target.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise DocumentError("io", f"cannot read {target}: {exc.strerror}") from exc
    if (digest_of(current) if current is not None else None) != base_digest:
        raise DocumentError(
            "conflict",
            "the configuration file changed on disk since it was read; reload and try again",
        )
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise DocumentError("too-large", "the configuration would be too large")
    temporary = _write_temporary(text, target.parent)
    try:
        checked = check_file(temporary)
        if not checked.valid:
            raise DocumentError(
                "invalid",
                "the configuration would not be valid: "
                + "; ".join(
                    f"{e['loc']}: {e['message']}" if e["loc"] else e["message"]
                    for e in checked.errors
                ),
            )
        backup = _keep_backup(target, current) if current is not None else None
        try:
            os.replace(temporary, target)
        except OSError as exc:
            raise DocumentError("io", f"cannot replace {target}: {exc.strerror}") from exc
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return {"backup": backup, "digest": digest_of(text.encode("utf-8"))}


def create_starter(located: Located, text: str) -> dict[str, Any]:
    if located.exists:
        raise DocumentError("conflict", f"{located.path} already exists")
    return save_text(located, text, None)


# ------------------------------------------------------------------------------- backups


def _backup_dir(target: Path) -> Path:
    return target.parent / "backups"


def _keep_backup(target: Path, content: bytes) -> str:
    directory = _backup_dir(target)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{target.stem}-{stamp}-{digest_of(content)[:8]}.toml"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        # No backup, no save: the promise is that every change can be undone.
        raise DocumentError("io", f"cannot keep a backup in {directory}: {exc.strerror}") from exc
    for old in sorted(directory.glob(f"{target.stem}-*.toml"), reverse=True)[MAX_BACKUPS:]:
        with contextlib.suppress(OSError):
            old.unlink()
    return name


def list_backups(located: Located) -> list[dict[str, Any]]:
    target = _target(located)
    found: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for file in _backup_dir(target).glob(f"{target.stem}-*.toml"):
            with contextlib.suppress(OSError):
                info = file.stat()
                found.append({"name": file.name, "modified": info.st_mtime, "size": info.st_size})
    found.sort(key=lambda entry: entry["name"], reverse=True)
    return found


def backup_text(located: Located, name: str) -> str:
    """The content of one backup. ``name`` comes from the browser: it must be a plain
    file name of the expected shape, inside the backup directory."""
    target = _target(located)
    if not _BACKUP_RE.match(name) or not name.startswith(f"{target.stem}-"):
        raise DocumentError("not-found", "no such backup")
    file = _backup_dir(target) / name
    try:
        if file.resolve().parent != _backup_dir(target).resolve():
            raise DocumentError("not-found", "no such backup")
        data = file.read_bytes()
    except OSError as exc:
        raise DocumentError("not-found", "no such backup") from exc
    if len(data) > MAX_TEXT_BYTES:
        raise DocumentError("too-large", "the backup is too large")
    return data.decode("utf-8", errors="replace")


# ----------------------------------------------------------------- what the panel shows


def describe(document: Document) -> dict[str, Any]:
    """The document as the panel's API returns it. No credential is in here."""
    located = document.located
    config = document.checked.config
    if not located.exists:
        from_environment = config is not None and bool(config.connections)
        origin = "missing" if located.explicit else ("environment" if from_environment else "none")
    else:
        origin = "file"
    out: dict[str, Any] = {
        "file": {
            "path": str(located.path),
            "exists": located.exists,
            "explicit": located.explicit,
            "origin": origin,
            "digest": document.digest,
            "mode": document.mode,
            "modified": document.modified,
            "is_toml": document.raw is not None or not located.exists,
        },
        "valid": document.checked.valid,
        "errors": list(document.checked.errors),
        "warnings": [scrub(w) for w in config.warnings] if config else [],
        "raw": document.raw or {},
        "secrets": list(document.secrets),
        "connections": {},
        "default_connection": config.default_connection if config else None,
    }
    if config:
        out["connections"] = {
            name: _describe_connection(profile) for name, profile in config.connections.items()
        }
    return out


def _describe_connection(profile: ConnectionConfig) -> dict[str, Any]:
    """Values after defaults were applied, plus what the panel needs to explain them."""
    effective = profile.model_dump(mode="json", exclude={*SECRET_KEYS, "params", "name"})
    effective["params"] = {k: v for k, v in profile.params.items() if k not in SECRET_PARAMS}
    environment: dict[str, dict[str, Any]] = {}
    for key in ("user_env", "password_env", "dsn_env"):
        variable = getattr(profile, key)
        if variable:
            # Whether the variable exists here, never what it holds.
            environment[key] = {"name": variable, "set": bool(os.environ.get(variable))}
    password_file = None
    if profile.password_file:
        path = Path(profile.password_file).expanduser()
        password_file = {"path": profile.password_file, "readable": os.access(path, os.R_OK)}
    return {
        "effective": effective,
        "target": profile.target(),
        "is_production": profile.is_production,
        "needs_approval": profile.effective_approval.value == "session",
        "environment_variables": environment,
        "password_file": password_file,
    }
