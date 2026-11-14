"""Single source of truth for the legacy table: column mapping, types, DDL and DML.

Column names reproduce a messy 2000s enterprise schema on purpose; the agent has to cope with them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Bump when mapping/typing/validation changes so an unchanged CSV is reloaded anyway.
LOADER_VERSION = "1"

STAGING_SUFFIX = "__STG"
OLD_SUFFIX = "__OLD"
AUDIT_TABLE = "TBL_SC_INGEST_AUDIT"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")


def quote_ident(name: str) -> str:
    """Bracket-quote an identifier after whitelisting it (table/schema names come from config)."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f"[{name}]"


@dataclass(frozen=True)
class ColumnSpec:
    source: str | None  # CSV column; None for columns the loader derives
    target: str
    sql_type: str


COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("timestamp", "TS_UTC", "DATETIME2(0)"),
    ColumnSpec("vehicle_gps_latitude", "V_LAT", "FLOAT"),
    ColumnSpec("vehicle_gps_longitude", "V_LON", "FLOAT"),
    ColumnSpec("iot_temperature", "IOT_TEMP_VAL_C", "FLOAT"),
    ColumnSpec("cargo_condition_status", "CGO_COND_CD", "FLOAT"),
    ColumnSpec("risk_classification", "RISK_CLS_TXT", "VARCHAR(20)"),
    ColumnSpec("delay_probability", "DELAY_PROB_DEC", "FLOAT"),
    ColumnSpec("port_congestion_level", "PRT_CNG_LVL", "FLOAT"),
    ColumnSpec("route_risk_level", "RT_RSK_IDX", "FLOAT"),
    ColumnSpec(None, "SYS_INGEST_FLAG", "CHAR(1)"),
)

PRIMARY_KEY = "TS_UTC"
RISK_COLUMN = "RISK_CLS_TXT"
FLAG_COLUMN = "SYS_INGEST_FLAG"
RISK_CLASSES: tuple[str, ...] = ("Low Risk", "Moderate Risk", "High Risk")

LEGACY_MAPPING: dict[str, str] = {c.source: c.target for c in COLUMNS if c.source}
SOURCE_COLUMNS: tuple[str, ...] = tuple(LEGACY_MAPPING)
TARGET_COLUMNS: tuple[str, ...] = tuple(c.target for c in COLUMNS)
NUMERIC_COLUMNS: tuple[str, ...] = tuple(c.target for c in COLUMNS if c.sql_type == "FLOAT")


@dataclass(frozen=True)
class TableRef:
    schema: str
    name: str

    def __post_init__(self) -> None:
        quote_ident(self.schema)
        quote_ident(self.name)

    @property
    def qualified(self) -> str:
        """``[schema].[name]`` for use inside SQL text."""
        return f"{quote_ident(self.schema)}.{quote_ident(self.name)}"

    @property
    def object_name(self) -> str:
        """``schema.name`` for OBJECT_ID() / sp_rename parameters."""
        return f"{self.schema}.{self.name}"

    def with_suffix(self, suffix: str) -> "TableRef":
        return TableRef(self.schema, self.name + suffix)


def constraint_names(table_name: str) -> dict[str, str]:
    """Constraint names derive from the table name, so staging and live never collide."""
    return {
        "pk": f"PK_{table_name}",
        "df_flag": f"DF_{table_name}_FLAG",
        "ck_risk": f"CK_{table_name}_RISK",
        "ck_flag": f"CK_{table_name}_FLAG",
    }


def create_table_sql(table: TableRef) -> str:
    names = {k: quote_ident(v) for k, v in constraint_names(table.name).items()}
    lines = []
    for col in COLUMNS:
        line = f"    {quote_ident(col.target)} {col.sql_type} NOT NULL"
        if col.target == PRIMARY_KEY:
            line += f" CONSTRAINT {names['pk']} PRIMARY KEY CLUSTERED"
        elif col.target == FLAG_COLUMN:
            line += f" CONSTRAINT {names['df_flag']} DEFAULT 'Y'"
        lines.append(line)
    risk_values = ", ".join(f"'{value}'" for value in RISK_CLASSES)
    lines.append(f"    CONSTRAINT {names['ck_risk']} CHECK ({quote_ident(RISK_COLUMN)} IN ({risk_values}))")
    lines.append(f"    CONSTRAINT {names['ck_flag']} CHECK ({quote_ident(FLAG_COLUMN)} IN ('Y', 'N'))")
    return f"CREATE TABLE {table.qualified} (\n" + ",\n".join(lines) + "\n);"


def insert_sql(table: TableRef) -> str:
    cols = ", ".join(quote_ident(c) for c in TARGET_COLUMNS)
    params = ", ".join("?" for _ in TARGET_COLUMNS)
    return f"INSERT INTO {table.qualified} ({cols}) VALUES ({params});"


def merge_sql(target: TableRef, source: TableRef) -> str:
    """MERGE staging into live on the PK; returns one row ``(inserted, updated)``."""
    pk = quote_ident(PRIMARY_KEY)
    non_key = [quote_ident(c) for c in TARGET_COLUMNS if c != PRIMARY_KEY]
    all_cols = [quote_ident(c) for c in TARGET_COLUMNS]
    # EXISTS(SELECT src EXCEPT SELECT tgt) is a NULL-safe "any column differs" test.
    changed = (
        f"EXISTS (SELECT {', '.join('src.' + c for c in non_key)} "
        f"EXCEPT SELECT {', '.join('tgt.' + c for c in non_key)})"
    )
    return (
        "SET NOCOUNT ON;\n"
        "DECLARE @changes TABLE ([action] NVARCHAR(10));\n"
        f"MERGE {target.qualified} WITH (HOLDLOCK) AS tgt\n"
        f"USING {source.qualified} AS src ON tgt.{pk} = src.{pk}\n"
        f"WHEN MATCHED AND {changed} THEN\n"
        f"    UPDATE SET {', '.join(f'tgt.{c} = src.{c}' for c in non_key)}\n"
        "WHEN NOT MATCHED BY TARGET THEN\n"
        f"    INSERT ({', '.join(all_cols)}) VALUES ({', '.join('src.' + c for c in all_cols)})\n"
        "OUTPUT $action INTO @changes;\n"
        "SELECT COALESCE(SUM(CASE WHEN [action] = 'INSERT' THEN 1 ELSE 0 END), 0),\n"
        "       COALESCE(SUM(CASE WHEN [action] = 'UPDATE' THEN 1 ELSE 0 END), 0)\n"
        "FROM @changes;"
    )
