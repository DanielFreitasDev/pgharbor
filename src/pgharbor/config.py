"""Configuration: connection profiles, limits and safety rules.

The configuration is the only place where a connection's access mode is decided. No tool
can raise it, and nothing read from the database or from the conversation can change it.

Sources, first match wins:

1. ``--config PATH`` on the command line
2. the ``PGHARBOR_CONFIG`` environment variable
3. ``~/.config/pgharbor/config.toml`` (``%APPDATA%\\pgharbor\\config.toml`` on Windows)
4. no file at all: ``PGHARBOR_DSN`` or ``DATABASE_URL`` (or the libpq ``PG*`` variables)
   define one connection named ``default``

A configuration file in the current directory is never loaded on its own: a repository
you just cloned must not be able to hand the agent new database rules.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import sys
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError

__all__ = [
    "DEFAULT_REDACT_COLUMNS",
    "Access",
    "Approval",
    "AuditConfig",
    "Config",
    "ConnectionConfig",
    "CustomTool",
    "CustomToolParam",
    "FunctionPolicy",
    "ProtectedTable",
    "WriteConfirmation",
    "default_config_path",
    "load_config",
    "parse_duration_ms",
]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_PRODUCTION_LABELS = frozenset({"production", "prod", "producao", "produção", "live"})

# libpq keywords accepted under ``[connections.X.params]``. ``options`` and
# ``application_name`` are set by pgharbor itself and cannot be overridden here.
_LIBPQ_PARAMS = frozenset(
    {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslpassword",
        "sslcrl",
        "sslcrldir",
        "sslsni",
        "sslnegotiation",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "sslcertmode",
        "channel_binding",
        "require_auth",
        "gssencmode",
        "krbsrvname",
        "gsslib",
        "gssdelegation",
        "service",
        "passfile",
        "connect_timeout",
        "target_session_attrs",
        "load_balance_hosts",
        "hostaddr",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "tcp_user_timeout",
        "client_encoding",
    }
)


# Result columns whose name matches one of these (case-insensitive regular expressions)
# come back as "[redacted]". It protects against accidents, not against a determined
# query: an expression over the column gets a new name. Use `protect` for real rules.
DEFAULT_REDACT_COLUMNS: tuple[str, ...] = (
    r"password",
    r"passwd",
    r"(^|_)pass(phrase)?($|_)",
    r"senha",
    r"secret",
    r"segredo",
    r"(^|_)token($|_)",
    r"api_?key",
    r"private_?key",
    r"access_?key",
    r"credential",
    r"credencial",
)


class Access(StrEnum):
    READ_ONLY = "read-only"
    READ_WRITE = "read-write"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"read-only": 0, "read-write": 1, "admin": 2}[self.value]


class FunctionPolicy(StrEnum):
    STRICT = "strict"
    STANDARD = "standard"
    PERMISSIVE = "permissive"


class Approval(StrEnum):
    NEVER = "never"
    SESSION = "session"


class WriteConfirmation(StrEnum):
    DIALOG = "dialog"
    CONVERSATION = "conversation"
    NONE = "none"


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|min|m|h)?\s*$", re.IGNORECASE)
_DURATION_FACTORS = {"ms": 1, "s": 1000, "sec": 1000, "min": 60_000, "m": 60_000, "h": 3_600_000}


def parse_duration_ms(value: Any) -> int:
    """Accept ``30000``, ``"30s"``, ``"500ms"``, ``"2min"``. A bare number is milliseconds."""
    if isinstance(value, bool):
        raise ValueError("a duration cannot be a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            factor = _DURATION_FACTORS[(match.group(2) or "ms").lower()]
            return int(float(match.group(1)) * factor)
    raise ValueError(f"invalid duration {value!r}; use for example 30000, '30s' or '2min'")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProtectedTable(_Model):
    """Extra rules for one table (or a pattern such as ``audit.*``)."""

    table: str = Field(description="schema.table; shell-style wildcards are accepted")
    deny_columns: tuple[str, ...] = ()
    require_index_filter: bool = Field(
        default=False,
        description="refuse any plan that reads this table without an index condition",
    )
    max_rows: int | None = Field(default=None, ge=1)
    note: str = ""

    @field_validator("table")
    @classmethod
    def _qualified(cls, value: str) -> str:
        if value.count(".") != 1 or not all(value.split(".")):
            raise ValueError("use the form schema.table")
        return value

    @field_validator("deny_columns", mode="before")
    @classmethod
    def _lower_columns(cls, value: Any) -> Any:
        return tuple(str(v) for v in value) if isinstance(value, (list, tuple)) else value

    def matches(self, schema: str, name: str) -> bool:
        return fnmatch.fnmatchcase(f"{schema}.{name}", self.table)


class ConnectionConfig(_Model):
    name: str
    description: str = ""
    environment: str = "development"

    # ---- where to connect
    dsn: str | None = None
    dsn_env: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    dbname: str | None = None
    user: str | None = None
    user_env: str | None = None
    password: str | None = None
    password_env: str | None = None
    password_file: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    role: str | None = Field(default=None, description="SET ROLE to this role in every transaction")

    @field_validator("params", mode="before")
    @classmethod
    def _params_as_text(cls, value: Any) -> Any:
        # libpq takes every parameter as text; TOML authors naturally write keepalives = 1.
        if isinstance(value, dict):
            return {
                k: (str(int(v)) if isinstance(v, bool) else str(v))
                if isinstance(v, (bool, int, float))
                else v
                for k, v in value.items()
            }
        return value

    # ---- what is allowed
    access: Access = Access.READ_ONLY
    approval: Approval | None = None
    approval_ttl: int = Field(default=0, ge=0, description="ms; 0 keeps it for the session")
    approval_fallback: Literal["deny", "conversation"] = "conversation"
    write_confirmation: WriteConfirmation = WriteConfirmation.DIALOG
    function_policy: FunctionPolicy = FunctionPolicy.STANDARD
    allow_functions: tuple[str, ...] = ()
    deny_functions: tuple[str, ...] = ()
    deny_relations: tuple[str, ...] = ()
    expose_secret_catalogs: bool = False
    protect: tuple[ProtectedTable, ...] = ()
    redact_columns: tuple[str, ...] = DEFAULT_REDACT_COLUMNS
    allow_explain_analyze: bool = False
    allow_export: bool = True
    export_dir: str | None = None
    import_dir: str | None = None

    # ---- limits
    statement_timeout: int = 30_000
    lock_timeout: int = 3_000
    idle_in_transaction_session_timeout: int = 10_000
    max_rows: int = Field(default=200, ge=1)
    max_rows_limit: int = Field(default=5_000, ge=1)
    max_cell_chars: int = Field(default=2_000, ge=16)
    max_result_chars: int = Field(default=40_000, ge=1_000)
    max_sql_chars: int = Field(default=100_000, ge=100)
    max_plan_cost: float | None = Field(default=None, gt=0)

    # ---- connection handling
    pool_size: int = Field(default=2, ge=1, le=20)
    idle_close: int = Field(default=300_000, ge=0, description="ms; 0 closes after every call")
    connect_timeout: int = Field(default=10_000, ge=1_000)
    application_name: str = "pgharbor"

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError("use lowercase letters, digits, '-' and '_' (max 32 characters)")
        return value

    @field_validator(
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "idle_close",
        "connect_timeout",
        "approval_ttl",
        mode="before",
    )
    @classmethod
    def _duration(cls, value: Any) -> int:
        return parse_duration_ms(value)

    @field_validator("params")
    @classmethod
    def _known_params(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - _LIBPQ_PARAMS)
        if unknown:
            raise ValueError(
                f"unsupported connection parameter(s): {', '.join(unknown)}. "
                f"Accepted: {', '.join(sorted(_LIBPQ_PARAMS))}"
            )
        return {k: str(v) for k, v in value.items()}

    @field_validator("redact_columns")
    @classmethod
    def _valid_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        return value

    @field_validator("application_name")
    @classmethod
    def _plain_application_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,63}", value):
            raise ValueError("use letters, digits and _ . : - only")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ConnectionConfig:
        if self.dsn and self.dsn_env:
            raise ValueError("set either dsn or dsn_env, not both")
        if self.user and self.user_env:
            raise ValueError("set either user or user_env, not both")
        if sum(bool(x) for x in (self.password, self.password_env, self.password_file)) > 1:
            raise ValueError("set only one of password, password_env, password_file")
        if self.max_rows > self.max_rows_limit:
            raise ValueError("max_rows cannot be greater than max_rows_limit")
        if self.statement_timeout <= 0:
            raise ValueError("statement_timeout must be greater than zero")
        return self

    # ------------------------------------------------------------------ derived values

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in _PRODUCTION_LABELS

    @property
    def effective_approval(self) -> Approval:
        if self.approval is not None:
            return self.approval
        return Approval.SESSION if self.is_production else Approval.NEVER

    @property
    def read_only(self) -> bool:
        return self.access is Access.READ_ONLY

    def protection_for(self, schema: str, name: str) -> ProtectedTable | None:
        for rule in self.protect:
            if rule.matches(schema, name):
                return rule
        return None

    def target(self) -> str:
        """A description of the server without any secret, for messages and logs."""
        if self.dsn_env:
            return f"DSN from ${self.dsn_env}"
        if self.dsn:
            return "DSN from the configuration file"
        host = self.host or self.params.get("service") or "libpq defaults"
        port = f":{self.port}" if self.port else ""
        db = f"/{self.dbname}" if self.dbname else ""
        return f"{host}{port}{db}"

    def conninfo(self, environ: dict[str, str] | None = None) -> tuple[str, dict[str, Any]]:
        """Return ``(dsn, keyword arguments)`` for psycopg. Reads secrets at call time."""
        env = os.environ if environ is None else environ
        dsn = ""
        if self.dsn_env:
            dsn = env.get(self.dsn_env, "")
            if not dsn:
                raise ConfigError(
                    f"connection '{self.name}': environment variable {self.dsn_env} is not set"
                )
        elif self.dsn:
            dsn = self.dsn

        kwargs: dict[str, Any] = dict(self.params)
        for key in ("host", "port", "dbname"):
            value = getattr(self, key)
            if value is not None:
                kwargs[key] = value
        if self.user_env:
            user = env.get(self.user_env)
            if not user:
                raise ConfigError(
                    f"connection '{self.name}': environment variable {self.user_env} is not set"
                )
            kwargs["user"] = user
        elif self.user:
            kwargs["user"] = self.user
        if self.password_env:
            password = env.get(self.password_env)
            if password is None:
                raise ConfigError(
                    f"connection '{self.name}': environment variable {self.password_env} is not set"
                )
            kwargs["password"] = password
        elif self.password_file:
            path = Path(self.password_file).expanduser()
            try:
                kwargs["password"] = path.read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as exc:
                raise ConfigError(
                    f"connection '{self.name}': cannot read password_file ({exc.strerror})"
                ) from exc
        elif self.password is not None:
            kwargs["password"] = self.password
        kwargs.setdefault("connect_timeout", max(1, self.connect_timeout // 1000))
        kwargs["application_name"] = self.application_name
        return dsn, kwargs


class AuditConfig(_Model):
    enabled: bool = True
    path: str | None = None
    log_sql: bool = True
    log_params: bool = False

    def resolved_path(self) -> Path:
        if self.path:
            return Path(self.path).expanduser()
        return _state_dir() / "audit.jsonl"


class ToolsConfig(_Model):
    disabled: tuple[str, ...] = ()
    disabled_groups: tuple[str, ...] = ()


class CustomToolParam(_Model):
    name: str
    type: Literal["text", "integer", "number", "boolean"] = "text"
    description: str = ""
    required: bool = True
    default: str | int | float | bool | None = None

    @field_validator("name")
    @classmethod
    def _valid(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value):
            raise ValueError("use lowercase letters, digits and '_'")
        return value


class CustomTool(_Model):
    """A query approved by whoever owns the configuration, exposed as its own tool."""

    name: str
    description: str
    sql: str
    connections: tuple[str, ...] = ()
    params: tuple[CustomToolParam, ...] = ()
    max_rows: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _valid(cls, value: str) -> str:
        if not _TOOL_NAME_RE.match(value):
            raise ValueError("use lowercase letters, digits and '_' (2 to 48 characters)")
        return value


class Config(_Model):
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    custom_tools: dict[str, CustomTool] = Field(default_factory=dict)
    default_connection: str | None = None
    source: str = "environment"
    warnings: tuple[str, ...] = ()

    def connection(self, name: str | None) -> ConnectionConfig:
        if not self.connections:
            raise ConfigError(
                "No connection is configured. Create "
                f"{default_config_path()} (run `pgharbor init`) or set PGHARBOR_DSN."
            )
        if name is None:
            if self.default_connection:
                name = self.default_connection
            elif len(self.connections) == 1:
                name = next(iter(self.connections))
            else:
                raise ConfigError(
                    "Several connections are configured; pass `connection`. Available: "
                    + ", ".join(sorted(self.connections))
                )
        try:
            return self.connections[name]
        except KeyError:
            raise ConfigError(
                f"Unknown connection '{name}'. Available: "
                + (", ".join(sorted(self.connections)) or "none")
            ) from None


# ------------------------------------------------------------------------------------ loading


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "pgharbor"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pgharbor"


def _state_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "pgharbor"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pgharbor"


def default_config_path() -> Path:
    return _config_dir() / "config.toml"


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load the configuration. Raises :class:`ConfigError` with a readable message."""
    explicit = path or os.environ.get("PGHARBOR_CONFIG")
    if explicit:
        file = Path(explicit).expanduser()
        if not file.is_file():
            raise ConfigError(f"configuration file not found: {file}")
        return _from_file(file)
    file = default_config_path()
    if file.is_file():
        return _from_file(file)
    return _from_environment()


