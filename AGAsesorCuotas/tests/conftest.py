import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from database import Database  # noqa: E402
from mock_sender import MockMailer  # noqa: E402

TOKEN = "token-de-prueba"


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def mailer():
    return MockMailer("joaquin@agasesor.com", delay=0)


def add_helpers(c):
    """Atajos para las pruebas: peticiones con token y envío por nombre/correo."""
    headers = {"X-CSRF-Token": TOKEN}
    c.post_json = lambda url, json: c.post(url, json=json, headers=headers)
    c.put_json = lambda url, json: c.put(url, json=json, headers=headers)
    c.delete_json = lambda url: c.delete(url, json={}, headers=headers)

    def template_id(name="Cuota mensual"):
        return next(t["id"] for t in c.get("/api/templates").get_json()["templates"] if t["name"] == name)

    def client_id(person):
        """Id del cliente con ese correo; lo crea si no existe."""
        for saved in c.get("/api/clients").get_json()["clients"]:
            if saved["email"].lower() == person["email"].lower():
                return saved["id"]
        r = c.post_json("/api/clients", {"name": person["name"], "email": person["email"]})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["client"]["id"]

    def payload(people, month=10, year=2026, template=None, **extra):
        """people: dicts con name, email y los campos de la plantilla (p. ej. amount -> importe)."""
        tid = template if isinstance(template, int) else template_id(template or "Cuota mensual")
        recipients = []
        for p in people:
            fields = {k: v for k, v in p.items() if k not in ("name", "email", "amount")}
            if "amount" in p:
                fields["importe"] = p["amount"]
            recipients.append({"client_id": client_id(p), "fields": fields})
        return {"month": month, "year": year, "template_id": tid, "recipients": recipients, **extra}

    def preview(people, **kw):
        return c.post_json("/api/preview", payload(people, **kw))

    def send(people, confirm=True, **kw):
        body = payload(people, **kw)
        prev = c.post_json("/api/preview", body).get_json()
        if confirm:
            body["confirm"] = True
        body.setdefault("template_updated_at", prev.get("summary", {}).get("template_updated_at"))
        return c.post_json("/api/send", body)

    c.template_id, c.client_id, c.payload, c.preview, c.send = template_id, client_id, payload, preview, send
    return c


@pytest.fixture
def client(db, mailer):
    app = create_app(mailer, db, synchronous=True, csrf_token=TOKEN)
    app.config["TESTING"] = True
    return add_helpers(app.test_client())
