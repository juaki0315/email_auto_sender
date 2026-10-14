from core import Period, render_email


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


def test_each_client_gets_only_own_data(client, mailer):
    clients = [
        {"name": "Ana", "email": "ana@ejemplo.com", "amount": "10,00"},
        {"name": "Benito", "email": "benito@ejemplo.com", "amount": "20,00"},
    ]
    r = client.post_json("/api/send", {"month": 10, "year": 2026, "clients": clients, "confirm": True})
    assert r.status_code == 202
    assert len(mailer.sent) == 2
    (from1, to1, _, body1), (_, to2, _, body2) = mailer.sent
    assert from1 == "joaquin@agasesor.com"
    assert to1 == "ana@ejemplo.com" and "Ana" in body1 and "10,00" in body1
    assert "Benito" not in body1 and "20,00" not in body1
    assert to2 == "benito@ejemplo.com" and "Benito" in body2 and "Ana" not in body2
