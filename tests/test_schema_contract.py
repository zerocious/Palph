"""
Drift test for [analysis/schema_v1.yaml](../analysis/schema_v1.yaml) —
PA-roadmap #5.

Loads the YAML schema contract and compares it against the actual SQLite
schema produced by `db.init_db()`. Catches three classes of drift:

1. **Missing column** in YAML — DB has a column the contract doesn't
   document. New columns must either be added to the YAML or land in
   v2 (breaking change).
2. **Extra column** in YAML — contract claims a column the DB doesn't
   provide. Indicates a stale YAML.
3. **Type mismatch** — column exists in both but SQLite affinity ≠
   contract `type`. Catches `INTEGER`→`REAL` migrations etc.

Tests skip if PyYAML isn't importable (CI without dev-deps shouldn't fail
the whole suite over this one test file).

Test coverage is **schema-level**: it doesn't inspect rendered CSV row
values. CSV-output type fidelity is a separate concern (and pandas does
its own type inference anyway).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio


_ROOT = Path(__file__).parent.parent
_YAML_PATH = _ROOT / "analysis" / "schema_v1.yaml"


yaml = pytest.importorskip("yaml", reason="PyYAML not installed")


# Mapping from YAML semantic-`type` to one of the SQLite type-affinity
# names. `integer` and `real` map directly; everything else lands on TEXT.
_YAML_TO_SQLITE_AFFINITY = {
    "integer": "INTEGER",
    "real":    "REAL",
    "text":    "TEXT",
}


def _load_contract() -> dict:
    with _YAML_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest_asyncio.fixture
async def db_columns(db):
    """
    Returns {table_name: {column_name: {"type": sqlite_affinity,
                                         "notnull": bool, "pk": bool}}}
    for every exported table.
    """
    from services import AnalyticsService
    table_names = list(AnalyticsService.EXPORTABLE_TABLES.values())
    result: dict[str, dict] = {}
    for tname in table_names:
        info: dict[str, dict] = {}
        async with db.execute(f"PRAGMA table_info({tname})") as cur:
            rows = await cur.fetchall()
        for r in rows:
            info[r[1]] = {
                "type": r[2],     # SQLite type affinity (uppercase)
                "notnull": bool(r[3]),
                "pk": bool(r[5]),
            }
        result[tname] = info
    return result


class TestFileExists:
    def test_yaml_file_present(self):
        assert _YAML_PATH.is_file(), f"missing {_YAML_PATH}"

    def test_yaml_parses(self):
        c = _load_contract()
        assert isinstance(c, dict)
        assert "schema_version" in c
        assert "tables" in c
        assert isinstance(c["tables"], dict)


class TestAllExportedTablesDocumented:
    """Every entry of AnalyticsService.EXPORTABLE_TABLES needs a yaml entry."""

    def test_every_exported_table_has_yaml_section(self):
        from services import AnalyticsService
        contract = _load_contract()
        documented = set(contract["tables"].keys())
        exported = set(AnalyticsService.EXPORTABLE_TABLES.values())
        missing = exported - documented
        assert not missing, (
            f"Tables exported but missing from schema_v1.yaml: {sorted(missing)}"
        )

    def test_no_extra_tables_in_yaml(self):
        """YAML shouldn't claim tables that don't get exported (otherwise
        downstream consumers will look for them in the ZIP and find nothing)."""
        from services import AnalyticsService
        contract = _load_contract()
        documented = set(contract["tables"].keys())
        exported = set(AnalyticsService.EXPORTABLE_TABLES.values())
        extra = documented - exported
        assert not extra, (
            f"Tables in YAML but not in EXPORTABLE_TABLES: {sorted(extra)}"
        )


class TestColumnsMatchDB:
    """Every column declared in YAML must exist in SQLite, and vice versa."""

    @pytest.fixture(autouse=True)
    def contract(self):
        self.c = _load_contract()

    async def test_no_undocumented_columns_in_db(self, db_columns):
        """If SQLite has a column the YAML doesn't, that's drift."""
        contract_tables = self.c["tables"]
        problems: list[str] = []
        for tname, db_cols in db_columns.items():
            if tname not in contract_tables:
                continue  # caught by TestAllExportedTablesDocumented
            yaml_cols = {col["name"] for col in contract_tables[tname]["columns"]}
            for db_col in db_cols.keys():
                if db_col not in yaml_cols:
                    problems.append(f"{tname}.{db_col} in DB but not in YAML")
        assert not problems, "\n".join(problems)

    async def test_no_phantom_columns_in_yaml(self, db_columns):
        """YAML must not declare columns the DB doesn't have."""
        contract_tables = self.c["tables"]
        problems: list[str] = []
        for tname, table_def in contract_tables.items():
            if tname not in db_columns:
                continue
            db_col_names = set(db_columns[tname].keys())
            for col in table_def["columns"]:
                if col["name"] not in db_col_names:
                    problems.append(
                        f"{tname}.{col['name']} in YAML but not in DB"
                    )
        assert not problems, "\n".join(problems)


