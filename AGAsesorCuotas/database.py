"""Persistencia en SQLite: tandas, destinatarios, historial y duplicados.

Estados de un destinatario:
  pending   -> registrado, todavía no se ha intentado
  sending   -> se está entregando a Outlook en este momento
  accepted  -> Outlook aceptó la orden de envío (no implica entrega)
  error     -> error confirmado: el mensaje NO llegó a Outlook
  uncertain -> no se sabe si se envió; requiere revisión manual
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import core

STATUSES = ("pending", "sending", "accepted", "error", "uncertain")
# Estados que impiden un nuevo envío al mismo destinatario y periodo.
BLOCKING_STATUSES = ("pending", "sending", "uncertain")
# Estado que solo se puede repetir con una confirmación explícita.
RESENDABLE_STATUSES = ("accepted",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT NOT NULL UNIQUE,
    period        TEXT NOT NULL,
    comm_type     TEXT NOT NULL,
    mailer        TEXT NOT NULL,
    sender        TEXT NOT NULL DEFAULT '',
    template_id   INTEGER,
    template_name TEXT NOT NULL DEFAULT '',
    subject_tpl   TEXT NOT NULL DEFAULT '',
    body_tpl      TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'completed_with_errors', 'interrupted')),
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL UNIQUE,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    position     INTEGER NOT NULL,
    client_id    INTEGER,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    email_key    TEXT NOT NULL,
    amount_cents INTEGER CHECK (amount_cents IS NULL OR amount_cents >= 0),
    fields       TEXT NOT NULL DEFAULT '{}',
    period       TEXT NOT NULL,
    comm_type    TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'accepted', 'error', 'uncertain')),
    attempted_at TEXT,
    error        TEXT,
    resolution   TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    comm_type   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    email_key   TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_templates_name ON templates (name_key) WHERE archived = 0;
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_recipients_key ON recipients (email_key, period, comm_type);
CREATE INDEX IF NOT EXISTS idx_recipients_batch ON recipients (batch_id);
"""

RECIPIENT_COLUMNS = ("id, uid, batch_id, position, name, email, email_key, amount_cents, period, "
                     "comm_type, status, attempted_at, error, resolution, updated_at")


