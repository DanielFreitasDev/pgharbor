"""Security tools on every PostgreSQL version of the matrix."""

import json
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.tools.security import (
    _classify,
    pg_find_sensitive_columns,
    pg_privileges,
    pg_roles,
    pg_security_audit,
)

from ..conftest import Server, call, rows_as_dicts, superuser

pytestmark = pytest.mark.integration

SCHEMA = "pgh_c_sec"
GROUP = "pgh_c_group"
INHERITING = "pgh_c_inh"
NOT_INHERITING = "pgh_c_noinh"
MD5_ROLE = "pgh_c_md5"
SCRAM_ROLE = "pgh_c_scram"
NO_PASSWORD = "pgh_c_nopw"
SERVER_NAME = "pgh_c_srv"
EVENT_TRIGGER = "pgh_c_evt"
# Values that must never come back from any tool.
ROLE_PASSWORD = "pgh-c-role-secret"
MAPPING_PASSWORD = "pgh-c-mapping-secret"
SENTINEL_DATA = "SENSITIVE-VALUE-pgh-c-123"
ROLES = (INHERITING, NOT_INHERITING, MD5_ROLE, SCRAM_ROLE, NO_PASSWORD, GROUP)


def _cleanup(conn: psycopg.Connection[Any], v: int) -> None:
    conn.execute(f"DROP EVENT TRIGGER IF EXISTS {EVENT_TRIGGER}")
    conn.execute(f"DROP SERVER IF EXISTS {SERVER_NAME} CASCADE")
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute("DROP FUNCTION IF EXISTS public.pgh_c_noop_evt()")
    for role in ROLES:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]).fetchone()
        if exists:
            conn.execute(f"DROP OWNED BY {role}")
            conn.execute(f"DROP ROLE {role}")


@pytest.fixture
def security_objects(ready: Server) -> Iterator[None]:
    """Roles and objects with known weaknesses, all named pgh_c_*, removed afterwards."""
    with superuser(ready) as conn:
        v = conn.info.server_version
        _cleanup(conn, v)
        try:
            conn.execute(f"CREATE ROLE {GROUP} NOLOGIN")
            if v >= 100000:
                conn.execute("SET password_encryption = 'md5'")
            conn.execute(
                f"CREATE ROLE {MD5_ROLE} LOGIN CREATEROLE CREATEDB REPLICATION "
                f"PASSWORD '{ROLE_PASSWORD}' CONNECTION LIMIT 5"
            )
            if v >= 90500:
                conn.execute(f"ALTER ROLE {MD5_ROLE} BYPASSRLS")
            if v >= 100000:
                conn.execute("SET password_encryption = 'scram-sha-256'")
                conn.execute(f"CREATE ROLE {SCRAM_ROLE} LOGIN PASSWORD '{ROLE_PASSWORD}'")
            conn.execute(f"CREATE ROLE {NO_PASSWORD} LOGIN")
            conn.execute(f"CREATE ROLE {INHERITING} LOGIN")
            conn.execute(f"GRANT {GROUP} TO {INHERITING} WITH ADMIN OPTION")
            if v >= 160000:
                conn.execute(f"CREATE ROLE {NOT_INHERITING} LOGIN")
                conn.execute(f"GRANT {GROUP} TO {NOT_INHERITING} WITH INHERIT FALSE, SET FALSE")
            else:
                conn.execute(f"CREATE ROLE {NOT_INHERITING} LOGIN NOINHERIT")
                conn.execute(f"GRANT {GROUP} TO {NOT_INHERITING}")
            conn.execute(f"ALTER ROLE {MD5_ROLE} SET statement_timeout = '5s'")
            if v >= 110000:
                conn.execute(f"GRANT pg_execute_server_program TO {NO_PASSWORD}")
                conn.execute(f"GRANT pg_read_server_files TO {MD5_ROLE}")

            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            conn.execute(f"GRANT CREATE ON SCHEMA {SCHEMA} TO PUBLIC")
            conn.execute(f"CREATE TABLE {SCHEMA}.open_t (id int PRIMARY KEY, label text)")
            conn.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.open_t TO PUBLIC")
            conn.execute(f"CREATE TABLE {SCHEMA}.read_t (id int PRIMARY KEY)")
            conn.execute(f"GRANT SELECT ON {SCHEMA}.read_t TO PUBLIC")
            conn.execute(f"CREATE TABLE {SCHEMA}.group_t (id int PRIMARY KEY, label text)")
            conn.execute(f"GRANT SELECT ON {SCHEMA}.group_t TO {GROUP}")
            conn.execute(f"GRANT UPDATE (label) ON {SCHEMA}.group_t TO {GROUP} WITH GRANT OPTION")
            conn.execute(f"CREATE TABLE {SCHEMA}.rls_no_policy (id int)")
            conn.execute(f"ALTER TABLE {SCHEMA}.rls_no_policy ENABLE ROW LEVEL SECURITY")
            conn.execute(f"CREATE TABLE {SCHEMA}.policy_no_rls (id int)")
            conn.execute(f"CREATE POLICY p ON {SCHEMA}.policy_no_rls USING (id > 0)")
            conn.execute(
                f"CREATE FUNCTION {SCHEMA}.definer_open() RETURNS int LANGUAGE sql "
                "SECURITY DEFINER AS $$ SELECT 1 $$"
            )
            conn.execute(
                f"CREATE FUNCTION {SCHEMA}.definer_safe() RETURNS int LANGUAGE sql "
                "SECURITY DEFINER SET search_path = pg_catalog AS $$ SELECT 1 $$"
            )
            conn.execute(
                "CREATE FUNCTION public.pgh_c_noop_evt() RETURNS event_trigger "
                "LANGUAGE plpgsql AS $$ BEGIN NULL; END $$"
            )
            conn.execute(
                f"CREATE EVENT TRIGGER {EVENT_TRIGGER} ON ddl_command_end "
                "EXECUTE PROCEDURE public.pgh_c_noop_evt()"
            )
            available = conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'postgres_fdw'"
            ).fetchone()
            if available:
                conn.execute(
                    f"CREATE SERVER {SERVER_NAME} FOREIGN DATA WRAPPER postgres_fdw "
                    "OPTIONS (host 'remote.invalid', dbname 'remote')"
                )
                conn.execute(
                    f"CREATE USER MAPPING FOR postgres SERVER {SERVER_NAME} "
                    f"OPTIONS (user 'remote_user', password '{MAPPING_PASSWORD}')"
                )
            yield
        finally:
            _cleanup(conn, v)


