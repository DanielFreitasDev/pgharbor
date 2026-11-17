"""Function classification against the catalogs of every supported version.

``scripts/dev/catalogs.json.gz`` is a snapshot of the built-in functions of PostgreSQL
9.6 through 19. These tests make the policy explicit and force a review whenever a new
version adds a function that changes something.
"""

import gzip
import json
import re
from pathlib import Path

import pytest

from pgharbor.functions import (
    ALWAYS_DENIED,
    DENIED_WHEN_READ_ONLY,
    SAFE_VOLATILE,
    Verdict,
    classify_builtin,
    is_always_denied,
    is_safe_volatile,
)

SNAPSHOT = Path(__file__).parents[2] / "scripts" / "dev" / "catalogs.json.gz"
_SIGNATURE = re.compile(r"^(?P<name>[^(]+)\(.*\) -> .* \[(?P<volatility>[isv])\]$")

# VOLATILE built-ins that cannot be called from a query in any useful way: trigger
# functions, access-method handlers, encoding converters and procedural-language glue.
# They are refused like every other unclassified volatile function; they are listed here
# only so that a genuinely new function stands out.
INTERNAL = re.compile(
    r"^("
    r"RI_FKey_\w+|\w+handler|plpgsql_\w+|\w+_to_\w+|suppress_redundant_updates_trigger"
    r"|tsvector_update_trigger\w*|unique_key_recheck|dsnowball_\w+|amvalidate|bernoulli"
    r"|system|currtid2?|pg_isolation_test_session_is_blocked"
    r")$"
)


def builtins() -> dict[str, set[str]]:
    with gzip.open(SNAPSHOT, "rt") as handle:
        data = json.load(handle)
    out: dict[str, set[str]] = {}
    for version in data.values():
        for signature in version["functions"]:
            match = _SIGNATURE.match(signature)
            assert match, signature
            out.setdefault(match["name"], set()).add(match["volatility"])
    return out


BUILTINS = builtins()


def test_snapshot_covers_the_supported_range() -> None:
    assert len(BUILTINS) > 2_500
    assert {"pg_stat_get_io", "pg_current_xlog_location", "pg_current_wal_lsn"} <= set(BUILTINS)


def test_every_volatile_builtin_has_been_reviewed() -> None:
    unreviewed = sorted(
        name
        for name, volatilities in BUILTINS.items()
        if "v" in volatilities
        and not is_always_denied(name)
        and name not in DENIED_WHEN_READ_ONLY
        and not is_safe_volatile(name)
        and not INTERNAL.match(name)
    )
    assert unreviewed == [], (
        "New VOLATILE built-in function(s). Decide for each one whether it belongs in "
        "ALWAYS_DENIED, DENIED_WHEN_READ_ONLY or SAFE_VOLATILE (pgharbor/functions.py): "
        + ", ".join(unreviewed)
    )


def test_safe_list_only_names_real_functions() -> None:
    assert sorted(SAFE_VOLATILE - set(BUILTINS)) == []


@pytest.mark.parametrize(
    "name",
    [
        # stable or immutable in the catalog, and still not acceptable
        "table_to_xml", "schema_to_xml", "database_to_xml", "database_to_xmlschema",
        "txid_current", "pg_current_xact_id",
        # volatile and dangerous
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_waldir", "pg_stat_file",
        "lo_import", "lo_export", "lo_get", "lo_put", "lo_unlink", "loread", "lowrite",
        "query_to_xml", "cursor_to_xml", "ts_stat", "ts_rewrite", "set_config", "pg_sleep",
        "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
        "pg_switch_wal", "pg_switch_xlog", "pg_create_restore_point", "pg_start_backup",
        "pg_backup_start", "pg_promote", "pg_advisory_lock", "pg_try_advisory_lock",
        "pg_advisory_xact_lock", "nextval", "setval", "pg_notify", "pg_logical_emit_message",
        "pg_create_physical_replication_slot", "pg_drop_replication_slot",
        "pg_logical_slot_get_changes", "pg_replication_origin_create", "pg_stat_reset",
        "pg_stat_reset_shared", "pg_export_snapshot", "gin_clean_pending_list",
        "brin_summarize_new_values", "binary_upgrade_set_next_pg_type_oid",
        "pg_restore_relation_stats", "pg_clear_relation_stats", "pg_import_system_collations",
    ],
)  # fmt: skip
def test_dangerous_builtins_are_refused_when_read_only(name: str) -> None:
    assert name in BUILTINS, name
    for volatility in BUILTINS[name]:
        verdict = classify_builtin(name, volatility, read_only=True)
        assert verdict is not Verdict.ALLOW, name


@pytest.mark.parametrize(
    "name",
    [
        "lower", "now", "current_setting", "pg_get_viewdef", "pg_relation_size",
        "pg_total_relation_size", "random", "clock_timestamp", "timezone", "gen_random_uuid",
        "pg_is_in_recovery", "pg_current_wal_lsn", "pg_blocking_pids", "pg_stat_get_activity",
        "has_table_privilege", "generate_series", "jsonb_build_object", "array_agg",
        "pg_get_functiondef", "format_type", "pg_size_pretty", "to_tsvector", "currval",
    ],
)  # fmt: skip
def test_useful_builtins_are_allowed(name: str) -> None:
    assert name in BUILTINS, name
    for volatility in BUILTINS[name]:
        assert classify_builtin(name, volatility, read_only=True) is Verdict.ALLOW, name


def test_side_effect_functions_are_allowed_only_when_writing() -> None:
    for name in ("nextval", "setval", "pg_notify", "txid_current"):
        assert classify_builtin(name, "v", read_only=True) is Verdict.DENY_READ_ONLY
        assert classify_builtin(name, "v", read_only=False) is Verdict.ALLOW


def test_host_and_session_functions_are_refused_even_when_writing() -> None:
    for name in ("pg_read_file", "lo_export", "dblink_exec", "set_config", "pg_sleep",
                 "pg_terminate_backend", "query_to_xml"):  # fmt: skip
        assert classify_builtin(name, "v", read_only=False) is Verdict.DENY_ALWAYS


def test_denied_families_match_by_prefix() -> None:
    for name in ("lo_anything_new", "pg_ls_newdir", "dblink_new", "pg_stat_reset_new",
                 "pg_replication_origin_new", "binary_upgrade_new"):  # fmt: skip
        assert is_always_denied(name), name
    for name in ("log", "lower", "pg_lsn_in", "pg_stat_get_new"):
        assert not is_always_denied(name), name


def test_lists_do_not_overlap() -> None:
    assert not ALWAYS_DENIED & SAFE_VOLATILE
    assert not DENIED_WHEN_READ_ONLY & SAFE_VOLATILE
    assert not ALWAYS_DENIED & DENIED_WHEN_READ_ONLY
    assert not any(is_always_denied(name) for name in SAFE_VOLATILE)
