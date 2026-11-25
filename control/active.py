import sqlite3
from dataclasses import dataclass, field

from orders.bottles import BottleJob


@dataclass(frozen=True)
class ActiveCollection:
    job: BottleJob
    session_id: int
    command_id: str
    completion_number: int | None = None
    # Operational actual-start evidence; excluded from historical equality.
    basis: tuple[str, int | None] | None = field(default=None, compare=False)


def initialize_active(db: sqlite3.Connection) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS active_collection (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            order_id TEXT NOT NULL REFERENCES jobs(order_id),
            session_id INTEGER NOT NULL,
            command_id TEXT NOT NULL,
            completion_number INTEGER,
            basis_kind TEXT CHECK (basis_kind IN ('initial', 'rinse')),
            basis_rinse_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS control_progress (
            batch_id TEXT PRIMARY KEY NOT NULL REFERENCES batches(batch_id),
            position INTEGER NOT NULL CHECK (position > 0)
        );
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(active_collection)')}
    if 'basis_kind' not in columns:
        db.execute("ALTER TABLE active_collection ADD COLUMN basis_kind TEXT")
    if 'basis_rinse_id' not in columns:
        db.execute("ALTER TABLE active_collection ADD COLUMN basis_rinse_id INTEGER")


def load_active(db: sqlite3.Connection) -> ActiveCollection | None:
    row = db.execute('''
        SELECT j.order_id, j.batch_id, j.bottle_id, j.position, j.kind,
               a.session_id, a.command_id, a.completion_number,
               a.basis_kind, a.basis_rinse_id
        FROM active_collection AS a JOIN jobs AS j USING (order_id) WHERE a.slot = 1
    ''').fetchone()
    if row is None:
        return None
    job = BottleJob(*row[:5])
    basis = None if row[8] is None else (row[8], None if row[9] is None else int(row[9]))
    return ActiveCollection(job, row[5], row[6], row[7], basis)


def load_progress(db: sqlite3.Connection, batch_id: str) -> int:
    row = db.execute('SELECT position FROM control_progress WHERE batch_id = ?', (batch_id,)).fetchone()
    return 0 if row is None else row[0]


def reserve_attempt(db: sqlite3.Connection, job: BottleJob, session_id: int, command_id: str,
                    prior: ActiveCollection | None = None) -> ActiveCollection | None:
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = load_active(db)
        if current != prior or job.position != load_progress(db, job.batch_id) + 1:
            return None
        if prior is None:
            db.execute('''
                INSERT INTO active_collection
                    (slot, order_id, session_id, command_id, completion_number)
                VALUES (1, ?, ?, ?, NULL)
            ''', (job.order_id, session_id, command_id))
        else:
            if prior.completion_number is not None or prior.job != job:
                return None
            db.execute('UPDATE active_collection SET session_id = ?, command_id = ? WHERE slot = 1',
                       (session_id, command_id))
    return ActiveCollection(job, session_id, command_id)


def record_completion(db: sqlite3.Connection, attempt: ActiveCollection,
                      number: int,
                      basis: tuple[str, int | None] | None = None) -> ActiveCollection | None:
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = load_active(db)
        if current is None or (current.job, current.session_id, current.command_id) != (
            attempt.job, attempt.session_id, attempt.command_id
        ):
            return None
        if current.completion_number not in (None, number):
            return None
        if current.basis is not None and basis is not None and current.basis != basis:
            return None
        basis_to_store = current.basis or basis
        basis_kind = None if basis_to_store is None else basis_to_store[0]
        basis_rinse_id = None if basis_to_store is None else basis_to_store[1]
        db.execute('''
            UPDATE active_collection
            SET completion_number = ?, basis_kind = ?, basis_rinse_id = ?
            WHERE slot = 1
        ''', (number, basis_kind, basis_rinse_id))
    return ActiveCollection(attempt.job, attempt.session_id, attempt.command_id, number,
                            basis_to_store)


def confirm_delivery(db: sqlite3.Connection, attempt: ActiveCollection,
                     published_completion: int) -> bool:
    if attempt.completion_number != published_completion:
        return False
    job = attempt.job
    with db:
        db.execute('BEGIN IMMEDIATE')
        progress = load_progress(db, job.batch_id)
        if progress >= job.position:
            return True
        if load_active(db) != attempt or progress + 1 != job.position:
            return False
        db.execute('''
            INSERT INTO control_progress VALUES (?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET position = excluded.position
        ''', (job.batch_id, job.position))
        db.execute('DELETE FROM active_collection WHERE slot = 1')
    return True