def _no_secrets(text: str) -> None:
    assert "SCRAM-SHA-256$" not in text
    assert not re.search(r"md5[0-9a-f]{32}", text)
    assert "password=" not in text
    assert ROLE_PASSWORD not in text
    assert MAPPING_PASSWORD not in text
    assert SENTINEL_DATA not in text


# ------------------------------------------------------------------------------ pg_roles


async def test_roles_attributes_memberships_and_password_methods(
    app: Any, ready: Server, security_objects: None
) -> None:
    result = await call(pg_roles, connection="ro", pattern="pgh_c_*")
    roles = {r["name"]: r for r in rows_as_dicts(result["roles"])}
    assert set(roles) == {GROUP, INHERITING, NOT_INHERITING, MD5_ROLE, NO_PASSWORD} | (
        {SCRAM_ROLE} if ready.at_least(10) else set()
    )

    md5 = roles[MD5_ROLE]
    assert {"login", "createrole", "createdb", "replication"} <= set(md5["attributes"])
    assert ("bypassrls" in md5["attributes"]) is True
    assert md5["connection_limit"] == 5
    assert md5["password"] == "md5"
    assert md5["settings"] == ["statement_timeout=5s"]
    assert roles[NO_PASSWORD]["password"] == "none"
    assert roles[GROUP]["attributes"] == []
    assert roles[GROUP]["password"] is None
    if ready.at_least(10):
        assert roles[SCRAM_ROLE]["password"] == "scram-sha-256"

    assert roles[INHERITING]["member_of"] == [f"{GROUP} [admin]"]
    assert INHERITING in " ".join(roles[GROUP]["members"])
    if ready.at_least(16):
        assert roles[NOT_INHERITING]["member_of"] == [f"{GROUP} [no inherit] [no set]"]
    else:
        assert roles[NOT_INHERITING]["member_of"] == [GROUP]
        assert "noinherit" in roles[NOT_INHERITING]["attributes"]
    if ready.at_least(11):
        assert "pg_read_server_files" in roles[MD5_ROLE]["member_of"][0] or any(
            "pg_read_server_files" in m for m in roles[MD5_ROLE]["member_of"]
        )


