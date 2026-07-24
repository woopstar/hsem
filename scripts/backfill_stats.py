#!/usr/bin/env python3
"""
Backfill statistics from a Home Assistant SQLite backup into the running
database after a mass deletion (e.g. Developer Tools → Statistics →
"Delete selected entities").

Imports both ``statistics`` (long-term) and ``statistics_short_term``, remapping
metadata IDs between the two databases.  The ``id`` column is deliberately
omitted so the target auto-assigns fresh IDs — this avoids PK collisions.

Uses INSERT OR IGNORE, so any rows that already exist in the target (based on
the unique constraints ``(metadata_id, start_ts)`` for long-term and
``(metadata_id, created_ts)`` for short-term) are silently skipped.

Usage
-----
1. Stop Home Assistant
2. Backup the running database:
       cp home-assistant_v2.db home-assistant_v2.db.before-backfill
3. Dry-run to preview what will be imported:
       python backfill_stats.py /path/to/backup.db /path/to/home-assistant_v2.db --dry-run
4. When satisfied, run the actual import:
       python backfill_stats.py /path/to/backup.db /path/to/home-assistant_v2.db
5. Start Home Assistant
6. The last ~8 hours (since the backup) are gone — HA will rebuild them
   going forward from new short-term data.
"""

import argparse
import sqlite3
import sys
from typing import Any


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_metadata_map(source_cur, target_cur):
    """Return (source_id→statistic_id, statistic_id→target_id)."""
    source_cur.execute("SELECT id, statistic_id FROM statistics_meta")
    src: dict[int, str] = {row[0]: row[1] for row in source_cur.fetchall()}

    target_cur.execute("SELECT id, statistic_id FROM statistics_meta")
    tgt: dict[str, int] = {row[1]: row[0] for row in target_cur.fetchall()}

    return src, tgt


# ── per-table import ─────────────────────────────────────────────────────────

def _import_table(
    *,
    table: str,
    source_cur,
    target_cur,
    source_map: dict[int, str],
    target_map: dict[str, int],
    dry_run: bool,
) -> None:
    """Import every row from *table* in the backup into the target DB.

    ``id`` is excluded so the target generates its own primary keys.
    """
    # Determine columns to read / write (everything except 'id').
    source_cur.execute(f"PRAGMA table_info({table})")
    all_cols = [r[1] for r in source_cur.fetchall()]
    cols_no_id = [c for c in all_cols if c != "id"]

    col_list = ", ".join(cols_no_id)
    placeholders = ", ".join(["?"] * len(cols_no_id))

    insert_sql = (
        f"INSERT OR IGNORE INTO {table} ({col_list}) "
        f"VALUES ({placeholders})"
    )

    # metadata_id position within cols_no_id
    meta_idx = cols_no_id.index("metadata_id")

    source_cur.execute(f"SELECT COUNT(*) FROM {table}")
    total_rows: int = source_cur.fetchone()[0]

    batch_size = 1000
    offset = 0
    inserted = 0
    skipped_meta = 0
    no_source_meta: set[int] = set()

    print(f"\n{'─' * 55}")
    print(f"Table: {table}  ({total_rows:,} rows in backup)")
    print(f"{'─' * 55}")

    while True:
        source_cur.execute(
            f"SELECT {', '.join(cols_no_id)} FROM {table} "
            f"LIMIT {batch_size} OFFSET {offset}"
        )
        rows = source_cur.fetchall()

        if not rows:
            break

        batch: list[tuple[Any, ...]] = []
        for row in rows:
            src_meta_id: int = row[meta_idx]

            stat_id = source_map.get(src_meta_id)
            if stat_id is None:
                if src_meta_id not in no_source_meta:
                    print(
                        f"  WARNING: No metadata in source for "
                        f"metadata_id={src_meta_id}"
                    )
                    no_source_meta.add(src_meta_id)
                skipped_meta += 1
                continue

            tgt_meta_id = target_map.get(stat_id)
            if tgt_meta_id is None:
                skipped_meta += 1
                continue

            new_row = list(row)
            new_row[meta_idx] = tgt_meta_id
            batch.append(tuple(new_row))

        if not dry_run and batch:
            target_cur.executemany(insert_sql, batch)
            target_cur.connection.commit()

        inserted += len(batch)
        offset += batch_size

        if offset % (batch_size * 10) == 0 or offset >= total_rows:
            pct = min(100.0, (offset / total_rows) * 100) if total_rows else 100.0
            print(
                f"  Scanned {min(offset, total_rows):,} / {total_rows:,} "
                f"({pct:.0f}%)  —  {inserted:,} staged"
            )

    # summary
    already_there = total_rows - inserted - skipped_meta
    print(f"\n  {'Result:':<30} {inserted:>8,} rows staged for insert")
    print(f"  {'Already in target (skipped):':<30} {already_there:>8,}")
    print(f"  {'No metadata mapping (skipped):':<30} {skipped_meta:>8,}")


# ── main ─────────────────────────────────────────────────────────────────────

def backfill(source_db: str, target_db: str, dry_run: bool = False) -> None:
    # Open backup read-only so we can't corrupt it by accident.
    source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target_conn = sqlite3.connect(target_db)

    source_cur = source_conn.cursor()
    target_cur = target_conn.cursor()

    source_map, target_map = _build_metadata_map(source_cur, target_cur)

    # The metadata tables themselves differ (different auto-increment
    # sequences), so we build a cross-walk and import only the data tables.
    for table in ("statistics_short_term", "statistics"):
        _import_table(
            table=table,
            source_cur=source_cur,
            target_cur=target_cur,
            source_map=source_map,
            target_map=target_map,
            dry_run=dry_run,
        )

    source_conn.close()
    target_conn.close()

    print()
    if dry_run:
        print("DRY RUN — no changes were written to the target database.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill statistics tables from a HA SQLite backup."
    )
    parser.add_argument(
        "source_db", help="Path to the backup database (opened read-only)"
    )
    parser.add_argument("target_db", help="Path to the running database")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing to the target DB",
    )
    args = parser.parse_args()

    print(f"Source (backup):  {args.source_db}")
    print(f"Target (running): {args.target_db}")

    backfill(args.source_db, args.target_db, dry_run=args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
