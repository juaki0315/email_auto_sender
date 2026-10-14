import pytest

from core import (Period, ValidationError, format_amount, normalize_email, parse_amount,
                  parse_period, validate_clients)


@pytest.mark.parametrize("raw, cents", [
    ("45", 4500), ("45,5", 4550), ("45,50", 4550), ("45.50", 4550), ("0", 0), ("0,01", 1),
    ("1.234", 123400), ("1.234,56", 123456), ("1234,56", 123456), (" 12,30 € ", 1230),
    ("1.234.567,89", 123456789), (120, 12000),
])
def test_parse_amount_ok(raw, cents):
    assert parse_amount(raw) == cents


@pytest.mark.parametrize("raw", [
    "", "  ", None, "-5", "abc", "12,345", "1,234.56", "1.23.4", "12,", ",5", "1e3",
    "inf", "NaN", 12.5, True, "99999999999", "12..5",
])
def test_parse_amount_rejects(raw):
    with pytest.raises(ValidationError):
        parse_amount(raw)


def test_amounts_are_exact():
    # 0,1 + 0,2 debe ser exactamente 0,30 (no 0,30000000000000004)
    assert parse_amount("0,10") + parse_amount("0,20") == 30


def test_format_amount():
    assert format_amount(0) == "0,00"
    assert format_amount(5) == "0,05"
    assert format_amount(4550) == "45,50"
    assert format_amount(123456) == "1.234,56"
    assert format_amount(123456789) == "1.234.567,89"


@pytest.mark.parametrize("email", ["cliente@ejemplo.com", "a.b+c@sub.dominio.es", "X@Y.ES"])
def test_email_ok(email):
    assert normalize_email(email) == email


@pytest.mark.parametrize("email", ["", "sin-arroba", "a@b", "a@@b.com", "a b@c.com", "a@b..com",
                                   "a@-b.com", "a@b.com;c@d.com", "a@b.com, c@d.com"])
def test_email_rejects(email):
    with pytest.raises(ValidationError):
        normalize_email(email)


def test_period():
    p = parse_period("10", 2026)
    assert p == Period(2026, 10)
    assert p.key == "2026-10"
    assert p.label == "octubre de 2026"
    assert Period.from_key("2026-01").label == "enero de 2026"
    for bad in [("0", "2026"), ("13", "2026"), ("1", "1999"), ("x", "2026"), ("1", None)]:
        with pytest.raises(ValidationError):
            parse_period(*bad)


def test_validate_clients_mixed():
    rows = [
        {"name": "  Ana   López ", "email": "ana@ejemplo.com", "amount": "45,50"},
        {"name": "", "email": "mal", "amount": "-1"},
        {"name": "Ana bis", "email": "ANA@ejemplo.com", "amount": "10"},
        "no es un dict",
    ]
    clients, errors = validate_clients(rows)
    assert [c.name for c in clients] == ["Ana López"]
    assert set(errors[1]) == {"name", "email", "amount"}
    assert "repetido" in errors[2]["email"]
    assert 3 in errors


def test_validate_clients_empty_and_control_chars():
    with pytest.raises(ValidationError):
        validate_clients([])
    with pytest.raises(ValidationError):
        validate_clients("x")
    _, errors = validate_clients([{"name": "Ana‮", "email": "a@b.es", "amount": "1"}])
    assert "name" in errors[0]