async def test_roles_hide_system_roles_and_filter(app: Any, ready: Server) -> None:
    default = rows_as_dicts((await call(pg_roles, connection="ro"))["roles"])
    assert not any(r["name"].startswith("pg_") for r in default)
    assert "postgres" in {r["name"] for r in default}
    postgres = next(r for r in default if r["name"] == "postgres")
    assert {"superuser", "login"} <= set(postgres["attributes"])

    everything = rows_as_dicts(
        (await call(pg_roles, connection="ro", include_system=True))["roles"]
    )
    assert any(r["name"].startswith("pg_") for r in everything)

    one = await call(pg_roles, connection="ro", pattern="harbor", limit=1)
    assert [r["name"] for r in rows_as_dicts(one["roles"])] == ["harbor_ro"]
    nothing = await call(pg_roles, connection="ro", pattern="zz_no_such_role")
    assert nothing["roles"]["rows"] == []
    assert "hint" in nothing

    limited = await call(pg_roles, connection="ro", include_system=True, limit=1)
    assert limited["has_more"] is True


async def test_roles_for_an_unprivileged_login_omit_password_methods(
    app: Any, ready: Server, security_objects: None
) -> None:
    result = await call(pg_roles, connection="limited", pattern="pgh_c_md5")
    role = rows_as_dicts(result["roles"])[0]
    assert role["name"] == MD5_ROLE
    assert "password" not in result["roles"]["columns"]
    assert any("superuser" in note for note in result["notes"])


# ------------------------------------------------------------------------- pg_privileges


async def test_privileges_of_a_table_show_columns_and_owner(app: Any, ready: Server) -> None:
    result = await call(pg_privileges, connection="ro", object="shop.customers")
    assert result["owner"] == "postgres"
    grantees = {r["grantee"]: r["privileges"] for r in rows_as_dicts(result["privileges"])}
    assert "postgres (owner)" in grantees
    assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= set(grantees["postgres (owner)"])
    # harbor_ro lost table-level SELECT and has it on some columns only.
    assert "harbor_ro" not in grantees
    columns = rows_as_dicts(result["column_privileges"])
    entry = next(c for c in columns if c["grantee"] == "harbor_ro")
    assert entry["privilege"] == "SELECT"
    assert "api_token" not in entry["columns"]
    assert {"id", "name", "email"} <= set(entry["columns"])

    orders = await call(pg_privileges, connection="ro", object="shop.orders")
    granted = {r["grantee"]: r["privileges"] for r in rows_as_dicts(orders["privileges"])}
    assert granted["harbor_ro"] == ["SELECT"]
    assert "column_privileges" not in orders


async def test_privileges_of_schema_function_sequence_and_database(app: Any, ready: Server) -> None:
    schema = await call(pg_privileges, connection="ro", object="shop", kind="schema")
    granted = {r["grantee"]: r["privileges"] for r in rows_as_dicts(schema["privileges"])}
    assert granted["harbor_ro"] == ["USAGE"]

    function = await call(
        pg_privileges, connection="ro", object="shop.order_total", kind="function"
    )
    assert function["object"] == "shop.order_total(bigint)"
    assert function["acl_is_default"] is True
    assert {r["grantee"] for r in rows_as_dicts(function["privileges"])} >= {"PUBLIC"}
    with_types = await call(
        pg_privileges, connection="ro", object="shop.fmt_price(numeric)", kind="function"
    )
    assert with_types["object"] == "shop.fmt_price(numeric)"

    sequence = await call(
        pg_privileges, connection="ro", object="shop.invoice_seq", kind="sequence"
    )
    assert sequence["owner"] == "postgres"

    database = await call(pg_privileges, connection="ro", object="pgharbor_test", kind="database")
    names = {r["grantee"] for r in rows_as_dicts(database["privileges"])}
    assert {"harbor_ro", "PUBLIC"} <= names


