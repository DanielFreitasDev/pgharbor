"""The repository tells the truth about itself: examples load, generated docs are current."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from pgharbor import __version__
from pgharbor.app import REGISTRY
from pgharbor.cli import EXAMPLE_CONFIG
from pgharbor.config import load_config
from pgharbor.server import INSTRUCTIONS, load_tool_modules

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    "example", sorted((ROOT / "examples").glob("*.toml")), ids=lambda p: p.name
)
def test_examples_are_valid_configurations(example: Path) -> None:
    config = load_config(example)
    assert config.connections


def test_starter_configuration_is_valid(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(EXAMPLE_CONFIG)
    assert list(load_config(file).connections) == ["dev"]


def test_tool_reference_is_up_to_date() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "dev" / "gen_docs.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_agent_instruction_files_are_identical() -> None:
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()


def test_learning_index_links_resolve() -> None:
    index = (ROOT / "LEARNING.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\((docs/learning/[^)]+)\)", index)
    assert links
    for link in links:
        assert (ROOT / link).is_file(), link
    listed = {Path(link).name for link in links}
    on_disk = {p.name for p in (ROOT / "docs" / "learning").glob("*.md")}
    assert listed == on_disk


def test_version_is_consistent() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_instructions_only_mention_real_tools() -> None:
    load_tool_modules()
    mentioned = set(re.findall(r"\bpg_[a-z_]+\b", INSTRUCTIONS))
    assert mentioned <= set(REGISTRY), sorted(mentioned - set(REGISTRY))


def test_documents_only_mention_real_tools() -> None:
    load_tool_modules()
    known = set(REGISTRY)
    not_tools = {
        # PostgreSQL's own names that look like tool names
        "pg_catalog", "pg_class", "pg_stat_activity", "pg_stat_statements", "pg_stat_io",
        "pg_stat_wal", "pg_stat_checkpointer", "pg_wait_events", "pg_hba", "pg_authid",
        "pg_shadow", "pg_user_mapping", "pg_subscription", "pg_largeobject", "pg_statistic",
        "pg_stats", "pg_sleep", "pg_read_file", "pg_ls_dir", "pg_terminate_backend",
        "pg_cancel_backend", "pg_advisory_lock", "pg_relation_size", "pg_is_in_recovery",
        "pg_blocking_pids", "pg_reload_conf", "pg_notify", "pg_trgm", "pg_cron", "pg_partman",
        "pg_buffercache", "pg_service", "pg_hba_file_rules", "pg_stat_reset", "pg_locks",
        "pg_roles", "pg_settings", "pg_read_all_stats", "pg_sequence", "pg_stat_lock",
        "pg_stat_progress_repack", "pg_stat_recovery", "pg_stat_replication_slots",
        "pg_stat_subscription_stats", "pg_current_wal_lsn", "pg_stat_view", "pg_get_viewdef",
        "pg_depend", "pg_rewrite", "pg_stat_kcache", "pg_qualstats", "pg_wait_sampling",
        "pg_basebackup", "pg_dump", "pg_repack", "pg_stat_user_functions", "pg_prewarm",
    }  # fmt: skip
    for name in ("README.md", "README.pt-BR.md", "docs/security.md", "docs/configuration.md",
                 "docs/clients.md", "docs/compatibility.md"):  # fmt: skip
        text = (ROOT / name).read_text(encoding="utf-8")
        text = text.split("<!-- tools:start -->")[0] + text.split("<!-- tools:end -->")[-1]
        mentioned = set(re.findall(r"`(pg_[a-z_]+)`", text))
        unknown = mentioned - known - not_tools
        assert not unknown, f"{name} mentions tools that do not exist: {sorted(unknown)}"


def test_documented_test_counts_match_the_suite() -> None:
    """Numbers quoted in the documents are counted, not remembered."""
    sys.path.insert(0, str(ROOT))
    try:
        from tests.integration import test_guard as guard
    finally:
        sys.path.remove(str(ROOT))
    protected = len(guard.GUARDED_OK) + len(guard.GUARDED_REFUSED)
    security = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    quoted = " ".join(security.split())
    assert f"{len(guard.ALLOWED)} queries that must work" in quoted
    assert f"{len(guard.REFUSED)} attacks that must be refused" in quoted
    assert f"{protected} protected-table cases" in quoted
    refusals = len(guard.REFUSED) + len(guard.GUARDED_REFUSED)
    for readme, phrase in (
        ("README.md", "more than 180 attacks"),
        ("README.pt-BR.md", "mais de 180 ataques"),
    ):
        assert phrase in (ROOT / readme).read_text(encoding="utf-8"), readme
    assert refusals > 180
