"""Copy SATQUERY records from SQLite into an empty PostgreSQL database.

The source database is opened read-only. By default this command only reports
source row counts. Passing --apply requires DATABASE_URL to be PostgreSQL and
the destination SATQUERY tables to be empty. No source or destination rows are
deleted or overwritten.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)


METADATA = MetaData()
USERS = Table(
    "users", METADATA,
    Column("id", Integer, primary_key=True),
    Column("email", String, unique=True, index=True),
    Column("name", String),
    Column("password", String),
    Column("role", String),
    Column("active", Boolean),
    Column("created", DateTime),
)
ANALYSES = Table(
    "analyses", METADATA,
    Column("id", String, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("kind", String),
    Column("name", String),
    Column("location", String),
    Column("lat", String),
    Column("lon", String),
    Column("source", String),
    Column("result", Text),
    Column("created", DateTime),
)
NOTIFICATIONS = Table(
    "notifications", METADATA,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("message", String),
    Column("created", DateTime),
)
ORIGINAL_IMAGES = Table(
    "original_images", METADATA,
    Column("id", Integer, primary_key=True),
    Column("analysis_id", String, ForeignKey("analyses.id", ondelete="CASCADE"), index=True),
    Column("role", String),
    Column("filename", String),
    Column("content_type", String),
    Column("data", LargeBinary),
    Column("created", DateTime),
    UniqueConstraint("analysis_id", "role", name="uq_original_image_analysis_role"),
)
TABLES = (USERS, ANALYSES, NOTIFICATIONS, ORIGINAL_IMAGES)
LEGACY_TABLES = ("users", "analyses", "notifications")
OPTIONAL_TABLES = ("original_images",)
MIGRATION_TABLES = LEGACY_TABLES + OPTIONAL_TABLES


def _parse_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def read_source(source: Path) -> dict[str, list[dict]]:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {source}")
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(LEGACY_TABLES) - present
        if missing:
            raise ValueError("SQLite source is missing required SATQUERY tables")
        rows = {name: [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')] for name in LEGACY_TABLES}
        rows["original_images"] = (
            [dict(row) for row in connection.execute('SELECT * FROM "original_images"')]
            if "original_images" in present else []
        )
    finally:
        connection.close()
    for table_rows in rows.values():
        for row in table_rows:
            if "created" in row:
                row["created"] = _parse_datetime(row["created"])
    return rows


def migrate_records(source_rows: dict[str, list[dict]], target_engine) -> dict[str, int]:
    """Atomically insert records into an empty target; safe to exercise with SQLite tests."""
    METADATA.create_all(target_engine)
    expected = {name: len(source_rows[name]) for name in MIGRATION_TABLES}
    with target_engine.begin() as connection:
        existing = {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one() for table in TABLES}
        if any(existing.values()):
            raise ValueError("Destination SATQUERY tables are not empty; refusing to merge or overwrite records")
        for name in ("users", "analyses", "original_images", "notifications"):
            records = source_rows[name]
            if records:
                table = {"users": USERS, "analyses": ANALYSES, "notifications": NOTIFICATIONS, "original_images": ORIGINAL_IMAGES}[name]
                connection.execute(table.insert(), records)
        actual = {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one() for table in TABLES}
        if any(actual[name] != expected[name] for name in MIGRATION_TABLES):
            raise RuntimeError("Destination row counts did not match the SQLite source")
        if target_engine.dialect.name == "postgresql":
            for table in ("users", "notifications", "original_images"):
                connection.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                    f"EXISTS(SELECT 1 FROM {table}))"
                ))
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "satquery.db")
    parser.add_argument("--target-url", default=None, help="PostgreSQL URL; defaults to DATABASE_URL")
    parser.add_argument("--apply", action="store_true", help="Write to an empty PostgreSQL target (default is dry-run)")
    args = parser.parse_args()
    try:
        source_rows = read_source(args.source)
        counts = {name: len(source_rows[name]) for name in MIGRATION_TABLES}
        print("SQLite source row counts:", counts)
        if not args.apply:
            print("Dry run only. Review the source and take a PostgreSQL backup before using --apply.")
            return 0
        target_url = args.target_url or __import__("os").environ.get("DATABASE_URL", "")
        if not target_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
            raise ValueError("--apply requires a PostgreSQL --target-url or DATABASE_URL")
        if target_url.startswith("postgres://"):
            target_url = "postgresql+psycopg://" + target_url[len("postgres://"):]
        elif target_url.startswith("postgresql://"):
            target_url = "postgresql+psycopg://" + target_url[len("postgresql://"):]
        target_engine = create_engine(target_url, pool_pre_ping=True)
        try:
            migrated = migrate_records(source_rows, target_engine)
            print("Migration committed:", migrated)
        finally:
            target_engine.dispose()
        return 0
    except Exception as error:
        # Avoid printing SQLAlchemy exception text, which can contain connection credentials.
        print(f"Migration stopped safely ({type(error).__name__}). No SQLite source rows were changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