async def test_privileges_follow_memberships_and_public(
    app: Any, ready: Server, security_objects: None
) -> None:
    inheriting = await call(pg_privileges, connection="ro", role=INHERITING)
    assert inheriting["role"]["inherits_from"] == [GROUP]
    grants = rows_as_dicts(inheriting["grants"])
    via_group = [g for g in grants if g["via"] == f"role {GROUP}"]
    assert any(
        g["object"] == f"{SCHEMA}.group_t" and g["privileges"] == ["SELECT"] for g in via_group
    )
    column = next(g for g in via_group if g["kind"] == "column")
    assert column["object"] == f"{SCHEMA}.group_t (label)"
    assert column["privileges"] == ["UPDATE WITH GRANT OPTION"]
    # PUBLIC rights reach everyone, and are labelled as such.
    assert any(g["via"] == "PUBLIC" and g["object"] == f"{SCHEMA}.read_t" for g in grants)

    # A role that does not inherit does not get the group's rights.
    isolated = await call(pg_privileges, connection="ro", role=NOT_INHERITING)
    assert "inherits_from" not in isolated["role"]
    assert not any(g["via"] == f"role {GROUP}" for g in rows_as_dicts(isolated["grants"]))

    public = await call(pg_privileges, connection="ro", role="PUBLIC")
    assert public["role"] == {"name": "PUBLIC"}
    assert any(
        g["object"] == f"{SCHEMA}.open_t" and g["privileges"] == ["INSERT", "SELECT"]
        for g in rows_as_dicts(public["grants"])
    )


async def test_privileges_of_a_role_include_owned_objects_and_defaults(
    app: Any, ready: Server, security_objects: None
) -> None:
    with superuser(ready) as conn:
        conn.execute(f"ALTER SCHEMA {SCHEMA} OWNER TO {MD5_ROLE}")
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {MD5_ROLE} IN SCHEMA {SCHEMA} "
            f"GRANT SELECT ON TABLES TO {GROUP}"
        )
    owner = await call(pg_privileges, connection="ro", role=MD5_ROLE)
    owned = rows_as_dicts(owner["owns"])
    assert {"kind": "schema", "object": SCHEMA} in owned
    defaults = rows_as_dicts(owner["default_privileges"])
    assert defaults[0]["defined_by"] == MD5_ROLE
    assert defaults[0]["schema"] == SCHEMA
    assert defaults[0]["on_new"] == "tables"
    assert defaults[0]["grantee"] == GROUP
    assert defaults[0]["privileges"] == ["SELECT"]

    admin = await call(pg_privileges, connection="ro", role="postgres")
    assert admin["role"]["superuser"] is True
    assert "bypasses every privilege check" in admin["note"]


async def test_privileges_report_mistakes_helpfully(app: Any, ready: Server) -> None:
    with pytest.raises(ToolError) as neither:
        await call(pg_privileges, connection="ro")
    assert "exactly one" in str(neither.value)
    with pytest.raises(ToolError):
        await call(pg_privileges, connection="ro", object="shop.orders", role="harbor_ro")
    with pytest.raises(ToolError) as missing_role:
        await call(pg_privileges, connection="ro", role="harbor")
    assert "harbor_ro" in str(missing_role.value)
    with pytest.raises(ToolError) as missing_table:
        await call(pg_privileges, connection="ro", object="shop.order")
    assert "shop.orders" in str(missing_table.value)
    with pytest.raises(ToolError):
        await call(pg_privileges, connection="ro", object="nope", kind="schema")
    with pytest.raises(ToolError):
        await call(pg_privileges, connection="ro", object="shop.nope", kind="function")
    with pytest.raises(ToolError) as bad_signature:
        await call(pg_privileges, connection="ro", object="shop.order_total(", kind="function")
    assert "function" in str(bad_signature.value)


async def test_privileges_as_an_unprivileged_role(app: Any, ready: Server) -> None:
    result = await call(pg_privileges, connection="limited", role="harbor_ro")
    grants = rows_as_dicts(result["grants"])
    assert any(g["kind"] == "column" and "api_token" not in g["object"] for g in grants)
    assert any(g["object"] == "shop.orders" and g["via"] == "direct" for g in grants)


# ---------------------------------------------------------------------- pg_security_audit


def _by_check(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["check"]: f for f in result["findings"]}


