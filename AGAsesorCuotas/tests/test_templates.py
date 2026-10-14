import json
import sqlite3

import pytest

from core import (Period, ValidationError, render_email, render_template, template_fields,
                  validate_template)
from database import Database

AVISO = {
    "name": "Aviso de documentación",
    "subject": "Documentación pendiente: {documento}",
    "body": "Hola {nombre}:\n\nNecesitamos {documento} antes del {fecha_limite}.\n\nAG Asesor",
}


# --- core ---------------------------------------------------------------------

def test_render_email():
    subject, body = render_email("Ana López", Period(2026, 10), 4550)
    assert subject == "Cuota correspondiente a octubre de 2026"
    assert body.startswith("Estimado/a Ana López:\n")
    assert "periodo de octubre de 2026 asciende a 45,50 €." in body
    assert body.rstrip().endswith("AG Asesor")


def test_render_email_thousands_and_braces():
    # Un nombre con llaves no debe interpretarse como plantilla.
    subject, body = render_email("Empresa {importe} S.L.", Period(2026, 1), 123456)
    assert "Empresa {importe} S.L." in body
    assert "1.234,56 €" in body
    assert subject == "Cuota correspondiente a enero de 2026"


def test_template_fields_order_and_case():
    fields = template_fields("Aviso {Documento}", "Hola {nombre}, {documento} y {importe} en {periodo}. {x")
    assert fields == ["documento", "importe"]


def test_render_template_keeps_unknown_and_literal_braces():
    s, b = render_template("{a} {b}", "{A} {no existe} {}", {"a": "1"})
    assert s == "1 {b}" and b == "1 {no existe} {}"


def test_validate_template():
    t = validate_template({"name": "  Aviso   general ", "subject": " Hola ", "body": "\r\nLínea 1\r\nLínea 2\n"})
    assert t == {"name": "Aviso general", "subject": "Hola", "body": "Línea 1\nLínea 2"}
    for bad in [{}, {**AVISO, "name": ""}, {**AVISO, "subject": "a\nb"}, {**AVISO, "body": "  "},
                {**AVISO, "body": " ".join("{c%d}" % i for i in range(16))}, "x"]:
        with pytest.raises(ValidationError):
            validate_template(bad)


# --- API de plantillas ----------------------------------------------------------

def test_default_template_exists(client):
    d = client.get("/api/templates").get_json()
    [t] = d["templates"]
    assert t["name"] == "Cuota mensual"
    assert t["fields"] == [{"name": "importe", "label": "Importe", "amount": True}]
    assert d["builtin"] == ["nombre", "periodo"]


def test_create_edit_delete_template(client):
    r = client.post_json("/api/templates", AVISO)
    assert r.status_code == 201
    t = r.get_json()["template"]
    assert [f["name"] for f in t["fields"]] == ["documento", "fecha_limite"]
    assert t["fields"][1]["label"] == "Fecha limite"

    # Nombre repetido (sin distinguir mayúsculas).
    assert client.post_json("/api/templates", {**AVISO, "name": "aviso DE documentación"}).status_code == 409

    r = client.put_json(f"/api/templates/{t['id']}", {**AVISO, "body": "Hola {nombre}, falta {documento}."})
    assert r.status_code == 200
    assert [f["name"] for f in r.get_json()["template"]["fields"]] == ["documento"]

    assert client.delete_json(f"/api/templates/{t['id']}").status_code == 200
    assert client.delete_json(f"/api/templates/{t['id']}").status_code == 404
    assert [x["name"] for x in client.get("/api/templates").get_json()["templates"]] == ["Cuota mensual"]
    # Tras eliminarla se puede crear otra con el mismo nombre.
    assert client.post_json("/api/templates", AVISO).status_code == 201


def test_invalid_template_rejected(client):
    r = client.post_json("/api/templates", {**AVISO, "subject": ""})
    assert r.status_code == 400 and "asunto" in r.get_json()["error"]


# --- Envío con plantillas propias -------------------------------------------

def test_each_client_gets_only_own_data(client, mailer):
    clients = [
        {"name": "Ana", "email": "ana@ejemplo.com", "amount": "10,00"},
        {"name": "Benito", "email": "benito@ejemplo.com", "amount": "20,00"},
    ]
    r = client.send(clients)
    assert r.status_code == 202
    assert len(mailer.sent) == 2
    (from1, to1, _, body1), (_, to2, _, body2) = mailer.sent
    assert from1 == "joaquin@agasesor.com"
    assert to1 == "ana@ejemplo.com" and "Ana" in body1 and "10,00" in body1
    assert "Benito" not in body1 and "20,00" not in body1
    assert to2 == "benito@ejemplo.com" and "Benito" in body2 and "Ana" not in body2


