"""Lógica de negocio: validación, importes, periodos y plantillas de correo.

No depende de Flask, de la base de datos ni de Outlook, para poder probarse
en cualquier plataforma.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# Tipo de comunicación de la plantilla de cuotas original. Se conserva para que
# el control de duplicados siga reconociendo los envíos anteriores.
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

MAX_TEMPLATE_NAME_LEN = 100
MAX_SUBJECT_LEN = 255
MAX_BODY_LEN = 20_000
MAX_FIELDS = 15
MAX_FIELD_NAME_LEN = 40
MAX_FIELD_VALUE_LEN = 500

# Campos que se rellenan solos: el nombre del cliente y el periodo de la tanda.
BUILTIN_FIELDS = ("nombre", "periodo")
# Campo con tratamiento especial: se valida como importe y se suma en el total.
AMOUNT_FIELD = "importe"

_LETTERS = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
PLACEHOLDER_RE = re.compile(r"\{([" + _LETTERS + "_][" + _LETTERS + r"0-9_]*)\}")

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
    """Destinatario de una tanda con los valores de los campos de la plantilla.

    fields contiene los valores ya formateados tal y como aparecerán en el
    correo (p. ej. {"importe": "1.234,56", "fecha_limite": "15 de mayo"}).
    """
    name: str
    email: str
    amount_cents: Optional[int] = None
    fields: dict = field(default_factory=dict)
    client_id: Optional[int] = None

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


def _check_text(value, label, max_len, multiline=False):
    if not isinstance(value, str):
        raise ValidationError(f"{label} es obligatorio.")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.strip() if not multiline else value.strip("\n ")
    if not value:
        raise ValidationError(f"{label} es obligatorio.")
    if len(value) > max_len:
        raise ValidationError(f"{label} no puede superar {max_len} caracteres.")
    allowed = ("\n", "\t") if multiline else ()
    if any(unicodedata.category(c) in ("Cc", "Cf") and c not in allowed for c in value):
        raise ValidationError(f"{label} contiene caracteres no permitidos.")
    return value


# --- Plantillas ------------------------------------------------------------

def template_fields(subject: str, body: str) -> list:
    """Campos que hay que rellenar al enviar, en orden de aparición.

    Excluye los automáticos ({nombre}, {periodo}). Los nombres no distinguen
    mayúsculas: {Importe} e {importe} son el mismo campo.
    """
    out = []
    for text in (subject, body):
        for m in PLACEHOLDER_RE.finditer(text or ""):
            name = m.group(1).lower()
            if name not in BUILTIN_FIELDS and name not in out:
                out.append(name)
    return out


def field_label(name: str) -> str:
    """'fecha_limite' -> 'Fecha limite'."""
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def validate_template(data) -> dict:
    """Valida nombre, asunto y cuerpo de una plantilla. Devuelve los valores normalizados."""
    if not isinstance(data, dict):
        raise ValidationError("Datos de la plantilla no válidos.")
    name = " ".join(_check_text(data.get("name"), "El nombre de la plantilla", MAX_TEMPLATE_NAME_LEN).split())
    subject = _check_text(data.get("subject"), "El asunto", MAX_SUBJECT_LEN)
    body = _check_text(data.get("body"), "El cuerpo del correo", MAX_BODY_LEN, multiline=True)
    fields = template_fields(subject, body)
    if len(fields) > MAX_FIELDS:
        raise ValidationError(f"Una plantilla no puede tener más de {MAX_FIELDS} campos.")
    too_long = [f for f in fields if len(f) > MAX_FIELD_NAME_LEN]
    if too_long:
        raise ValidationError(f"El nombre del campo {{{too_long[0]}}} es demasiado largo.")
    return {"name": name, "subject": subject, "body": body}


def validate_fields(fields, raw):
    """Valida los valores escritos para los campos de la plantilla.

    Devuelve (valores_formateados, importe_en_centimos_o_None, errores).
    """
    raw = raw if isinstance(raw, dict) else {}
    values, errors, amount = {}, {}, None
    for name in fields:
        value = raw.get(name)
        if name == AMOUNT_FIELD:
            try:
                amount = parse_amount(value)
                values[name] = format_amount(amount)
            except ValidationError as e:
                errors[name] = str(e)
            continue
        try:
            values[name] = " ".join(_check_text(value, "Este campo", MAX_FIELD_VALUE_LEN).split())
        except ValidationError as e:
            errors[name] = str(e)
    return values, amount, errors


def render_template(subject: str, body: str, values: dict):
    """Devuelve (asunto, cuerpo) sustituyendo los campos por sus valores.

    Los valores se insertan tal cual: si contienen llaves no se vuelven a
    interpretar. Un campo sin valor se deja como está.
    """
    def sub(m):
        return values.get(m.group(1).lower(), m.group(0))
    return PLACEHOLDER_RE.sub(sub, subject), PLACEHOLDER_RE.sub(sub, body)


# Plantilla inicial (la de cuotas). Se crea la primera vez que se abre la aplicación.

DEFAULT_TEMPLATE_NAME = "Cuota mensual"

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
    """Correo de la plantilla de cuotas inicial para un único destinatario."""
    values = {"nombre": name, "periodo": period.label, "importe": format_amount(amount_cents)}
    return render_template(SUBJECT_TEMPLATE, BODY_TEMPLATE, values)