async def test_audit_finds_the_planted_weaknesses(
    app: Any, ready: Server, security_objects: None
) -> None:
    result = await call(pg_security_audit, connection="ro")
    found = _by_check(result)
    severities = [f["severity"] for f in result["findings"]]
    assert severities == sorted(severities, key=["critical", "warning", "info"].index)
    assert result["summary"]["critical"] >= 2

    assert found["public_write"]["severity"] == "critical"
    assert f"{SCHEMA}.open_t (INSERT, SELECT)" in found["public_write"]["objects"]
    assert found["public_read"]["severity"] == "warning"
    assert f"{SCHEMA}.read_t (SELECT)" in found["public_read"]["objects"]
    assert SCHEMA in found["schema_create_public"]["objects"]
    # PostgreSQL 15 stopped granting CREATE on schema public to everyone.
    assert ("public" in found["schema_create_public"]["objects"]) == (not ready.at_least(15))

    assert found["security_definer_open"]["severity"] == "critical"
    assert f"{SCHEMA}.definer_open()" in found["security_definer_open"]["objects"]
    flagged = json.dumps(result["findings"])
    assert "definer_safe" not in flagged

    assert f"{SCHEMA}.policy_no_rls" in found["policy_without_rls"]["objects"]
    assert found["policy_without_rls"]["severity"] == "critical"
    assert f"{SCHEMA}.rls_no_policy" in found["rls_without_policy"]["objects"]

    assert MD5_ROLE in found["role_createrole"]["objects"]
    assert MD5_ROLE in found["role_replication"]["objects"]
    assert MD5_ROLE in found["role_createdb"]["objects"]
    if ready.at_least(10):
        assert MD5_ROLE in found["password_md5"]["objects"]
        assert SCRAM_ROLE not in found["password_md5"]["objects"]
    assert NO_PASSWORD in found["login_without_password"]["objects"]
    assert "postgres" in found["superusers"]["objects"][0]

    assert any("event trigger" in f["title"] for f in result["findings"])
    assert f"{EVENT_TRIGGER} on ddl_command_end" in found["event_triggers"]["objects"]
    assert "dblink" in found["risky_extensions"]["objects"]
    assert found["ssl_off"]["recommendation"]
    assert all(f["recommendation"] for f in result["findings"])

    if ready.at_least(11):
        assert found["member_pg_execute_server_program"]["severity"] == "critical"
        assert NO_PASSWORD in found["member_pg_execute_server_program"]["objects"]
        assert MD5_ROLE in found["member_pg_read_server_files"]["objects"]
    else:
        assert any("server_files" in note for note in result.get("notes", []))

    if ready.at_least(10):
        assert any(
            f["check"] == "hba_trust" and any("trust" in o for o in f["objects"])
            for f in result["findings"]
        )
    else:
        assert any("pg_hba" in note for note in result["notes"])


async def test_audit_counts_foreign_servers_without_reading_their_options(
    app: Any, ready: Server, security_objects: None
) -> None:
    with superuser(ready) as conn:
        has_fdw = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'postgres_fdw'"
        ).fetchone()
    text = await pg_security_audit(connection="ro")
    _no_secrets(text)
    if has_fdw:
        found = _by_check(json.loads(text))
        assert "foreign server" in found["foreign_servers"]["title"]
        assert "user mapping" in found["foreign_servers"]["title"]
        assert "remote.invalid" not in text


async def test_audit_as_an_unprivileged_role_degrades_to_notes(
    app: Any, ready: Server, security_objects: None
) -> None:
    result = await call(pg_security_audit, connection="limited")
    found = _by_check(result)
    # Checks that only need the catalogs still work.
    assert f"{SCHEMA}.open_t (INSERT, SELECT)" in found["public_write"]["objects"]
    assert f"{SCHEMA}.definer_open()" in found["security_definer_open"]["objects"]
    notes = " ".join(result["notes"])
    assert "superuser connection" in notes  # password checks
    if ready.at_least(10):
        assert "pg_hba_file_rules" in notes  # refused, reported, the rest still ran
        assert "permission denied" in notes
    assert "password_md5" not in found
    assert "hba_trust" not in found


# ------------------------------------------------------------------ pg_find_sensitive_columns


@pytest.fixture
def sensitive_table(ready: Server) -> Iterator[None]:
    with superuser(ready) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}_cols CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {SCHEMA}_cols")
            conn.execute(
                f"""
                CREATE TABLE {SCHEMA}_cols.people (
                    id int PRIMARY KEY,
                    password_hash text,
                    cpf varchar(14),
                    birth_date date,
                    client_ip text,
                    last_seen inet,
                    "userEmail" text,
                    telefone_celular text,
                    salario numeric,
                    endereco_rua text,
                    notes text,
                    credit_card_number text,
                    apikey text,
                    quantity int,
                    monkey text,
                    tokenizer text,
                    senha_antiga text
                )"""
            )
            conn.execute(
                f"COMMENT ON COLUMN {SCHEMA}_cols.people.notes IS "
                "'Free text with sensitive remarks about the person'"
            )
            conn.execute(
                f"INSERT INTO {SCHEMA}_cols.people (id, password_hash) "
                f"VALUES (1, '{SENTINEL_DATA}')"
            )
            conn.execute(f"ALTER TABLE {SCHEMA}_cols.people ENABLE ROW LEVEL SECURITY")
            conn.execute(f"GRANT SELECT (id, cpf) ON {SCHEMA}_cols.people TO harbor_ro")
            yield
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}_cols CASCADE")


