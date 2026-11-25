import sqlite3

from device.recording import DeviceError


def validate_flow_settings(initial_clean: object, doses: object) -> tuple[bool, int]:
    if not isinstance(initial_clean, bool):
        raise ValueError('initial_clean must be a boolean')
    if not isinstance(doses, int) or isinstance(doses, bool) or doses < 0:
        raise ValueError('doses must be a nonnegative integer')
    return initial_clean, doses


def initialize_flow_tables(db: sqlite3.Connection) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS flow_config (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            initial_clean INTEGER NOT NULL,
            doses INTEGER NOT NULL CHECK (doses >= 0)
        );
        CREATE TABLE IF NOT EXISTS rinse_identities (
            rinse_id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE IF NOT EXISTS flow_status (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            clean INTEGER NOT NULL,
            basis_kind TEXT CHECK (basis_kind IN ('initial', 'rinse')),
            basis_rinse_id INTEGER UNIQUE REFERENCES rinse_identities(rinse_id),
            CHECK (clean = 0 OR basis_kind IS NOT NULL)
        );
        CREATE TABLE IF NOT EXISTS command_clearance (
            session_id INTEGER NOT NULL,
            command_id TEXT NOT NULL,
            basis_kind TEXT NOT NULL CHECK (basis_kind IN ('initial', 'rinse')),
            basis_rinse_id INTEGER REFERENCES rinse_identities(rinse_id),
            PRIMARY KEY (session_id, command_id),
            FOREIGN KEY (session_id, command_id)
                REFERENCES commands(session_id, command_id)
        );
        CREATE TABLE IF NOT EXISTS flow_reservation (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            bottle_id TEXT NOT NULL,
            session_id INTEGER,
            command_id TEXT
        );
    ''')


def configure_flow(db: sqlite3.Connection, initial_clean: object, doses: object) -> None:
    clean, dose_count = validate_flow_settings(initial_clean, doses)
    row = db.execute('SELECT initial_clean, doses FROM flow_config WHERE slot = 1').fetchone()
    if row is not None:
        if tuple(row) != (int(clean), dose_count):
            raise ValueError('shared flow settings conflict with the existing recording')
        return
    db.execute('INSERT INTO flow_config VALUES (1, ?, ?)', (int(clean), dose_count))
    db.execute('''
        INSERT INTO flow_status(slot, clean, basis_kind, basis_rinse_id)
        VALUES (1, ?, ?, NULL)
        ON CONFLICT(slot) DO UPDATE SET
            clean = excluded.clean,
            basis_kind = excluded.basis_kind,
            basis_rinse_id = NULL
    ''', (int(clean), 'initial' if clean else None))


def ensure_status(db: sqlite3.Connection) -> None:
    db.execute('''
        INSERT INTO flow_status(slot, clean, basis_kind, basis_rinse_id)
        VALUES (1, 1, 'initial', NULL)
        ON CONFLICT(slot) DO NOTHING
    ''')


def flow_configured(db: sqlite3.Connection) -> bool:
    return db.execute('SELECT 1 FROM flow_config WHERE slot = 1').fetchone() is not None


def rinse_history(db: sqlite3.Connection) -> tuple[int, ...]:
    rows = db.execute('SELECT rinse_id FROM rinse_identities ORDER BY rinse_id').fetchall()
    return tuple(int(row[0]) for row in rows)


def remaining_doses(db: sqlite3.Connection) -> int:
    row = db.execute('SELECT doses FROM flow_config WHERE slot = 1').fetchone()
    if row is None:
        return 0
    used = db.execute('SELECT COUNT(*) FROM rinse_identities').fetchone()[0]
    return max(0, int(row[0]) - int(used))


def current_clearance(db: sqlite3.Connection) -> tuple[bool, tuple[str, int | None] | None]:
    if not flow_configured(db):
        return False, None
    ensure_status(db)
    clean, kind, rinse_id = db.execute('''
        SELECT clean, basis_kind, basis_rinse_id FROM flow_status WHERE slot = 1
    ''').fetchone()
    if not clean or kind is None:
        return False, None
    return True, (kind, None if rinse_id is None else int(rinse_id))


def clearance_code(db: sqlite3.Connection) -> str:
    if not flow_configured(db):
        return 'flow_disabled'
    clean, _basis = current_clearance(db)
    if clean:
        return 'clean'
    return 'rinse_available' if remaining_doses(db) > 0 else 'rinse_unavailable'


def complete_rinse(db: sqlite3.Connection) -> int:
    if remaining_doses(db) <= 0:
        raise DeviceError('no rinse dose remains')
    cursor = db.execute('INSERT INTO rinse_identities DEFAULT VALUES')
    rinse_id = int(cursor.lastrowid)
    db.execute('''
        UPDATE flow_status
        SET clean = 1, basis_kind = 'rinse', basis_rinse_id = ?
        WHERE slot = 1
    ''', (rinse_id,))
    return rinse_id


def next_queued_command_requires_clean(db: sqlite3.Connection) -> bool:
    row = db.execute('''
        SELECT c.requires_clean
        FROM commands AS c
        JOIN sessions AS s USING (session_id)
        WHERE c.state = 'queued' AND s.mode IN ('open', 'seal')
          AND NOT EXISTS (SELECT 1 FROM commands WHERE state = 'running')
        ORDER BY c.queue_id
        LIMIT 1
    ''').fetchone()
    return row is not None and bool(row[0])


def rinse_before_next_command(db: sqlite3.Connection) -> int | None:
    """Complete one rinse if the next command needs a clean path.

    Returns a positive rinse identity when a rinse was performed, zero when the
    path is dirty and no dose remains, and ``None`` when ordinary start logic
    should proceed.
    """
    if not next_queued_command_requires_clean(db):
        return None
    if not flow_configured(db):
        return 0
    clean, _basis = current_clearance(db)
    if clean:
        return None
    if remaining_doses(db) <= 0:
        return 0
    return complete_rinse(db)


def begin_command_clearance(db: sqlite3.Connection, requires_clean: bool) -> tuple[str, int | None] | None:
    """Apply contamination/clearance rules at the physical aspiration start."""
    if not flow_configured(db):
        # Legacy/non-flow recordings retain ordinary behavior, but a sensitive
        # command cannot establish the cleanliness required to consume liquid.
        return None
    ensure_status(db)
    if not requires_clean:
        # An ordinary aspiration is itself a contamination event.
        db.execute('''
            UPDATE flow_status
            SET clean = 0, basis_kind = NULL, basis_rinse_id = NULL
            WHERE slot = 1
        ''')
        return None

    clean, basis = current_clearance(db)
    if not clean or basis is None:
        return None
    return basis


def record_command_basis(db: sqlite3.Connection, session_id: int, command_id: str,
                         basis: tuple[str, int | None]) -> None:
    kind, rinse_id = basis
    db.execute('''
        INSERT INTO command_clearance(session_id, command_id, basis_kind, basis_rinse_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(session_id, command_id) DO UPDATE SET
            basis_kind = excluded.basis_kind,
            basis_rinse_id = excluded.basis_rinse_id
    ''', (session_id, command_id, kind, rinse_id))


def command_basis(db: sqlite3.Connection, session_id: int,
                  command_id: str) -> tuple[str, int | None] | None:
    row = db.execute('''
        SELECT basis_kind, basis_rinse_id FROM command_clearance
        WHERE session_id = ? AND command_id = ?
    ''', (session_id, command_id)).fetchone()
    return None if row is None else (row[0], None if row[1] is None else int(row[1]))


def reserve_flow(db: sqlite3.Connection, bottle_id: str | None = None,
                 session_id: int | None = None,
                 command_id: str | None = None) -> str:
    from orders.bottles import validate_identifier

    if bottle_id is None and session_id is not None and command_id is not None:
        row = db.execute('''
            SELECT bottle_id FROM commands
            WHERE session_id = ? AND command_id = ?
        ''', (session_id, command_id)).fetchone()
        if row is not None:
            bottle_id = row[0]
    if bottle_id is None:
        row = db.execute('''
            SELECT c.bottle_id, c.session_id, c.command_id
            FROM commands AS c
            JOIN sessions AS s USING (session_id)
            WHERE c.state = 'queued' AND c.requires_clean = 1
              AND s.mode IN ('open', 'seal')
            ORDER BY c.queue_id LIMIT 1
        ''').fetchone()
        if row is not None:
            bottle_id, session_id, command_id = row
    if bottle_id is None:
        raise ValueError('a bottle is required for a flow reservation')
    validate_identifier(bottle_id, 'bottle ID')
    db.execute('''
        INSERT INTO flow_reservation(slot, bottle_id, session_id, command_id)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(slot) DO UPDATE SET
            bottle_id = excluded.bottle_id,
            session_id = excluded.session_id,
            command_id = excluded.command_id
    ''', (bottle_id, session_id, command_id))
    return 'busy'


def release_flow(db: sqlite3.Connection, bottle_id: str | None = None,
                 session_id: int | None = None, command_id: str | None = None) -> bool:
    if bottle_id is None and session_id is None and command_id is None:
        cursor = db.execute('DELETE FROM flow_reservation WHERE slot = 1')
        return cursor.rowcount > 0
    clauses = []
    values = []
    if bottle_id is not None:
        clauses.append('bottle_id = ?')
        values.append(bottle_id)
    if session_id is not None:
        clauses.append('session_id = ?')
        values.append(session_id)
    if command_id is not None:
        clauses.append('command_id = ?')
        values.append(command_id)
    cursor = db.execute(f'DELETE FROM flow_reservation WHERE {" AND ".join(clauses)}', values)
    return cursor.rowcount > 0


def active_reservation(db: sqlite3.Connection, bottle_id: str | None = None) -> tuple | None:
    row = db.execute('''
        SELECT bottle_id, session_id, command_id
        FROM flow_reservation WHERE slot = 1
    ''').fetchone()
    if row is None:
        return None
    reserved_bottle, session_id, command_id = row
    if bottle_id is not None and reserved_bottle != bottle_id:
        return None
    # A command-linked reservation is released if its command is already
    # cancelled or completed; physical start is released by start_command too.
    if session_id is not None and command_id is not None:
        state = db.execute('''
            SELECT state FROM commands WHERE session_id = ? AND command_id = ?
        ''', (session_id, command_id)).fetchone()
        if state is None or state[0] in ('cancelled', 'completed', 'running'):
            release_flow(db, session_id=session_id, command_id=command_id)
            return None
    return tuple(row)


def maintenance_contact(db: sqlite3.Connection, bottle_id: str | None = None) -> str:
    # The path is shared, so any live bottle reservation blocks contact even if
    # the caller labels the contact with a different bottle identifier.
    if active_reservation(db) is not None:
        return 'busy'
    ensure_status(db)
    db.execute('''
        UPDATE flow_status
        SET clean = 0, basis_kind = NULL, basis_rinse_id = NULL
        WHERE slot = 1
    ''')
    return 'applied'
