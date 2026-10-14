from app import create_app, load_config, save_config
from conftest import TOKEN, add_helpers
from mock_sender import MockMailer

ANA = {"name": "Ana", "email": "ana@ejemplo.com", "amount": "10"}


def make_client(db, sender="", saved=None):
    mailer = MockMailer(sender, delay=0)
    app = create_app(mailer, db, synchronous=True, csrf_token=TOKEN,
                     save_sender=(saved.append if saved is not None else None))
    return add_helpers(app.test_client()), mailer


def send(c, clients=(ANA,)):
    return c.send(list(clients))


def test_without_sender_nothing_is_sent(db):
    c, mailer = make_client(db)
    st = c.get("/api/status").get_json()
    assert st["ok"] is False and st["sender"] == ""
    r = send(c)
    assert r.status_code == 503
    assert "Configuración" in r.get_json()["error"]
    assert mailer.sent == [] and db.history() == []


def test_list_and_choose_account(db):
    saved = []
    c, mailer = make_client(db, saved=saved)
    acc = c.get("/api/accounts").get_json()
    assert acc["accounts"] == ["joaquin@agasesor.com", "administracion@agasesor.com"]

    r = c.post_json("/api/settings/sender", {"sender": "Administracion@AGAsesor.com"})
    assert r.status_code == 200
    assert saved == ["administracion@agasesor.com"]
    assert c.get("/api/status").get_json()["sender"] == "administracion@agasesor.com"

    bid = send(c).get_json()["batch_id"]
    assert mailer.sent[0][0] == "administracion@agasesor.com"
    assert c.get(f"/api/batches/{bid}").get_json()["sender"] == "administracion@agasesor.com"
    assert db.history()[0]["sender"] == "administracion@agasesor.com"


def test_unknown_or_invalid_account_rejected(db):
    saved = []
    c, mailer = make_client(db, sender="joaquin@agasesor.com", saved=saved)
    assert c.post_json("/api/settings/sender", {"sender": "otra@agasesor.com"}).status_code == 400
    assert c.post_json("/api/settings/sender", {"sender": "no-es-correo"}).status_code == 400
    assert c.post_json("/api/settings/sender", {}).status_code == 400
    assert saved == [] and mailer.sender == "joaquin@agasesor.com"


def test_change_requires_csrf(db):
    c, _ = make_client(db)
    r = c.post("/api/settings/sender", json={"sender": "joaquin@agasesor.com"})
    assert r.status_code == 403


def test_saved_account_missing_from_outlook_blocks_send(db):
    c, mailer = make_client(db, sender="antigua@agasesor.com")
    r = send(c)
    assert r.status_code == 503
    assert "no está configurada" in r.get_json()["error"]
    assert mailer.sent == []


def test_config_roundtrip(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["sender"] == "" and cfg["port"] == 8765
    cfg["sender"] = "joaquin@agasesor.com"
    save_config(tmp_path, cfg)
    assert load_config(tmp_path)["sender"] == "joaquin@agasesor.com"
