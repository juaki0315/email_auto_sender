"""Envío real mediante Outlook clásico (automatización COM). Solo Windows.

Requiere pywin32 y Outlook clásico con la cuenta corporativa configurada.
El nuevo Outlook ("new Outlook") no admite automatización COM.
"""

import logging
from contextlib import contextmanager

from mailer import BaseMailer, MailerUnavailable, SendFailed

log = logging.getLogger(__name__)

OL_MAIL_ITEM = 0
OL_FORMAT_PLAIN = 1
OL_FOLDER_DRAFTS = 16
# DISPID de MailItem.SendUsingAccount y DISPATCH_PROPERTYPUTREF: alternativa
# conocida cuando la asignación directa no surte efecto con pywin32.
_DISPID_SEND_USING_ACCOUNT = 64209
_DISPATCH_PROPERTYPUTREF = 8


def _import_com():
    try:
        import pythoncom
        import pywintypes
        import win32com.client
    except ImportError:
        raise MailerUnavailable(
            "El envío con Outlook solo funciona en Windows con pywin32 instalado.")
    return pythoncom, pywintypes, win32com.client


def _com_text(error):
    """Descripción legible de un com_error (la que da Outlook, si la hay)."""
    excepinfo = error.args[2] if len(error.args) > 2 else None
    if excepinfo and len(excepinfo) > 2 and excepinfo[2]:
        return str(excepinfo[2]).strip()
    return str(error.args[1]) if len(error.args) > 1 else str(error)


class OutlookMailer(BaseMailer):
    name = "outlook"

    @contextmanager
    def _outlook(self):
        """Abre Outlook en el hilo actual y produce (aplicación, espacio MAPI)."""
        pythoncom, pywintypes, client = _import_com()
        # Cada hilo que use COM debe inicializarlo.
        pythoncom.CoInitialize()
        try:
            try:
                app = client.Dispatch("Outlook.Application")
                namespace = app.GetNamespace("MAPI")
            except pywintypes.com_error as e:
                log.warning("No se pudo abrir Outlook: %s", getattr(e, "hresult", e))
                raise MailerUnavailable(
                    "No se pudo conectar con Outlook clásico. Compruebe que Outlook clásico está "
                    "instalado (el nuevo Outlook no es compatible) y que tiene un perfil configurado.")
            yield app, namespace, pywintypes
        finally:
            pythoncom.CoUninitialize()

    def _accounts(self, namespace, pywintypes):
        """[(direccion, objeto_cuenta)] de las cuentas configuradas en el perfil."""
        try:
            accounts = namespace.Accounts
            out = []
            for i in range(1, accounts.Count + 1):
                acc = accounts.Item(i)
                address = (acc.SmtpAddress or "").strip().lower()
                if address:
                    out.append((address, acc))
            return out
        except pywintypes.com_error:
            raise MailerUnavailable("No se pudieron leer las cuentas configuradas en Outlook.")

    def list_accounts(self):
        with self._outlook() as (_, namespace, pywintypes):
            return [address for address, _ in self._accounts(namespace, pywintypes)]

    @contextmanager
    def session(self, sender=None):
        address = self._resolve_sender(sender)
        with self._outlook() as (app, namespace, pywintypes):
            accounts = self._accounts(namespace, pywintypes)
            account = next((acc for addr, acc in accounts if addr == address), None)
            if account is None:
                raise MailerUnavailable(
                    f"La cuenta {address} no está configurada en Outlook "
                    f"({len(accounts)} cuenta(s) encontrada(s)). No se enviará nada.")
            yield _OutlookSession(app, account, address, pywintypes)


class _OutlookSession:
    def __init__(self, app, account, expected_sender, pywintypes):
        self.app = app
        self.account = account
        self.account_address = expected_sender
        self._expected = expected_sender
        self._com_error = pywintypes.com_error

    def send(self, to, subject, body):
        # Fase 1: preparar el mensaje. Cualquier fallo aquí es un error
        # confirmado: el mensaje todavía no se ha entregado a Outlook.
        try:
            mail = self._create_mail()
            mail.To = to
            mail.Subject = subject
            mail.BodyFormat = OL_FORMAT_PLAIN
            mail.Body = body
            self._set_account(mail)

            if mail.Recipients.Count != 1:
                raise SendFailed("El mensaje no tiene exactamente un destinatario.")
            if not mail.Recipients.ResolveAll():
                raise SendFailed("Outlook no pudo resolver la dirección del destinatario.")
            sender = (mail.SendUsingAccount.SmtpAddress or "").strip().lower()
            if sender != self._expected:
                raise SendFailed("No se pudo fijar la cuenta corporativa como remitente.")
        except SendFailed:
            raise
        except self._com_error as e:
            log.exception("Error COM preparando mensaje")
            raise SendFailed(f"Outlook rechazó la preparación del mensaje ({_com_text(e)}).")

        # Fase 2: entregar a Outlook. Si falla aquí no podemos saber con
        # certeza si el mensaje salió, así que se marca como incierto.
        try:
            mail.Send()
        except self._com_error:
            log.exception("Error COM en Send()")
            raise SendFailed(
                "Outlook devolvió un error al enviar. Compruebe Elementos enviados y Bandeja de salida.",
                uncertain=True)

    def _create_mail(self):
        # En algunos perfiles Application.CreateItem falla con un error genérico
        # (0x80004005) aunque Outlook responde; crear el mensaje en Borradores
        # de la cuenta elegida suele funcionar y además lo asocia a esa cuenta.
        try:
            return self.app.CreateItem(OL_MAIL_ITEM)
        except self._com_error as e:
            log.warning("CreateItem falló (%s); se crea el mensaje en Borradores de %s",
                        _com_text(e), self._expected)
        drafts = self.account.DeliveryStore.GetDefaultFolder(OL_FOLDER_DRAFTS)
        return drafts.Items.Add(OL_MAIL_ITEM)

    def _set_account(self, mail):
        try:
            mail.SendUsingAccount = self.account
        except self._com_error:
            pass
        current = mail.SendUsingAccount
        if current is None or (current.SmtpAddress or "").strip().lower() != self._expected:
            mail._oleobj_.Invoke(_DISPID_SEND_USING_ACCOUNT, 0, _DISPATCH_PROPERTYPUTREF, 0, self.account)