def test_send_with_custom_template(client, mailer):
    tid = client.post_json("/api/templates", AVISO).get_json()["template"]["id"]
    ana = {"name": "Ana", "email": "ana@ejemplo.com", "documento": "el modelo 303", "fecha_limite": "20 de octubre"}

    prev = client.preview([ana], template=tid).get_json()
    assert prev["valid"]
    assert prev["summary"]["has_amount"] is False and prev["summary"]["total"] is None
    assert prev["sample"]["subject"] == "Documentación pendiente: el modelo 303"
    assert prev["recipients"][0]["amount"] is None

    assert client.send([ana], template=tid).status_code == 202
    _, _, subject, body = mailer.sent[0]
    assert subject == "Documentación pendiente: el modelo 303"
    assert body == "Hola Ana:\n\nNecesitamos el modelo 303 antes del 20 de octubre.\n\nAG Asesor"

    row = client.get("/api/history").get_json()["rows"][0]
    assert row["template_name"] == "Aviso de documentación" and row["amount"] is None


def test_missing_field_values_are_reported_per_client(client, mailer):
    tid = client.post_json("/api/templates", AVISO).get_json()["template"]["id"]
    ana = {"name": "Ana", "email": "ana@ejemplo.com", "documento": "el 303", "fecha_limite": ""}
    prev = client.preview([ana], template=tid).get_json()
    cid = client.client_id(ana)
    assert prev["valid"] is False
    assert set(prev["errors"][str(cid)]) == {"fecha_limite"}
    assert client.send([ana], template=tid).status_code == 400
    assert mailer.sent == []


def test_duplicates_are_per_template(client, mailer):
    ana = {"name": "Ana", "email": "ana@ejemplo.com", "amount": "10", "documento": "x", "fecha_limite": "y"}
    tid = client.post_json("/api/templates", AVISO).get_json()["template"]["id"]
    assert client.send([ana]).status_code == 202
    # Misma persona y periodo, pero otra plantilla: no es un duplicado.
    assert client.send([ana], template=tid).status_code == 202
    assert client.send([ana], template=tid).status_code == 409
    assert len(mailer.sent) == 2


def test_template_changed_after_review_blocks_send(client, mailer):
    tid = client.post_json("/api/templates", AVISO).get_json()["template"]["id"]
    ana = {"name": "Ana", "email": "ana@ejemplo.com", "documento": "x", "fecha_limite": "y"}
    body = client.payload([ana], template=tid)
    reviewed = client.post_json("/api/preview", body).get_json()["summary"]["template_updated_at"]
    # Se simula una edición de la plantilla posterior a la revisión.
    body.update(confirm=True, template_updated_at=reviewed + "x")
    r = client.post_json("/api/send", body)
    assert r.status_code == 409 and "modificado" in r.get_json()["error"]
    assert mailer.sent == []


def test_batch_keeps_copy_of_template(db):
    from core import Client
    tid = db.save_template(None, "Aviso", "Asunto {campo}", "Cuerpo {campo}")
    tpl = db.get_template(tid)
    bid = db.create_batch("2026-10", tpl["comm_type"], "simulado",
                          [Client("Ana", "ana@ejemplo.com", None, {"campo": "uno"})], template=tpl)
    db.save_template(tid, "Aviso", "Otro asunto", "Otro cuerpo")
    [r] = db.pending_recipients(bid)
    assert (r["subject_tpl"], r["body_tpl"]) == ("Asunto {campo}", "Cuerpo {campo}")
    assert json.loads(r["fields"]) == {"campo": "uno"}


# --- Clientes ---------------------------------------------------------------------

def test_clients_crud(client):
    r = client.post_json("/api/clients", {"name": "  Ana  López ", "email": "ana@ejemplo.com"})
    assert r.status_code == 201
    cid = r.get_json()["client"]["id"]
    assert client.get("/api/clients").get_json()["clients"] == [
        {"id": cid, "name": "Ana López", "email": "ana@ejemplo.com"}]

    assert client.post_json("/api/clients", {"name": "Otra", "email": "ANA@ejemplo.com"}).status_code == 409
    assert client.post_json("/api/clients", {"name": "Mal", "email": "mal"}).status_code == 400

    r = client.put_json(f"/api/clients/{cid}", {"name": "Ana L.", "email": "ana.l@ejemplo.com"})
    assert r.status_code == 200
    assert client.get("/api/clients").get_json()["clients"][0]["email"] == "ana.l@ejemplo.com"

    assert client.delete_json(f"/api/clients/{cid}").status_code == 200
    assert client.get("/api/clients").get_json()["clients"] == []
    assert client.put_json(f"/api/clients/{cid}", {"name": "X", "email": "x@ejemplo.com"}).status_code == 409


