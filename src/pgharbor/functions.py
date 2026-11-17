"""Which functions a statement may call.

A read-only transaction does **not** stop a function from having side effects:
``pg_terminate_backend``, ``pg_read_file``, ``lo_export``, ``dblink_exec``, ``set_config``,
``pg_advisory_lock`` and many others run fine inside ``BEGIN READ ONLY``. So pgharbor
decides by function name, with these lists:

``ALWAYS_DENIED``
    Never callable through free SQL, in any access mode: host files, programs, server
    control, replication control, functions that execute SQL held in a string (they would
    hide what really runs), large objects, and anything that changes session state.

``DENIED_WHEN_READ_ONLY``
    Harmless in a writing session but a side effect in a read-only one
    (``nextval``, ``pg_notify``, ...).

``SAFE_VOLATILE``
    Built-in functions that PostgreSQL labels VOLATILE only because their result changes
    between calls (``random``, ``clock_timestamp``, ``pg_relation_size``, ...). Every other
    VOLATILE built-in is refused: the default is deny.

The lists were built from the catalogs of PostgreSQL 9.6 through 19 and are checked by
``tests/unit/test_functions.py`` and by the integration suite on every version.
"""

from __future__ import annotations

import re
from enum import StrEnum

__all__ = [
    "ALWAYS_DENIED",
    "DENIED_WHEN_READ_ONLY",
    "SAFE_VOLATILE",
    "Verdict",
    "classify_builtin",
    "is_always_denied",
    "is_denied_when_read_only",
    "is_safe_volatile",
]


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY_ALWAYS = "deny-always"
    DENY_READ_ONLY = "deny-read-only"
    DENY_VOLATILE = "deny-volatile"


# --------------------------------------------------------------------------- always denied

ALWAYS_DENIED: frozenset[str] = frozenset(
    {
        # --- files and programs on the database host
        "pg_read_file",
        "pg_read_file_old",
        "pg_read_binary_file",
        "pg_stat_file",
        "pg_file_write",
        "pg_file_read",
        "pg_file_length",
        "pg_file_rename",
        "pg_file_unlink",
        "pg_file_sync",
        "pg_logdir_ls",
        "pg_logfile_rotate",
        # --- functions that run SQL held in a string (they hide what really executes)
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "table_to_xml",
        "table_to_xmlschema",
        "table_to_xml_and_xmlschema",
        "schema_to_xml",
        "schema_to_xmlschema",
        "schema_to_xml_and_xmlschema",
        "database_to_xml",
        "database_to_xmlschema",
        "database_to_xml_and_xmlschema",
        "ts_stat",
        "ts_rewrite",
        "crosstab",
        "crosstab2",
        "crosstab3",
        "crosstab4",
        "connectby",
        "xpath_table",
        # --- session state and waiting
        "set_config",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_shared",
        "pg_advisory_unlock_all",
        # --- other sessions and the server itself
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_rotate_logfile_old",
        "pg_promote",
        "pg_log_backend_memory_contexts",
        "pg_log_standby_snapshot",
        "pg_log_query_plan",
        # --- WAL, backup and recovery control
        "pg_switch_wal",
        "pg_switch_xlog",
        "pg_create_restore_point",
        "pg_start_backup",
        "pg_stop_backup",
        "pg_backup_start",
        "pg_backup_stop",
        "pg_wal_replay_pause",
        "pg_wal_replay_resume",
        "pg_xlog_replay_pause",
        "pg_xlog_replay_resume",
        "pg_export_snapshot",
        "pg_logical_emit_message",
        # --- replication slots and origins
        "pg_create_physical_replication_slot",
        "pg_create_logical_replication_slot",
        "pg_drop_replication_slot",
        "pg_copy_physical_replication_slot",
        "pg_copy_logical_replication_slot",
        "pg_replication_slot_advance",
        "pg_sync_replication_slots",
        "pg_logical_slot_get_changes",
        "pg_logical_slot_get_binary_changes",
        "pg_logical_slot_peek_changes",
        "pg_logical_slot_peek_binary_changes",
        # --- statistics and catalog maintenance
        "pg_stat_clear_snapshot",
        "pg_stat_force_next_flush",
        "pg_stat_statements_reset",
        "pg_stat_kcache_reset",
        "pg_qualstats_reset",
        "pg_wait_sampling_reset_profile",
        "pg_restore_relation_stats",
        "pg_restore_attribute_stats",
        "pg_restore_extended_stats",
        "pg_clear_relation_stats",
        "pg_clear_attribute_stats",
        "pg_clear_extended_stats",
        "pg_import_system_collations",
        "pg_extension_config_dump",
        "pg_nextoid",
        "pg_stop_making_pinned_objects",
        # --- index and storage maintenance that writes even in a read-only transaction
        "gin_clean_pending_list",
        "brin_summarize_new_values",
        "brin_summarize_range",
        "brin_desummarize_range",
        "pg_prewarm",
        "autoprewarm_start_worker",
        "autoprewarm_dump_now",
        "pg_buffercache_evict",
        "pg_buffercache_evict_relation",
        "pg_buffercache_evict_all",
        "pg_buffercache_mark_dirty",
        "pg_buffercache_mark_dirty_relation",
        "pg_buffercache_mark_dirty_all",
        "pg_truncate_visibility_map",
        "heap_force_kill",
        "heap_force_freeze",
        # --- large objects (reads included: they bypass table-level rules)
        "loread",
        "lowrite",
    }
)

