import sqlite3


class ArchiveStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def initialize(self) -> None:
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS samples (
                order_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(order_id),
                completion_number INTEGER NOT NULL UNIQUE,
                raw_code TEXT NOT NULL,
                numerator TEXT NOT NULL,
                denominator TEXT NOT NULL,
                unit TEXT NOT NULL,
                UNIQUE (order_id, completion_number)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                order_id TEXT PRIMARY KEY NOT NULL,
                completion_number INTEGER NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY (order_id, completion_number) REFERENCES samples(order_id, completion_number)
            );
            CREATE TABLE IF NOT EXISTS sensitive_bases (
                order_id TEXT PRIMARY KEY NOT NULL REFERENCES samples(order_id),
                basis_kind TEXT NOT NULL CHECK (basis_kind IN ('initial', 'rinse')),
                basis_rinse_id INTEGER
            );
        ''')

    def lookup(self, order_id: str) -> tuple | None:
        return self.db.execute('''
            SELECT j.order_id, j.batch_id, j.bottle_id, s.completion_number, s.raw_code,
                   s.numerator, s.denominator, s.unit, r.payload
            FROM samples AS s JOIN jobs AS j ON j.order_id = s.order_id
            JOIN receipts AS r ON r.order_id = s.order_id AND r.completion_number = s.completion_number
            WHERE s.order_id = ?
        ''', (order_id,)).fetchone()

    def commit_pair(self, order_id: str, number: int, raw_code: int, numerator: int,
                    denominator: int, unit: str, receipt_payload: str,
                    basis: tuple[str, int | None] | None = None) -> tuple:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            stored = self.lookup(order_id)
            if stored is None:
                self.db.execute('INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)',
                                (order_id, number, str(raw_code), str(numerator),
                                 str(denominator), unit))
                self.db.execute('INSERT INTO receipts VALUES (?, ?, ?)',
                                (order_id, number, receipt_payload))
                if basis is not None:
                    basis_kind, rinse_id = basis
                    self.db.execute('INSERT INTO sensitive_bases VALUES (?, ?, ?)',
                                    (order_id, basis_kind, rinse_id))
                stored = self.lookup(order_id)
                if stored is None:
                    raise sqlite3.IntegrityError('sample and receipt could not be paired')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return stored

    def lookup_sensitive(self, order_id: str) -> tuple | None:
        return self.db.execute('''
            SELECT j.order_id, j.batch_id, j.bottle_id, s.completion_number, s.raw_code,
                   s.numerator, s.denominator, s.unit, r.payload,
                   b.basis_kind, b.basis_rinse_id
            FROM samples AS s
            JOIN jobs AS j ON j.order_id = s.order_id
            JOIN receipts AS r
              ON r.order_id = s.order_id AND r.completion_number = s.completion_number
            JOIN sensitive_bases AS b ON b.order_id = s.order_id
            WHERE s.order_id = ?
        ''', (order_id,)).fetchone()
