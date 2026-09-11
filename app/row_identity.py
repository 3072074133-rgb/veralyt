from __future__ import annotations

from typing import Any


INTERNAL_ROW_ID = "__aa_row_id"
ROW_SEQUENCE_TABLE = "__aa_row_sequences"


def quote_identifier(value: str) -> str:
    return value.replace('"', '""')


def table_has_internal_row_id(connection: Any, table_name: str) -> bool:
    row = connection.execute(
        """SELECT 1 FROM information_schema.columns
        WHERE table_name=? AND column_name=? LIMIT 1""",
        [table_name, INTERNAL_ROW_ID],
    ).fetchone()
    return row is not None


def ensure_internal_row_id(connection: Any, table_name: str) -> None:
    if table_has_internal_row_id(connection, table_name):
        return
    table = quote_identifier(table_name)
    column = quote_identifier(INTERNAL_ROW_ID)
    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" BIGINT')
    connection.execute(f'UPDATE "{table}" SET "{column}" = rowid + 1')


def initialize_row_sequence(connection: Any, dataset_id: str, table_name: str) -> None:
    table = quote_identifier(table_name)
    column = quote_identifier(INTERNAL_ROW_ID)
    next_id = int(
        connection.execute(
            f'SELECT COALESCE(MAX("{column}"), 0) + 1 FROM "{table}"'
        ).fetchone()[0]
    )
    _ensure_sequence_table(connection)
    connection.execute(
        f'INSERT OR IGNORE INTO "{ROW_SEQUENCE_TABLE}" VALUES (?, ?)',
        [dataset_id, next_id],
    )
    connection.execute(
        f'UPDATE "{ROW_SEQUENCE_TABLE}" SET next_id=GREATEST(next_id, ?) WHERE dataset_id=?',
        [next_id, dataset_id],
    )


def reserve_row_ids(
    connection: Any, dataset_id: str, count: int, minimum_next_id: int
) -> int:
    _ensure_sequence_table(connection)
    connection.execute(
        f'INSERT OR IGNORE INTO "{ROW_SEQUENCE_TABLE}" VALUES (?, ?)',
        [dataset_id, minimum_next_id],
    )
    row = connection.execute(
        f'SELECT next_id FROM "{ROW_SEQUENCE_TABLE}" WHERE dataset_id=?',
        [dataset_id],
    ).fetchone()
    start = max(int(row[0]), minimum_next_id)
    connection.execute(
        f'UPDATE "{ROW_SEQUENCE_TABLE}" SET next_id=? WHERE dataset_id=?',
        [start + count, dataset_id],
    )
    return start


def _ensure_sequence_table(connection: Any) -> None:
    connection.execute(
        f'''CREATE TABLE IF NOT EXISTS "{ROW_SEQUENCE_TABLE}" (
        dataset_id VARCHAR PRIMARY KEY,
        next_id BIGINT NOT NULL
        )'''
    )