# Whole families, matched by prefix.
_ALWAYS_DENIED_PREFIXES: tuple[str, ...] = (
    "lo_",  # large objects: lo_import, lo_export, lo_get, lo_put, ...
    "pg_ls_",  # directory listings on the host
    "dblink",  # runs SQL on another connection: writes escape the read-only transaction
    "pg_replication_origin_",
    "binary_upgrade_",
    "pg_stat_reset",
)


def is_always_denied(name: str) -> bool:
    return name in ALWAYS_DENIED or name.startswith(_ALWAYS_DENIED_PREFIXES)


# ----------------------------------------------------------------- denied only when read-only

DENIED_WHEN_READ_ONLY: frozenset[str] = frozenset(
    {
        "nextval",
        "setval",
        "setseed",
        "pg_notify",
        "txid_current",
        "pg_current_xact_id",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
    }
)


def is_denied_when_read_only(name: str) -> bool:
    return name in DENIED_WHEN_READ_ONLY


# ------------------------------------------------------------------------------ safe volatile

SAFE_VOLATILE: frozenset[str] = frozenset(
    {
        # randomness and clocks
        "random",
        "random_normal",
        "gen_random_uuid",
        "uuidv4",
        "uuidv7",
        "array_sample",
        "array_shuffle",
        "clock_timestamp",
        "timeofday",
        "timezone",
        "current_query",
        # sequences, read side only
        "currval",
        "lastval",
        "pg_sequence_last_value",
        "pg_get_sequence_data",
        # sizes
        "pg_relation_size",
        "pg_total_relation_size",
        "pg_table_size",
        "pg_indexes_size",
        "pg_database_size",
        "pg_tablespace_size",
        # transactions (read side)
        "txid_status",
        "pg_xact_status",
        "pg_xact_commit_timestamp",
        "pg_xact_commit_timestamp_origin",
        "pg_last_committed_xact",
        "pg_get_multixact_members",
        "pg_get_multixact_stats",
        # recovery and WAL positions
        "pg_is_in_recovery",
        "pg_is_in_backup",
        "pg_is_wal_replay_paused",
        "pg_is_xlog_replay_paused",
        "pg_get_wal_replay_pause_state",
        "pg_last_wal_receive_lsn",
        "pg_last_wal_replay_lsn",
        "pg_last_xact_replay_timestamp",
        "pg_last_xlog_receive_location",
        "pg_last_xlog_replay_location",
        "pg_current_wal_lsn",
        "pg_current_wal_insert_lsn",
        "pg_current_wal_flush_lsn",
        "pg_current_xlog_location",
        "pg_current_xlog_insert_location",
        "pg_current_xlog_flush_location",
        "pg_current_logfile",
        "pg_get_wal_resource_managers",
        "pg_get_wal_summarizer_state",
        "pg_available_wal_summaries",
        "pg_wal_summary_contents",
        # locks and activity
        "pg_blocking_pids",
        "pg_safe_snapshot_blocking_pids",
        "pg_lock_status",
        "pg_prepared_xact",
        "pg_notification_queue_usage",
        # server facts
        "pg_collation_actual_version",
        "pg_database_collation_actual_version",
        "pg_control_checkpoint",
        "pg_control_init",
        "pg_control_recovery",
        "pg_control_system",
        "pg_get_aios",
        "pg_get_backend_memory_contexts",
        "pg_get_dsm_registry_allocations",
        "pg_get_loaded_modules",
        "pg_get_shmem_allocations",
        "pg_get_shmem_allocations_numa",
        "pg_get_wait_events",
        "pg_hba_file_rules",
        "pg_ident_file_mappings",
        "pg_show_all_file_settings",
        "pg_show_replication_origin_status",
        "pg_jit_available",
        "pg_stat_have_stats",
        # partitions
        "pg_partition_ancestors",
        "pg_partition_tree",
    }
)

