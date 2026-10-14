from conftest import TOKEN

BODY = {"month": 10, "year": 2026, "clients": [{"name": "Ana", "email": "ana@ejemplo.com", "amount": "1"}]}


def test_post_without_token_rejected(client):
    assert client.post("/api/preview", json=BODY).status_code == 403
    assert client.post("/api/preview", json=BODY, headers={"X-CSRF-Token": "otro"}).status_code == 403


def test_post_from_other_origin_rejected(client):
    r = client.post("/api/preview", json=BODY, headers={"X-CSRF-Token": TOKEN, "Origin": "http://malicioso.example"})
    assert r.status_code == 403


def test_form_post_rejected(client):
    r = client.post("/api/send", data={"x": "1"}, headers={"X-CSRF-Token": TOKEN})
    assert r.status_code == 415


def test_foreign_host_rejected(client):
    # Protección frente a DNS rebinding.
    assert client.get("/api/status", headers={"Host": "atacante.example"}).status_code == 400


def test_index_embeds_token_and_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert TOKEN in r.get_data(as_text=True)
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "MODO SIMULADO" in r.get_data(as_text=True)


def test_preview_ok(client):
    r = client.post_json("/api/preview", BODY)
    d = r.get_json()
    assert d["valid"] and d["summary"]["total"] == "1,00"
    assert d["sample"]["subject"] == "Cuota correspondiente a octubre de 2026"