def test_deleted_client_cannot_be_sent(client, mailer):
    ana = {"name": "Ana", "email": "ana@ejemplo.com", "amount": "10"}
    body = client.payload([ana])
    client.delete_json(f"/api/clients/{body['recipients'][0]['client_id']}")
    prev = client.post_json("/api/preview", body).get_json()
    assert prev["valid"] is False
    assert mailer.sent == []


def test_name_and_email_come_from_saved_client(client, mailer):
    ana = {"name": "Ana", "email": "ana@ejemplo.com", "amount": "10"}
    body = client.payload([ana])
    # Aunque la petición traiga otros datos, se usan los del cliente guardado.
    body["recipients"][0].update(name="Intruso", email="otro@ejemplo.com")
    assert client.send([ana]).status_code == 202
    assert mailer.sent[0][1] == "ana@ejemplo.com"


def test_selection_validation(client):
    tid = client.template_id()
    base = {"month": 10, "year": 2026, "template_id": tid}
    assert client.post_json("/api/preview", {**base, "recipients": []}).status_code == 400
    assert client.post_json("/api/preview", {**base, "recipients": [{"client_id": "1"}]}).status_code == 400
    cid = client.client_id({"name": "Ana", "email": "ana@ejemplo.com"})
    dup = [{"client_id": cid}, {"client_id": cid}]
    assert client.post_json("/api/preview", {**base, "recipients": dup}).status_code == 400
    r = client.post_json("/api/preview", {**base, "template_id": 999, "recipients": [{"client_id": cid}]})
    assert r.status_code == 400 and "plantilla" in r.get_json()["error"]


# --- Migración desde la versión anterior --------------------------------------------

OLD_SCHEMA = """
CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE, period TEXT NOT NULL,
  comm_type TEXT NOT NULL, mailer TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE,
  batch_id INTEGER NOT NULL REFERENCES batches(id), position INTEGER NOT NULL, name TEXT NOT NULL,
  email TEXT NOT NULL, email_key TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  period TEXT NOT NULL, comm_type TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT, error TEXT,
  resolution TEXT, updated_at TEXT NOT NULL);
CREATE INDEX idx_recipients_key ON recipients (email_key, period, comm_type);
INSERT INTO batches VALUES (1, 'b1', '2026-09', 'cuota', 'outlook', 'yo@agasesor.com', 'completed', '2026-09-01', NULL);
INSERT INTO batches VALUES (2, 'b2', '2026-10', 'cuota', 'outlook', 'yo@agasesor.com', 'completed', '2026-10-01', NULL);
INSERT INTO recipients VALUES (1, 'r1', 1, 0, 'Ana Antigua', 'ana@ejemplo.com', 'ana@ejemplo.com', 4550,
  '2026-09', 'cuota', 'accepted', '2026-09-01', NULL, NULL, '2026-09-01');
INSERT INTO recipients VALUES (2, 'r2', 2, 0, 'Ana López', 'Ana@Ejemplo.com', 'ana@ejemplo.com', 4600,
  '2026-10', 'cuota', 'accepted', '2026-10-01', NULL, NULL, '2026-10-01');
INSERT INTO recipients VALUES (3, 'r3', 2, 1, 'Benito', 'benito@ejemplo.com', 'benito@ejemplo.com', 100,
  '2026-10', 'cuota', 'error', '2026-10-01', 'x', NULL, '2026-10-01');
"""


def test_migration_from_previous_version(tmp_path):
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()

    db = Database(path)
    # Los clientes se importan del historial, con el nombre más reciente.
    assert [(c["name"], c["email"]) for c in db.list_clients()] == [
        ("Ana López", "Ana@Ejemplo.com"), ("Benito", "benito@ejemplo.com")]
    # Se conserva el historial y el control de duplicados de las cuotas.
    hist = db.history()
    assert [r["amount_cents"] for r in hist] == [100, 4600, 4550]
    assert hist[0]["template_name"] == "Cuota mensual"
    [tpl] = db.list_templates()
    assert tpl["comm_type"] == "cuota"
    conflicts = db.find_conflicts("2026-10", tpl["comm_type"], ["ana@ejemplo.com"])
    assert conflicts[0]["status"] == "accepted"
    # Ahora se admiten destinatarios sin importe.
    from core import Client
    db.create_batch("2026-11", "tpl-x", "simulado", [Client("Ana", "ana@ejemplo.com")])

    # Abrir de nuevo no duplica nada ni vuelve a importar clientes borrados.
    db.delete_client(db.list_clients()[1]["id"])
    db2 = Database(path)
    assert len(db2.list_templates()) == 1 and len(db2.list_clients()) == 1
