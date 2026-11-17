"""Reading a query plan (``EXPLAIN (VERBOSE, FORMAT JSON)``) for the pre-flight check.

The plan is the server's own account of what a statement will touch: views are already
expanded to their base tables and every scan names its relation and schema. pgharbor
reads it before running the statement to enforce relation rules, the "no sequential
scan" rule of protected tables and the optional cost ceiling.

A plan is not a complete permission system (relations pruned by the planner do not
appear, and functions are not plan nodes), which is why it is one layer among several.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PlanSummary", "Scan", "summarize_plan"]

_EXPRESSION_KEYS = (
    "Output",
    "Filter",
    "Index Cond",
    "Recheck Cond",
    "Join Filter",
    "Hash Cond",
    "Merge Cond",
    "TID Cond",
    "One-Time Filter",
    "Sort Key",
    "Group Key",
    "Function Call",
    "Table Function Call",
    "Order By",
    "Presorted Key",
    "Cache Key",
    "Run Condition",
)
_SEQUENTIAL_NODES = frozenset({"Seq Scan", "Sample Scan"})
_INDEX_NODES = frozenset({"Index Scan", "Index Only Scan"})


@dataclass(frozen=True, slots=True)
class Scan:
    node_type: str
    schema: str
    relation: str
    alias: str
    output: tuple[str, ...]
    index_condition: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.relation}"

    @property
    def sequential(self) -> bool:
        return self.node_type in _SEQUENTIAL_NODES

    @property
    def reads_everything(self) -> bool:
        """True when nothing narrows the scan through an index.

        A sequential scan reads the whole table. An index scan with no index condition
        walks the whole index, which is the same thing by another road. Bitmap heap scans
        and TID scans always start from a condition.
        """
        if self.sequential:
            return True
        return self.node_type in _INDEX_NODES and not self.index_condition


@dataclass(slots=True)
class PlanSummary:
    total_cost: float = 0.0
    plan_rows: float = 0.0
    scans: list[Scan] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)  # function scans: schema.name
    expressions: list[str] = field(default_factory=list)
    node_types: list[str] = field(default_factory=list)

    def relations(self) -> list[str]:
        seen: dict[str, None] = {}
        for scan in self.scans:
            seen.setdefault(scan.qualified, None)
        return list(seen)

    def uses_whole_row(self, scan: Scan) -> bool:
        """True when any expression of the plan uses the entire row of ``scan`` (``t.*``).

        That is how ``SELECT t``, ``row_to_json(t)``, ``t::text`` and ``count(t.*)`` appear
        in a plan, wrapped in whatever expression surrounds them.
        """
        pattern = re.compile(
            r'(?<![\w."])"?' + re.escape(scan.alias.replace('"', '""')) + r'"?\.\*'
        )
        return any(pattern.search(expression) for expression in self.expressions)


def summarize_plan(explain_output: Any) -> PlanSummary:
    """Accept the value returned by ``EXPLAIN (FORMAT JSON)`` (text or parsed)."""
    data = json.loads(explain_output) if isinstance(explain_output, (str, bytes)) else explain_output
    root = data[0]["Plan"] if isinstance(data, list) else data["Plan"]
    summary = PlanSummary(
        total_cost=float(root.get("Total Cost", 0.0)),
        plan_rows=float(root.get("Plan Rows", 0.0)),
    )
    _walk(root, summary)
    return summary


def _walk(node: dict[str, Any], summary: PlanSummary) -> None:
    node_type = node.get("Node Type", "")
    summary.node_types.append(node_type)
    relation = node.get("Relation Name")
    if relation:
        output = node.get("Output") or ()
        summary.scans.append(
            Scan(
                node_type=node_type,
                schema=node.get("Schema", ""),
                relation=relation,
                alias=node.get("Alias", relation),
                output=tuple(str(item) for item in output),
                index_condition=bool(node.get("Index Cond")),
            )
        )
    function = node.get("Function Name")
    if function:
        schema = node.get("Schema")
        summary.functions.append(f"{schema}.{function}" if schema else function)
    for key in _EXPRESSION_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            summary.expressions.append(value)
        elif isinstance(value, list):
            summary.expressions.extend(str(item) for item in value)
    for child in node.get("Plans") or ():
        _walk(child, summary)
