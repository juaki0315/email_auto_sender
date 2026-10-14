"""Lógica de negocio: validación, importes, periodos y plantilla de correo.

No depende de Flask, de la base de datos ni de Outlook, para poder probarse
en cualquier plataforma.
"""

import re
import unicodedata
from dataclasses import dataclass

COMM_TYPE_CUOTA = "cuota"

MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

MIN_YEAR = 2000
MAX_YEAR = 2100
MAX_NAME_LEN = 200
MAX_EMAIL_LEN = 254
MAX_CLIENTS = 1000
# 10 millones de euros: límite de cordura para detectar errores de tecleo.
MAX_AMOUNT_CENTS = 10_000_000_00

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

# "1234", "1234,5", "1234.56"  -> separador decimal coma o punto
_AMOUNT_PLAIN_RE = re.compile(r"^(\d+)(?:[.,](\d{1,2}))?$")
# "1.234", "1.234.567,89"     -> puntos como separador de miles
_AMOUNT_GROUPED_RE = re.compile(r"^(\d{1,3}(?:\.\d{3})+)(?:,(\d{1,2}))?$")


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    year: int
    month: int

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{MONTHS[self.month - 1]} de {self.year}"

    @classmethod
    def from_key(cls, key: str) -> "Period":
        year, month = key.split("-")
        return parse_period(month, year)


@dataclass(frozen=True)
class Client:
    name: str
    email: str
    amount_cents: int

    @property
    def email_key(self) -> str:
        return email_key(self.email)


def email_key(email: str) -> str:
    """Identificador lógico del destinatario (el correo normalizado)."""
    return email.strip().lower()


def parse_period(month, year) -> Period:
    try:
        m = int(str(month).strip())
        y = int(str(year).strip())
    except (TypeError, ValueError):
        raise ValidationError("Periodo no válido.")
    if not 1 <= m <= 12:
        raise ValidationError("Mes no válido.")
    if not MIN_YEAR <= y <= MAX_YEAR:
        raise ValidationError(f"El año debe estar entre {MIN_YEAR} y {MAX_YEAR}.")
    return Period(y, m)


def parse_amount(raw) -> int:
    """Convierte un importe escrito por el usuario a céntimos enteros.

    Acepta coma o punto decimal y puntos de miles ("1.234,56"). Nunca usa
    coma flotante y rechaza más de dos decimales en lugar de redondear.
    """
    if isinstance(raw, bool) or raw is None:
        raise ValidationError("El importe es obligatorio.")
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str):
        raise ValidationError("El importe debe escribirse como texto, p. ej. 45,50.")
    s = raw.strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not s:
        raise ValidationError("El importe es obligatorio.")
    if s.startswith("-"):
        raise ValidationError("El importe no puede ser negativo.")

    m = _AMOUNT_GROUPED_RE.match(s)
    if m:
        units = m.group(1).replace(".", "")
    else:
        m = _AMOUNT_PLAIN_RE.match(s)
        if not m:
            raise ValidationError("Importe no válido (ej.: 45,50; máx. 2 decimales).")
        units = m.group(1)
    decimals = (m.group(2) or "").ljust(2, "0")
    cents = int(units) * 100 + int(decimals)
    if cents > MAX_AMOUNT_CENTS:
        raise ValidationError("El importe es demasiado alto. Revise si es correcto.")
    return cents


def format_amount(cents: int) -> str:
    """12345 -> '123,45'; 123456 -> '1.234,56'."""
    if cents < 0:
        raise ValueError("Importe negativo")
    units, dec = divmod(cents, 100)
    units_str = f"{units:,}".replace(",", ".")
    return f"{units_str},{dec:02d}"


def normalize_name(raw) -> str:
    if not isinstance(raw, str):
        raise ValidationError("El nombre es obligatorio.")
    name = " ".join(raw.split())
    if not name:
        raise ValidationError("El nombre es obligatorio.")
    if len(name) > MAX_NAME_LEN:
        raise ValidationError(f"El nombre no puede superar {MAX_NAME_LEN} caracteres.")
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in name):
        raise ValidationError("El nombre contiene caracteres no permitidos.")
    return name


def normalize_email(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("El correo es obligatorio.")
    email = raw.strip()
    if len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        raise ValidationError("El correo no tiene un formato válido.")
    return email


def validate_clients(rows):
    """Valida la lista de filas recibida.

    Devuelve (clientes_validos, errores) donde errores es un dict
    {indice_fila: {campo: mensaje}}. Solo hay clientes válidos utilizables si
    errores está vacío.
    """
    if not isinstance(rows, list):
        raise ValidationError("Lista de clientes no válida.")
    if len(rows) > MAX_CLIENTS:
        raise ValidationError(f"No se pueden enviar más de {MAX_CLIENTS} clientes en una tanda.")

    clients, errors, seen = [], {}, {}
    for i, row in enumerate(rows):
        row = row if isinstance(row, dict) else {}
        row_errors, values = {}, {}
        for field, fn in (("name", normalize_name), ("email", normalize_email), ("amount", parse_amount)):
            try:
                values[field] = fn(row.get(field))
            except ValidationError as e:
                row_errors[field] = str(e)
        if "email" in values:
            key = email_key(values["email"])
            if key in seen:
                row_errors["email"] = f"Correo repetido (fila {seen[key] + 1})."
            else:
                seen[key] = i
        if row_errors:
            errors[i] = row_errors
        else:
            clients.append(Client(values["name"], values["email"], values["amount"]))
    if not rows:
        raise ValidationError("Añada al menos un cliente.")
    return clients, errors


# --- Plantilla -------------------------------------------------------------
# Se puede modificar libremente sin afectar al resto de la aplicación.

SUBJECT_TEMPLATE = "Cuota correspondiente a {periodo}"

BODY_TEMPLATE = (
    "Estimado/a {nombre}:\n"
    "\n"
    "Le informamos de que la cuota correspondiente al periodo de {periodo} "
    "asciende a {importe} €.\n"
    "\n"
    "Para cualquier consulta o aclaración, puede ponerse en contacto con nosotros.\n"
    "\n"
    "Atentamente,\n"
    "\n"
    "AG Asesor\n"
)


def render_email(name: str, period: Period, amount_cents: int):
    """Devuelve (asunto, cuerpo) para un único destinatario."""
    values = {
        "nombre": name,
        "periodo": period.label,
        "importe": format_amount(amount_cents),
    }
    return SUBJECT_TEMPLATE.format(**values), BODY_TEMPLATE.format(**values)
