"""Persistencia en SQLite: tandas, destinatarios, historial y duplicados.

Estados de un destinatario:
  pending   -> registrado, todavía no se ha intentado
  sending   -> se está entregando a Outlook en este momento
  accepted  -> Outlook aceptó la orden de envío (no implica entrega)
  error     -> error confirmado: el mensaje NO llegó a Outlook
  uncertain -> no se sabe si se envió; requiere revisión manual
"""

import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

STATUSES = ("pending", "sending", "accepted", "error", "uncertain")
# Estados que impiden un nuevo envío al mismo destinatario y periodo.
BLOCKING_STATUSES = ("pending", "sending", "uncertain")
# Estado que solo se puede repetir con una confirmación explícita.
RESENDABLE_STATUSES = ("accepted",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,
    period      TEXT NOT NULL,
    comm_type   TEXT NOT NULL,
    mailer      TEXT NOT NULL,
    sender      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'completed_with_errors', 'interrupted')),
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL UNIQUE,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    position     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    email_key    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    period       TEXT NOT NULL,
    comm_type    TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'accepted', 'error', 'uncertain')),
    attempted_at TEXT,
    error        TEXT,
    resolution   TEXT,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipients_key ON recipients (email_key, period, comm_type);
CREATE INDEX IF NOT EXISTS idx_recipients_batch ON recipients (batch_id);
"""


class DuplicateError(Exception):
    def __init__(self, conflicts):
        super().__init__("Hay destinatarios con envíos registrados para este periodo.")
        self.conflicts = conflicts


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _migrate(conn):
        # Bases creadas con versiones anteriores.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
        if "sender" not in cols:
            conn.execute("ALTER TABLE batches ADD COLUMN sender TEXT NOT NULL DEFAULT ''")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return _Conn(conn)

    # --- Recuperación y copias -------------------------------------------

    def recover_interrupted(self) -> int:
        """Llamar al arrancar: cierra tandas que quedaron a medias.

        - 'sending' pasa a 'uncertain' (pudo enviarse antes del cierre).
        - 'pending' pasa a 'error' (nunca se entregó a Outlook).
        Devuelve el número de destinatarios afectados.
        """
        ts = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            c1 = conn.execute(
                "UPDATE recipients SET status='uncertain', updated_at=?, "
                "error='La aplicación se cerró mientras se enviaba este mensaje. Compruebe en Outlook (Elementos enviados) si salió.' "
                "WHERE status='sending'", (ts,)).rowcount
            c2 = conn.execute(
                "UPDATE recipients SET status='error', updated_at=?, "
                "error='No enviado: la tanda se interrumpió antes de llegar a este destinatario.' "
                "WHERE status='pending'", (ts,)).rowcount
            conn.execute(
                "UPDATE batches SET status='interrupted', finished_at=? WHERE status='in_progress'", (ts,))
            conn.execute("COMMIT")
        return c1 + c2

    def backup(self, backup_dir, keep=30):
        """Copia de seguridad consistente (API de backup de SQLite)."""
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"{Path(self.path).stem}-{datetime.now():%Y%m%d-%H%M%S}.db"
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        backups = sorted(backup_dir.glob(f"{Path(self.path).stem}-*.db"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        return target

    # --- Duplicados ---------------------------------------------------------

    def find_conflicts(self, period_key, comm_type, email_keys, conn=None):
        """Registros previos que afectan a estos destinatarios en este periodo."""
        keys = sorted(set(email_keys))
        if not keys:
            return []
        own = conn is None
        conn = conn or self.connect()
        try:
            out = []
            # Se consulta en bloques para no superar el límite de parámetros de SQLite.
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT r.uid, r.email, r.email_key, r.name, r.status, r.attempted_at, r.amount_cents, b.id AS batch_id "
                    f"FROM recipients r JOIN batches b ON b.id = r.batch_id "
                    f"WHERE r.period=? AND r.comm_type=? AND r.email_key IN ({marks}) "
                    f"AND r.status IN ('pending','sending','uncertain','accepted') "
                    f"ORDER BY r.id", [period_key, comm_type, *chunk]).fetchall()
                out.extend(dict(r) for r in rows)
            return out
        finally:
            if own:
                conn.close()

    # --- Tandas -------------------------------------------------------------

    def create_batch(self, period_key, comm_type, mailer_name, clients, resend_ack=(), sender=""):
        """Crea la tanda y sus destinatarios en una única transacción.

        Lanza DuplicateError si algún destinatario tiene un envío en curso,
        incierto, o aceptado sin confirmación explícita de reenvío.
        """
        ack = {k.strip().lower() for k in resend_ack}
        ts = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conflicts = self.find_conflicts(period_key, comm_type, [c.email_key for c in clients], conn)
                blocking = [c for c in conflicts
                            if c["status"] in BLOCKING_STATUSES
                            or (c["status"] in RESENDABLE_STATUSES and c["email_key"] not in ack)]
                if blocking:
                    raise DuplicateError(blocking)
                cur = conn.execute(
                    "INSERT INTO batches (uid, period, comm_type, mailer, sender, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'in_progress', ?)",
                    (str(uuid.uuid4()), period_key, comm_type, mailer_name, sender, ts))
                batch_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO recipients (uid, batch_id, position, name, email, email_key, amount_cents, "
                    "period, comm_type, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    [(str(uuid.uuid4()), batch_id, i, c.name, c.email, c.email_key, c.amount_cents,
                      period_key, comm_type, ts) for i, c in enumerate(clients)])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return batch_id

    def pending_recipients(self, batch_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM recipients WHERE batch_id=? AND status='pending' ORDER BY position",
                (batch_id,)).fetchall()]

    def mark_sending(self, recipient_id):
        """Marca el destinatario como 'sending' y lo guarda ANTES de llamar a Outlook."""
        ts = now()
        with self.connect() as conn:
            n = conn.execute(
                "UPDATE recipients SET status='sending', attempted_at=?, updated_at=? "
                "WHERE id=? AND status='pending'", (ts, ts, recipient_id)).rowcount
        return n == 1

    def mark_result(self, recipient_id, status, error=None):
        if status not in ("accepted", "error", "uncertain"):
            raise ValueError(status)
        with self.connect() as conn:
            conn.execute(
                "UPDATE recipients SET status=?, error=?, updated_at=? WHERE id=? AND status='sending'",
                (status, error, now(), recipient_id))

    def abort_pending(self, batch_id, reason):
        with self.connect() as conn:
            conn.execute(
                "UPDATE recipients SET status='error', error=?, updated_at=? WHERE batch_id=? AND status='pending'",
                (reason, now(), batch_id))

    def finish_batch(self, batch_id):
        with self.connect() as conn:
            bad = conn.execute(
                "SELECT COUNT(*) FROM recipients WHERE batch_id=? AND status <> 'accepted'",
                (batch_id,)).fetchone()[0]
            conn.execute(
                "UPDATE batches SET status=?, finished_at=? WHERE id=? AND status='in_progress'",
                ("completed_with_errors" if bad else "completed", now(), batch_id))

    def get_batch(self, batch_id):
        with self.connect() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not b:
                return None
            recips = conn.execute(
                "SELECT uid, name, email, amount_cents, status, attempted_at, error FROM recipients "
                "WHERE batch_id=? ORDER BY position", (batch_id,)).fetchall()
        return {**dict(b), "recipients": [dict(r) for r in recips]}

    def active_batch_id(self):
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM batches WHERE status='in_progress' ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    # --- Historial ----------------------------------------------------------

    def history(self, period_key=None, status=None, limit=500):
        sql = ("SELECT r.uid, r.batch_id, r.name, r.email, r.amount_cents, r.period, r.status, "
               "r.attempted_at, r.error, r.resolution, b.created_at AS batch_created_at, b.sender "
               "FROM recipients r JOIN batches b ON b.id = r.batch_id WHERE 1=1")
        args = []
        if period_key:
            sql += " AND r.period=?"
            args.append(period_key)
        if status:
            sql += " AND r.status=?"
            args.append(status)
        sql += " ORDER BY r.id DESC LIMIT ?"
        args.append(int(limit))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def count_uncertain(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM recipients WHERE status='uncertain'").fetchone()[0]

    def resolve_uncertain(self, recipient_uid, sent: bool):
        """Resolución manual de un resultado incierto tras revisar Outlook."""
        new_status = "accepted" if sent else "error"
        note = (f"Revisado manualmente el {now()}: "
                + ("constaba como enviado en Outlook." if sent else "no constaba como enviado."))
        with self.connect() as conn:
            n = conn.execute(
                "UPDATE recipients SET status=?, resolution=?, updated_at=? WHERE uid=? AND status='uncertain'",
                (new_status, note, now(), recipient_uid)).rowcount
        return n == 1

    def last_batch_clients(self):
        """Clientes de la tanda más reciente, para reutilizarlos como borrador."""
        with self.connect() as conn:
            b = conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
            if not b:
                return []
            return [dict(r) for r in conn.execute(
                "SELECT name, email, amount_cents FROM recipients WHERE batch_id=? ORDER BY position",
                (b[0],)).fetchall()]


class _Conn:
    """Envoltorio para usar la conexión con 'with' y cerrarla siempre."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        self._conn.close()

    def close(self):
        self._conn.close()

