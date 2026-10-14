"""Envío SIMULADO para desarrollo y pruebas. No envía ningún correo.

Para probar los distintos resultados, la parte local del correo decide:
  ...+error@...      -> error confirmado (no llega a Outlook)
  ...+incierto@...   -> resultado incierto
  ...+caido@...      -> Outlook deja de estar disponible a mitad de tanda
"""

import time
from contextlib import contextmanager

from mailer import BaseMailer, MailerUnavailable, SendFailed

# Cuentas ficticias que "tiene configuradas" el Outlook simulado.
SIMULATED_ACCOUNTS = ["joaquin@agasesor.com", "administracion@agasesor.com"]


class MockMailer(BaseMailer):
    name = "simulado"
    is_simulation = True

    def __init__(self, sender="", delay=0.25, accounts=None):
        super().__init__(sender)
        self.delay = delay
        self.accounts = list(accounts or SIMULATED_ACCOUNTS)
        self.sent = []  # [(remitente, para, asunto, cuerpo)] solo en memoria

    def list_accounts(self):
        return list(self.accounts)

    @contextmanager
    def session(self, sender=None):
        address = self._resolve_sender(sender)
        if address not in self.accounts:
            raise MailerUnavailable(f"La cuenta {address} no está configurada en Outlook. No se enviará nada.")
        yield _MockSession(self, address)


class _MockSession:
    def __init__(self, mailer, address):
        self.mailer = mailer
        self.account_address = address

    def send(self, to, subject, body):
        if self.mailer.delay:
            time.sleep(self.mailer.delay)
        local = to.split("@", 1)[0].lower()
        if local.endswith("+error"):
            raise SendFailed("Error simulado antes de entregar a Outlook.")
        if local.endswith("+caido"):
            raise MailerUnavailable("Outlook simulado dejó de responder.")
        self.mailer.sent.append((self.account_address, to, subject, body))
        if local.endswith("+incierto"):
            raise SendFailed("Resultado simulado incierto.", uncertain=True)