def _from_file(file: Path) -> Config:
    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{file}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: {exc.strerror}") from exc

    known = {"defaults", "connections", "audit", "tools", "custom_tools", "default_connection"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"{file}: unknown top-level key(s): {', '.join(unknown)}")

    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"{file}: [defaults] must be a table")
    forbidden = sorted(
        set(defaults)
        & {
            "name",
            "dsn",
            "dsn_env",
            "host",
            "port",
            "dbname",
            "user",
            "user_env",
            "password",
            "password_env",
            "password_file",
            "protect",
            "description",
        }
    )
    if forbidden:
        raise ConfigError(
            f"{file}: [defaults] cannot set {', '.join(forbidden)}; set it per connection"
        )

    connections_raw = raw.get("connections", {})
    if not isinstance(connections_raw, dict):
        raise ConfigError(f"{file}: [connections] must be a table of tables")

    data: dict[str, Any] = {
        "connections": {
            name: {**defaults, **(body if isinstance(body, dict) else {}), "name": name}
            for name, body in connections_raw.items()
        },
        "audit": raw.get("audit", {}),
        "tools": raw.get("tools", {}),
        "custom_tools": {
            name: {**(body if isinstance(body, dict) else {}), "name": name}
            for name, body in raw.get("custom_tools", {}).items()
        },
        "default_connection": raw.get("default_connection"),
        "source": str(file),
        "warnings": tuple(_file_warnings(file, connections_raw)),
    }
    return _validate(data, origin=str(file))


