import sqlite3
from collections.abc import Iterable

from orders.bottles import BottleJob


_JOB_COLUMNS = 'order_id, batch_id, bottle_id, position, kind'


def initialize_queue(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            order_id TEXT PRIMARY KEY NOT NULL,
            batch_id TEXT NOT NULL REFERENCES batches(batch_id),
            bottle_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (position > 0),
            kind TEXT NOT NULL DEFAULT 'ordinary'
                CHECK (kind IN ('ordinary', 'sensitive')),
            UNIQUE(batch_id, position)
        )
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
    if 'kind' not in columns:
        # Existing work databases only contain ordinary registrations.
        db.execute('''
            ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'ordinary'
                CHECK (kind IN ('ordinary', 'sensitive'))
        ''')


def find_job(db: sqlite3.Connection, order_id: str) -> BottleJob | None:
    row = db.execute(f'SELECT {_JOB_COLUMNS} FROM jobs WHERE order_id = ?',
                     (order_id,)).fetchone()
    return None if row is None else BottleJob(*row)


def append_jobs(db: sqlite3.Connection, batch_id: str,
                items: Iterable[tuple[str, str]], *, kind: str = 'ordinary') -> list[BottleJob]:
    """Append previously unseen orders in the caller's admission transaction."""
    if kind not in ('ordinary', 'sensitive'):
        raise ValueError(f'unknown order kind {kind!r}')
    (tail,) = db.execute('SELECT COALESCE(MAX(position), 0) FROM jobs WHERE batch_id = ?',
                         (batch_id,)).fetchone()
    jobs = []
    for position, (order_id, bottle_id) in enumerate(items, tail + 1):
        db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)',
                   (order_id, batch_id, bottle_id, position, kind))
        jobs.append(BottleJob(order_id, batch_id, bottle_id, position, kind))
    return jobs


def jobs_after(db: sqlite3.Connection, batch_id: str, position: int,
               limit: int = -1) -> list[BottleJob]:
    rows = db.execute(f'''
        SELECT {_JOB_COLUMNS} FROM jobs WHERE batch_id = ? AND position > ?
        ORDER BY position LIMIT ?
    ''', (batch_id, position, limit))
    return [BottleJob(*row) for row in rows]
