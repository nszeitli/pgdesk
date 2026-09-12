"""PostgreSQL catalog projection and bounded, lossless retained query results."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import sql

CATALOG_SQL = """
SELECT n.nspname, c.relname, c.relkind, a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_catalog.pg_get_expr(d.adbin, d.adrelid),
       (SELECT string_agg(pg_catalog.pg_get_constraintdef(k.oid), '; ' ORDER BY k.conname)
        FROM pg_catalog.pg_constraint k WHERE k.conrelid = c.oid),
       s.avg_width
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
LEFT JOIN pg_catalog.pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname
                              AND s.attname = a.attname AND NOT s.inherited
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp_%%'
  AND has_schema_privilege(n.oid, 'USAGE')
  AND (has_table_privilege(c.oid, 'SELECT') OR has_any_column_privilege(c.oid, 'SELECT'))
ORDER BY n.nspname, c.relname, a.attnum
"""
SCHEMAS_SQL = """
SELECT nspname FROM pg_catalog.pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
AND nspname NOT LIKE 'pg_toast%%' AND nspname NOT LIKE 'pg_temp_%%'
AND has_schema_privilege(oid, 'USAGE') ORDER BY nspname
"""
INDEXES_SQL = """
SELECT n.nspname, c.relname, pg_catalog.pg_get_indexdef(i.indexrelid)
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisvalid AND i.indisready
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp_%%'
  AND has_schema_privilege(n.oid, 'USAGE')
  AND (has_table_privilege(c.oid, 'SELECT') OR has_any_column_privilege(c.oid, 'SELECT'))
ORDER BY n.nspname, c.relname, i.indexrelid
"""


@dataclass(frozen=True)
class Column:
    """Column metadata sufficient for navigation and SQL generation."""

    name: str
    data_type: str
    not_null: bool
    default: str | None
    average_width: int | None = None


@dataclass(frozen=True)
class Relation:
    """One schema-qualified table or view with ordered columns and constraints."""

    schema: str
    name: str
    kind: str
    columns: tuple[Column, ...]
    constraints: str = ""
    indexes: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        """Return a safely quoted SQL identifier, including unusual names."""
        return sql.Identifier(self.schema, self.name).as_string()

    @property
    def is_view(self) -> bool:
        """Group normal and materialized views separately from tables."""
        return self.kind in {"v", "m"}


@dataclass(frozen=True)
class Catalog:
    """A complete accessible user-relation metadata snapshot for one database."""

    schemas: tuple[str, ...]
    relations: tuple[Relation, ...]

    def ai_context(self) -> str:
        """Serialize metadata only; no rows or credentials are available in this projection."""
        return json.dumps(
            {
                "schemas": self.schemas,
                "relations": [
                    {
                        "schema": r.schema,
                        "name": r.name,
                        "kind": r.kind,
                        "columns": [
                            {
                                "name": c.name,
                                "type": c.data_type,
                                "not_null": c.not_null,
                                "default": c.default,
                                "average_width": c.average_width,
                            }
                            for c in r.columns
                        ],
                        "constraints": r.constraints,
                        "indexes": r.indexes,
                    }
                    for r in self.relations
                ],
            },
            ensure_ascii=False,
        )


def build_catalog(schemas: list[tuple], rows: list[tuple], indexes: list[tuple]) -> Catalog:
    """Group the ordered catalog query without losing empty tables or schemas."""
    groups: dict[tuple[str, str], dict] = {}
    relation_indexes: dict[tuple[str, str], list[str]] = {}
    for schema, name, definition in indexes:
        relation_indexes.setdefault((schema, name), []).append(definition)
    for schema, name, kind, column, data_type, not_null, default, constraints, width in rows:
        group = groups.setdefault(
            (schema, name), {"kind": kind, "columns": [], "constraints": constraints or ""}
        )
        if column is not None:
            group["columns"].append(Column(column, data_type, not_null, default, width))
    relations = tuple(
        Relation(
            schema,
            name,
            group["kind"],
            tuple(group["columns"]),
            group["constraints"],
            tuple(relation_indexes.get((schema, name), ())),
        )
        for (schema, name), group in groups.items()
    )
    return Catalog(tuple(row[0] for row in schemas), relations)


def cell_text(value: Any) -> str:
    """Render data predictably while distinguishing SQL NULL in the grid."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


@dataclass(frozen=True)
class QueryResult:
    """One completed statement; retained rows may be an explicitly truncated prefix."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    status: str
    elapsed: float
    truncated: bool

    def export_csv(self, path: Path) -> int:
        """Exclusively create a UTF-8 CSV of retained rows; never overwrite an existing file."""
        fd = os.open(path.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(self.columns)
            for row in self.rows:
                writer.writerow("" if value is None else cell_text(value) for value in row)
        return len(self.rows)