def _from_environment() -> Config:
    env = os.environ
    base: dict[str, Any] = {
        "name": "default",
        "access": env.get("PGHARBOR_ACCESS", "read-only"),
        "environment": env.get("PGHARBOR_ENVIRONMENT", "development"),
    }
    if env.get("PGHARBOR_DSN"):
        base["dsn_env"] = "PGHARBOR_DSN"
    elif env.get("DATABASE_URL"):
        base["dsn_env"] = "DATABASE_URL"
    elif not any(env.get(k) for k in ("PGHOST", "PGDATABASE", "PGSERVICE", "PGUSER")):
        return Config(source="environment (nothing configured)")
    return _validate(
        {"connections": {"default": base}, "source": "environment"}, origin="environment"
    )


def _validate(data: dict[str, Any], *, origin: str) -> Config:
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        lines = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"])
            lines.append(f"  {where}: {error['msg']}")
        raise ConfigError(f"{origin}: invalid configuration\n" + "\n".join(lines)) from exc

    if config.default_connection and config.default_connection not in config.connections:
        raise ConfigError(
            f"{origin}: default_connection '{config.default_connection}' is not defined"
        )
    for tool in config.custom_tools.values():
        missing = sorted(set(tool.connections) - set(config.connections))
        if missing:
            raise ConfigError(
                f"{origin}: custom tool '{tool.name}' names unknown connection(s): "
                + ", ".join(missing)
            )
    return config


def _file_warnings(file: Path, connections_raw: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    has_secret = any(
        isinstance(body, dict) and (body.get("password") or body.get("dsn"))
        for body in connections_raw.values()
    )
    if has_secret and sys.platform != "win32":
        mode = stat.S_IMODE(file.stat().st_mode)
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.append(
                f"{file} holds a password or DSN and can be read by other users of this "
                f"machine (mode {mode:o}). Run: chmod 600 {file}"
            )
    return warnings