# Volatile functions shipped by common extensions that only read or generate values.
_SAFE_VOLATILE_EXTENSION: frozenset[str] = frozenset(
    {
        "uuid_generate_v1",
        "uuid_generate_v1mc",
        "uuid_generate_v4",
        "gen_random_bytes",
        "gen_salt",
        "pg_stat_statements",
        "pg_stat_statements_info",
        "pgstattuple",
        "pgstattuple_approx",
        "pgstatindex",
        "pgstatginindex",
        "pgstathashindex",
        "pg_relpages",
        "pg_buffercache_pages",
        "pg_buffercache_summary",
        "pg_buffercache_usage_counts",
        "pg_buffercache_numa_pages",
        "pg_freespace",
        "pg_visibility",
        "pg_visibility_map",
        "pg_visibility_map_summary",
        "pg_check_frozen",
        "pg_check_visible",
        "show_trgm",
        "show_limit",
        "normal_rand",
    }
)

_SAFE_VOLATILE_PREFIXES: tuple[str, ...] = ("pg_stat_get_",)


def is_safe_volatile(name: str, *, builtin: bool = True) -> bool:
    if name in SAFE_VOLATILE or name.startswith(_SAFE_VOLATILE_PREFIXES):
        return True
    return not builtin and name in _SAFE_VOLATILE_EXTENSION


def classify_builtin(name: str, volatility: str, *, read_only: bool) -> Verdict:
    """Decide about one ``pg_catalog`` function.

    ``volatility`` is ``pg_proc.provolatile``: ``i`` immutable, ``s`` stable, ``v`` volatile.
    """
    if is_always_denied(name):
        return Verdict.DENY_ALWAYS
    if is_denied_when_read_only(name):
        return Verdict.DENY_READ_ONLY if read_only else Verdict.ALLOW
    if volatility == "v" and read_only and not is_safe_volatile(name):
        return Verdict.DENY_VOLATILE
    return Verdict.ALLOW


# A cheap second net: these names must not appear as calls in a query plan either.
# (The lexical check already refused them; this catches a scanner disagreement.)
PLAN_DENY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_$])("
    + "|".join(sorted((re.escape(n) for n in ALWAYS_DENIED), key=len, reverse=True))
    + r"|lo_[a-z0-9_]+|pg_ls_[a-z0-9_]+|dblink[a-z0-9_]*|pg_stat_reset[a-z0-9_]*"
    r"|pg_replication_origin_[a-z0-9_]+|binary_upgrade_[a-z0-9_]+"
    r')"?\s*\('
)