async def test_sensitive_columns_in_the_sample_database(app: Any, ready: Server) -> None:
    result = await call(pg_find_sensitive_columns, connection="ro", schema="shop")
    found = {(r["table"], r["column"]): r for r in rows_as_dicts(result)}
    token = found[("shop.customers", "api_token")]
    assert token["category"] == "secret"
    assert "token" in token["why"]
    assert token["column_grants"] is True
    assert token["rls"] is False
    assert found[("shop.customers", "email")]["category"] == "email"
    assert found[("shop.customers", "email")]["type"] == "shop.email"
    assert found[("shop.customers", "address")]["category"] == "address"
    assert ("shop.orders", "total") not in found

    vault = rows_as_dicts(await call(pg_find_sensitive_columns, connection="ro", schema="vault"))
    assert [(r["table"], r["column"], r["category"]) for r in vault] == [
        ("vault.files", "access_key", "secret")
    ]
    assert vault[0]["column_grants"] is False

    everywhere = await call(pg_find_sensitive_columns, connection="ro")
    assert everywhere["total"] >= 4
    assert everywhere["by_category"]["secret"] >= 2
    assert "no table data was read" in everywhere["notes"][0]


async def test_sensitive_columns_rules_cover_names_comments_and_types(
    app: Any, ready: Server, sensitive_table: None
) -> None:
    result = await call(pg_find_sensitive_columns, connection="ro", schema=f"{SCHEMA}_cols")
    found = {r["column"]: r for r in rows_as_dicts(result)}
    expected = {
        "password_hash": "credential",
        "senha_antiga": "credential",
        "apikey": "secret",
        "cpf": "national id",
        "credit_card_number": "payment card",
        "salario": "financial",
        "birth_date": "birth date",
        "userEmail": "email",
        "telefone_celular": "phone",
        "endereco_rua": "address",
        "client_ip": "network",
        "last_seen": "network",
        "notes": "personal data (comment)",
    }
    assert {name: found[name]["category"] for name in expected} == expected
    assert "inet" in found["last_seen"]["why"]
    assert "comment mentions 'sensitive'" in found["notes"]["why"]
    # Names that only look similar are left alone.
    assert not {"id", "quantity", "monkey", "tokenizer"} & set(found)
    # Table-level protection is reported per row.
    assert all(r["rls"] is True for r in found.values())
    assert all(r["column_grants"] is True for r in found.values())
    # The first rows are the most dangerous categories.
    assert result["rows"][0][3] in ("credential", "secret")

    short = await call(pg_find_sensitive_columns, connection="ro", schema=f"{SCHEMA}_cols", limit=2)
    assert len(short["rows"]) == 2
    assert short["has_more"] is True
    assert short["total"] == result["total"]


def test_classifier_reads_camel_case_accents_and_glued_words() -> None:
    assert _classify("accessToken", None, "text", False)[0][0] == "secret"
    assert _classify("dataNascimento", None, "date", False)[0][0] == "birth date"
    assert _classify("userpassword", None, "text", False)[0][0] == "credential"
    assert _classify("x", "Endereço de entrega", "text", False)[0][0] == "address"
    assert _classify("x", None, "cpf_t", True)[0][0] == "national id"
    assert _classify("monkey", None, "text", False) == []
    assert _classify("description", "Short text", "text", False) == []


# ------------------------------------------------------------------------- no secrets leave


async def test_no_tool_returns_a_hash_or_a_secret(
    app: Any, ready: Server, security_objects: None, sensitive_table: None
) -> None:
    for profile in ("ro", "limited"):
        outputs = [
            await pg_roles(connection=profile, include_system=True),
            await pg_security_audit(connection=profile),
            await pg_find_sensitive_columns(connection=profile),
            await pg_privileges(connection=profile, role=MD5_ROLE),
            await pg_privileges(connection=profile, role="postgres"),
            await pg_privileges(connection=profile, object=f"{SCHEMA}_cols.people"),
        ]
        for text in outputs:
            _no_secrets(text)
    # The proof that the checks above can fail: the hash really is in the catalog.
    with superuser(ready) as conn:
        stored = conn.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", [MD5_ROLE]
        ).fetchone()
    assert stored is not None
    assert re.match(r"md5[0-9a-f]{32}$", stored[0])