class ConflictError(Exception):
    """Operación rechazada por el estado de los datos (mensaje apto para el usuario)."""


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
            had_clients = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clients'").fetchone()
            had_batches = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'").fetchone()
            if had_batches:
                self._migrate_batches(conn)
            conn.executescript(SCHEMA)
            self._migrate_recipients(conn)
            conn.executescript(INDEXES)
            self._seed(conn, import_clients=not had_clients)
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _migrate_batches(conn):
        # Bases creadas con versiones anteriores: columnas nuevas de batches.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
        if "sender" not in cols:
            conn.execute("ALTER TABLE batches ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
        if "template_id" not in cols:
            conn.execute("ALTER TABLE batches ADD COLUMN template_id INTEGER")
            conn.execute("ALTER TABLE batches ADD COLUMN template_name TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE batches ADD COLUMN subject_tpl TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE batches ADD COLUMN body_tpl TEXT NOT NULL DEFAULT ''")
            # Hasta ahora solo existía la plantilla de cuotas.
            conn.execute("UPDATE batches SET template_name=?, subject_tpl=?, body_tpl=?",
                         (core.DEFAULT_TEMPLATE_NAME, core.SUBJECT_TEMPLATE, core.BODY_TEMPLATE))

    @staticmethod
    def _migrate_recipients(conn):
        # El importe pasa a ser opcional (no todas las plantillas lo usan) y se
        # guardan los valores de los campos. SQLite no permite cambiar una
        # restricción, así que se reconstruye la tabla conservando los datos.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipients)")}
        if "fields" in cols:
            return
        create = SCHEMA.split("CREATE TABLE IF NOT EXISTS recipients", 1)[1].split(";", 1)[0]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE recipients_new" + create)
            conn.execute(f"INSERT INTO recipients_new ({RECIPIENT_COLUMNS}) "
                         f"SELECT {RECIPIENT_COLUMNS} FROM recipients")
            for row in conn.execute("SELECT id, amount_cents FROM recipients_new").fetchall():
                conn.execute("UPDATE recipients_new SET fields=? WHERE id=?",
                             (json.dumps({core.AMOUNT_FIELD: core.format_amount(row["amount_cents"])}),
                              row["id"]))
            conn.execute("DROP TABLE recipients")
            conn.execute("ALTER TABLE recipients_new RENAME TO recipients")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _seed(conn, import_clients):
        ts = now()
        if not conn.execute("SELECT 1 FROM templates WHERE comm_type=?", (core.COMM_TYPE_CUOTA,)).fetchone():
            conn.execute(
                "INSERT INTO templates (comm_type, name, name_key, subject, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (core.COMM_TYPE_CUOTA, core.DEFAULT_TEMPLATE_NAME, core.DEFAULT_TEMPLATE_NAME.lower(),
                 core.SUBJECT_TEMPLATE, core.BODY_TEMPLATE, ts, ts))
        if import_clients:
            # Primera vez con la lista de clientes: se crean a partir de los
            # destinatarios ya registrados (el nombre más reciente de cada correo).
            conn.execute(
                "INSERT OR IGNORE INTO clients (name, email, email_key, created_at, updated_at) "
                "SELECT name, email, email_key, ?, ? FROM recipients "
                "WHERE id IN (SELECT MAX(id) FROM recipients GROUP BY email_key) ORDER BY id", (ts, ts))

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
        return backup_file(self.path, backup_dir, keep)

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

    def create_batch(self, period_key, comm_type, mailer_name, clients, resend_ack=(), sender="",
                     template=None):
        """Crea la tanda y sus destinatarios en una única transacción.

        template: dict con id, name, subject y body. Se guarda una copia para
        que los correos se generen con el texto que se confirmó aunque la
        plantilla se edite después.

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
                tpl = template or {"id": None, "name": core.DEFAULT_TEMPLATE_NAME,
                                   "subject": core.SUBJECT_TEMPLATE, "body": core.BODY_TEMPLATE}
                cur = conn.execute(
                    "INSERT INTO batches (uid, period, comm_type, mailer, sender, template_id, template_name, "
                    "subject_tpl, body_tpl, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)",
                    (str(uuid.uuid4()), period_key, comm_type, mailer_name, sender, tpl["id"], tpl["name"],
                     tpl["subject"], tpl["body"], ts))
                batch_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO recipients (uid, batch_id, position, client_id, name, email, email_key, "
                    "amount_cents, fields, period, comm_type, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    [(str(uuid.uuid4()), batch_id, i, c.client_id, c.name, c.email, c.email_key,
                      c.amount_cents, json.dumps(c.fields, ensure_ascii=False), period_key, comm_type, ts)
                     for i, c in enumerate(clients)])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return batch_id

    def pending_recipients(self, batch_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT r.*, b.subject_tpl, b.body_tpl FROM recipients r JOIN batches b ON b.id = r.batch_id "
                "WHERE r.batch_id=? AND r.status='pending' ORDER BY r.position",
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
               "r.attempted_at, r.error, r.resolution, b.created_at AS batch_created_at, b.sender, "
               "b.template_name "
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

    # --- Plantillas ----------------------------------------------------------

    def list_templates(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, comm_type, name, subject, body, updated_at FROM templates "
                "WHERE archived=0 ORDER BY name_key").fetchall()]

    def get_template(self, template_id):
        """Plantilla activa (no eliminada) o None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, comm_type, name, subject, body, updated_at FROM templates WHERE id=? AND archived=0",
                (template_id,)).fetchone()
        return dict(row) if row else None

    def save_template(self, template_id, name, subject, body):
        """Crea (template_id=None) o modifica una plantilla. Devuelve su id."""
        ts = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                clash = conn.execute(
                    "SELECT id FROM templates WHERE name_key=? AND archived=0 AND id IS NOT ?",
                    (name.lower(), template_id)).fetchone()
                if clash:
                    raise ConflictError(f"Ya existe una plantilla llamada «{name}».")
                if template_id is None:
                    template_id = conn.execute(
                        "INSERT INTO templates (comm_type, name, name_key, subject, body, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (f"tpl-{uuid.uuid4()}", name, name.lower(), subject, body, ts, ts)).lastrowid
                else:
                    n = conn.execute(
                        "UPDATE templates SET name=?, name_key=?, subject=?, body=?, updated_at=? "
                        "WHERE id=? AND archived=0",
                        (name, name.lower(), subject, body, ts, template_id)).rowcount
                    if n != 1:
                        raise ConflictError("La plantilla no existe o se ha eliminado.")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return template_id

    def delete_template(self, template_id):
        """Se marca como eliminada: el historial sigue mostrando su nombre."""
        with self.connect() as conn:
            n = conn.execute("UPDATE templates SET archived=1, updated_at=? WHERE id=? AND archived=0",
                             (now(), template_id)).rowcount
        return n == 1

    # --- Clientes -------------------------------------------------------------

    def list_clients(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, email FROM clients ORDER BY name COLLATE NOCASE, id").fetchall()]

    def get_clients(self, ids):
        """{id: cliente} de los ids indicados que existan."""
        ids = sorted(set(ids))
        out = {}
        with self.connect() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(f"SELECT id, name, email FROM clients WHERE id IN ({marks})", chunk):
                    out[r["id"]] = dict(r)
        return out

    def save_client(self, client_id, name, email):
        """Crea (client_id=None) o modifica un cliente. Devuelve su id."""
        ts = now()
        key = core.email_key(email)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                clash = conn.execute("SELECT name FROM clients WHERE email_key=? AND id IS NOT ?",
                                     (key, client_id)).fetchone()
                if clash:
                    raise ConflictError(f"Ya existe un cliente con el correo {email} ({clash['name']}).")
                if client_id is None:
                    client_id = conn.execute(
                        "INSERT INTO clients (name, email, email_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (name, email, key, ts, ts)).lastrowid
                else:
                    n = conn.execute("UPDATE clients SET name=?, email=?, email_key=?, updated_at=? WHERE id=?",
                                     (name, email, key, ts, client_id)).rowcount
                    if n != 1:
                        raise ConflictError("El cliente no existe o se ha eliminado.")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return client_id

    def delete_client(self, client_id):
        """Elimina el cliente de la lista. El historial de envíos se conserva."""
        with self.connect() as conn:
            n = conn.execute("DELETE FROM clients WHERE id=?", (client_id,)).rowcount
        return n == 1


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


def backup_file(path, backup_dir, keep=30):
    """Copia de seguridad consistente (API de backup de SQLite).

    Al arrancar se hace antes de abrir la base, para conservar una copia
    previa a cualquier migración de versión.
    """
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(path).stem
    target = backup_dir / f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(backup_dir.glob(f"{stem}-*.db"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
    return target
