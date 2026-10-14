"""Interfaz común para los módulos de envío (Outlook real o simulado)."""

from contextlib import contextmanager

NO_SENDER_MSG = "No hay ninguna cuenta de envío seleccionada. Elíjala en la pestaña Configuración."


class MailerUnavailable(Exception):
    """Outlook no está disponible o mal configurado. Mensaje apto para el usuario."""


class SendFailed(Exception):
    """Fallo al enviar un mensaje concreto.

    uncertain=False -> error confirmado antes de entregar el mensaje a Outlook.
    uncertain=True  -> el fallo ocurrió durante la entrega; pudo haberse enviado.
    """

    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.message = message
        self.uncertain = uncertain


class BaseMailer:
    name = "base"
    is_simulation = False

    def __init__(self, sender=""):
        self.sender = ""
        self.set_sender(sender or "")

    def set_sender(self, address: str):
        """Cuenta de envío seleccionada (se usa en las tandas nuevas)."""
        self.sender = address.strip().lower()

    def _resolve_sender(self, sender=None) -> str:
        sender = (sender or self.sender).strip().lower()
        if not sender:
            raise MailerUnavailable(NO_SENDER_MSG)
        return sender

    def list_accounts(self) -> list:
        """Direcciones de las cuentas disponibles para enviar."""
        raise NotImplementedError

    def check(self, sender=None) -> dict:
        """Comprueba que se puede enviar con la cuenta indicada (o la seleccionada)."""
        with self.session(sender) as s:
            return {"account": s.account_address, "simulation": self.is_simulation}

    @contextmanager
    def session(self, sender=None):
        """Contexto de envío para una tanda; produce un objeto con send()."""
        raise NotImplementedError
        yield


def create_mailer(simulate: bool, sender: str) -> BaseMailer:
    if simulate:
        from mock_sender import MockMailer
        return MockMailer(sender)
    from outlook_sender import OutlookMailer
    return OutlookMailer(sender)