class TestTypeFidelity:
    """SQLite affinity must match the YAML-declared semantic type bucket."""

    @pytest.fixture(autouse=True)
    def contract(self):
        self.c = _load_contract()

    async def test_yaml_type_matches_sqlite_affinity(self, db_columns):
        problems: list[str] = []
        contract_tables = self.c["tables"]
        for tname, table_def in contract_tables.items():
            if tname not in db_columns:
                continue
            for col in table_def["columns"]:
                if col["name"] not in db_columns[tname]:
                    continue
                expected_affinity = _YAML_TO_SQLITE_AFFINITY.get(col["type"])
                if expected_affinity is None:
                    problems.append(
                        f"{tname}.{col['name']}: YAML type "
                        f"'{col['type']}' not in known set "
                        f"{sorted(_YAML_TO_SQLITE_AFFINITY)}"
                    )
                    continue
                actual_affinity = db_columns[tname][col["name"]]["type"]
                if expected_affinity != actual_affinity:
                    problems.append(
                        f"{tname}.{col['name']}: YAML says '{col['type']}' "
                        f"(→ {expected_affinity}), DB has {actual_affinity}"
                    )
        assert not problems, "\n".join(problems)

    async def test_nullable_matches(self, db_columns):
        """YAML `nullable: false` must align with SQLite NOT NULL flag."""
        problems: list[str] = []
        contract_tables = self.c["tables"]
        for tname, table_def in contract_tables.items():
            if tname not in db_columns:
                continue
            for col in table_def["columns"]:
                if col["name"] not in db_columns[tname]:
                    continue
                db_info = db_columns[tname][col["name"]]
                yaml_nullable = col.get("nullable", True)
                db_nullable = not db_info["notnull"]
                # PK INTEGER columns are notnull=0 in PRAGMA output even
                # though they can't be NULL (SQLite quirk for INTEGER
                # PKs that auto-alias rowid). Skip those.
                if db_info["pk"] and db_info["type"] == "INTEGER":
                    continue
                if yaml_nullable != db_nullable:
                    problems.append(
                        f"{tname}.{col['name']}: YAML nullable={yaml_nullable}, "
                        f"DB nullable={db_nullable}"
                    )
        assert not problems, "\n".join(problems)


class TestPrimaryKeys:
    """primary_key list in YAML must reflect actual PK columns."""

    @pytest.fixture(autouse=True)
    def contract(self):
        self.c = _load_contract()

    async def test_pk_columns_match(self, db_columns):
        problems: list[str] = []
        for tname, table_def in self.c["tables"].items():
            if tname not in db_columns:
                continue
            yaml_pk = set(table_def.get("primary_key", []))
            db_pk = {n for n, info in db_columns[tname].items() if info["pk"]}
            if yaml_pk != db_pk:
                problems.append(
                    f"{tname}: YAML pk={sorted(yaml_pk)}, DB pk={sorted(db_pk)}"
                )
        assert not problems, "\n".join(problems)
