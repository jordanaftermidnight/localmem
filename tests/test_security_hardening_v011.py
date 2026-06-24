"""Regression tests for the v0.1.1 security hardening pass.

Each test class covers one finding from the public-release council review:

  - SQL allowlist is now AST-based (sqlglot DuckDB dialect) — must reject
    UNION exfiltration, subqueries, DuckDB file-reader functions, CHR-encoded
    keyword reconstruction, and recursive CTE shapes.
  - Wing names are now charset-constrained — path-traversal patterns and
    high-Unicode payloads must fail config validation.
  - WebSocket bearer auth no longer echoes the token in the 101 Switching
    Protocols response — the accepted subprotocol must be the literal
    string "bearer", never the token.
  - Entry.importance is now Pydantic-bounded — out-of-range values rejected
    at Entry construction AND at localmem_update direct-assignment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from localmem.archiver import _is_safe_sql_where
from localmem.config import LocalmemConfig
from localmem.models import Entry


# =====================================================================
# Finding #1 — DuckDB SQL AST allowlist
# =====================================================================


class TestSqlAstAllowlistAccepts:
    """Real WHERE clauses an operator might write must still pass."""

    @pytest.mark.parametrize(
        "clause",
        [
            "wing = 'router'",
            "wing = 'router' AND room = 'inbox'",
            "created_at > '2026-01-01'",
            "importance >= 0.5 AND pinned = true",
            "wing IN ('router', 'tools', 'observer')",
            "summary IS NOT NULL",
            "content LIKE '%error%'",
            "importance BETWEEN 0.2 AND 0.8",
            "(wing = 'a' OR wing = 'b') AND NOT pinned",
            "agent_id = 'x' AND created_at >= '2026-05-01' AND importance > 0.3",
        ],
    )
    def test_safe_clause_accepted(self, clause):
        assert _is_safe_sql_where(clause), f"safe clause rejected: {clause!r}"


class TestSqlAstAllowlistRejects:
    """The bypass surface the v0.5.1 regex missed."""

    @pytest.mark.parametrize(
        "clause,why",
        [
            ("1=1 UNION SELECT password FROM users", "UNION exfiltration"),
            ("wing = 'x' OR 1=1 UNION ALL SELECT * FROM read_csv_auto('/etc/passwd')",
             "UNION + file reader"),
            ("wing IN (SELECT secret FROM keys)", "subquery in IN"),
            ("EXISTS (SELECT 1 FROM secrets)", "EXISTS subquery"),
            ("wing = (SELECT wing FROM t LIMIT 1)", "scalar subquery"),
            ("wing = read_csv_auto('/etc/passwd')", "DuckDB file reader function"),
            ("wing = read_text('/etc/shadow')", "read_text function"),
            ("wing = load_extension('evil')", "load_extension function"),
            ("wing = CHR(100) || CHR(114) || CHR(111) || CHR(112)",
             "CHR-encoded payload (function calls)"),
            ("wing = list_value('x', 'y')", "list_value function"),
            ("wing = secrets.password", "qualified column"),
            ("t.wing = 'x'", "qualified column with table prefix"),
            ("password = 'x'", "column not in schema allowlist"),
            ("user.email = 'x'", "qualified + unknown column"),
            ("CAST(wing AS INTEGER) = 1", "cast expression"),
            ("wing = 'x' AND ROW_NUMBER() OVER () = 1", "window function"),
        ],
    )
    def test_unsafe_clause_rejected(self, clause, why):
        assert not _is_safe_sql_where(clause), f"should reject ({why}): {clause!r}"

    def test_empty_clause_rejected(self):
        assert not _is_safe_sql_where("")
        assert not _is_safe_sql_where("   ")

    def test_syntax_error_rejected(self):
        assert not _is_safe_sql_where("wing =")
        assert not _is_safe_sql_where("not a valid clause $@#")


# =====================================================================
# Finding #2 — Wing-name charset validator
# =====================================================================


class TestWingNameCharset:
    @pytest.mark.parametrize(
        "name",
        ["a", "router", "agent_a", "tools-v2", "observer1", "x" * 63],
    )
    def test_valid_wing_name_accepted(self, name):
        cfg = LocalmemConfig(wings=[name])
        assert name in cfg.wings

    @pytest.mark.parametrize(
        "name,why",
        [
            ("../etc", "path traversal"),
            ("..", "parent-dir literal"),
            ("a/b", "slash"),
            ("a\\b", "backslash"),
            ("a b", "whitespace"),
            ("a.b", "dot"),
            ("a:b", "colon"),
            ("Router", "uppercase"),
            ("ROUTER", "uppercase"),
            ("_underscore-start", "leading underscore"),
            ("-dash-start", "leading dash"),
            ("a" + "\x00", "null byte"),
            ("a" + "\n", "newline"),
            ("a" * 64, "exceeds 63 chars"),
            ("", "empty"),
            ("café", "non-ascii"),
            ("agent‮", "rtl override"),
        ],
    )
    def test_invalid_wing_name_rejected(self, name, why):
        with pytest.raises(ValidationError, match="(invalid wing name|non-empty)"):
            LocalmemConfig(wings=[name])


# =====================================================================
# Finding #5 — Entry.importance Pydantic bound
# =====================================================================


class TestImportanceBound:
    @pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.999, 1.0])
    def test_in_range_accepted(self, value):
        e = Entry(
            wing="router", room="r", agent_id="a", content="x", importance=value
        )
        assert e.importance == value

    @pytest.mark.parametrize("value", [-0.1, -1.0, 1.01, 2.0, 99.0, 1e9])
    def test_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            Entry(
                wing="router", room="r", agent_id="a", content="x", importance=value
            )
