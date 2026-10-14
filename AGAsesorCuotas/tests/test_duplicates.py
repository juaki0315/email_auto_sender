from core import Client
from database import DuplicateError

import pytest

ANA = {"name": "Ana", "email": "ana@ejemplo.com", "amount": "45,50"}
BEN = {"name": "Benito", "email": "benito@ejemplo.com", "amount": "20"}


def send(client, clients, month=10, year=2026, **extra):
    return client.send(clients, month=month, year=year, **extra)


def statuses(client, batch_id):
    return [r["status"] for r in client.get(f"/api/batches/{batch_id}").get_json()["recipients"]]


def test_second_send_same_period_is_blocked(client, mailer):
    r = send(client, [ANA, BEN])
    assert r.status_code == 202
    assert statuses(client, r.get_json()["batch_id"]) == ["accepted", "accepted"]

    prev = client.preview([ANA]).get_json()
    assert prev["conflicts"][0]["status"] == "accepted"
    assert prev["conflicts"][0]["resendable"] is True

    r2 = send(client, [ANA])
    assert r2.status_code == 409
    assert len(mailer.sent) == 2


def test_other_period_is_allowed(client, mailer):
    assert send(client, [ANA]).status_code == 202
    assert send(client, [ANA], month=11).status_code == 202
    assert len(mailer.sent) == 2


def test_explicit_resend(client, mailer):
    send(client, [ANA])
    r = send(client, [ANA], resend_ack=["ana@ejemplo.com"])
    assert r.status_code == 202
    assert len(mailer.sent) == 2


def test_requires_confirmation(client, mailer):
    r = client.send([ANA], confirm=False)
    assert r.status_code == 400
    assert mailer.sent == []


def test_invalid_rows_block_send(client, mailer):
    r = send(client, [ANA, {"name": "X", "email": "x@ejemplo.com", "amount": "1,234"}])
    assert r.status_code == 400
    assert mailer.sent == []


def test_error_vs_uncertain(client, mailer):
    clients = [
        {"name": "Error", "email": "x+error@ejemplo.com", "amount": "1"},
        {"name": "Incierto", "email": "y+incierto@ejemplo.com", "amount": "1"},
        ANA,
    ]
    r = send(client, clients)
    bid = r.get_json()["batch_id"]
    assert statuses(client, bid) == ["error", "uncertain", "accepted"]
    assert client.get(f"/api/batches/{bid}").get_json()["status"] == "completed_with_errors"

    # Un error confirmado se puede reintentar.
    assert send(client, [clients[0]]).status_code == 202
    # Un incierto no se puede reenviar, ni siquiera con confirmación de reenvío.
    r = send(client, [clients[1]], resend_ack=["y+incierto@ejemplo.com"])
    assert r.status_code == 409


def test_resolve_uncertain(client, db):
    inc = {"name": "Incierto", "email": "y+incierto@ejemplo.com", "amount": "1"}
    send(client, [inc])
    uid = client.get("/api/history?status=uncertain").get_json()["rows"][0]["uid"]

    r = client.post_json(f"/api/recipients/{uid}/resolve", {"sent": False})
    assert r.status_code == 200
    # Resuelto como "no enviado" -> ya se puede incluir en otra tanda.
    assert send(client, [{**inc, "email": "y@ejemplo.com"}]).status_code == 202
    assert db.count_uncertain() == 0
    # No se puede resolver dos veces.
    assert client.post_json(f"/api/recipients/{uid}/resolve", {"sent": True}).status_code == 409


def test_outlook_down_midway_stops_batch(client, mailer):
    clients = [ANA, {"name": "Caído", "email": "z+caido@ejemplo.com", "amount": "1"}, BEN]
    bid = send(client, clients).get_json()["batch_id"]
    assert statuses(client, bid) == ["accepted", "error", "error"]
    assert len(mailer.sent) == 1


def test_recovery_after_interruption(db):
    clients = [Client("Ana", "ana@ejemplo.com", 100), Client("Ben", "ben@ejemplo.com", 100),
               Client("Cris", "cris@ejemplo.com", 100)]
    bid = db.create_batch("2026-10", "cuota", "simulado", clients)
    rs = db.pending_recipients(bid)
    db.mark_sending(rs[0]["id"])
    db.mark_result(rs[0]["id"], "accepted")
    db.mark_sending(rs[1]["id"])  # la app "se cierra" aquí

    assert db.recover_interrupted() == 2
    b = db.get_batch(bid)
    assert b["status"] == "interrupted"
    assert [r["status"] for r in b["recipients"]] == ["accepted", "uncertain", "error"]

    with pytest.raises(DuplicateError) as e:
        db.create_batch("2026-10", "cuota", "simulado", clients[1:2])
    assert e.value.conflicts[0]["status"] == "uncertain"
    # El que nunca llegó a Outlook sí puede reenviarse.
    db.create_batch("2026-10", "cuota", "simulado", clients[2:3])


def test_pending_in_other_batch_blocks(db):
    c = [Client("Ana", "ana@ejemplo.com", 100)]
    db.create_batch("2026-10", "cuota", "simulado", c)
    with pytest.raises(DuplicateError):
        db.create_batch("2026-10", "cuota", "simulado", c, resend_ack=["ana@ejemplo.com"])
