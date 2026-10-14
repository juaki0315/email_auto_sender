"""Pruebas de outlook_sender con objetos COM simulados (no requieren Windows)."""

import pytest

from mailer import SendFailed
from outlook_sender import _com_text, _OutlookSession


class FakeComError(Exception):
    pass


class FakePywintypes:
    com_error = FakeComError


def outlook_error(text="Error en la operación."):
    return FakeComError(-2147352567, "Exception occurred.",
                        (4096, "Microsoft Outlook", text, None, 0, -2147467259), None)


class FakeAccount:
    def __init__(self, address, drafts=None):
        self.SmtpAddress = address
        self.DeliveryStore = FakeStore(drafts)


class FakeStore:
    def __init__(self, drafts):
        self.drafts = drafts

    def GetDefaultFolder(self, number):
        assert number == 16
        return self.drafts


class FakeRecipients:
    def __init__(self, mail):
        self.mail = mail

    @property
    def Count(self):
        return len([a for a in self.mail.To.split(";") if a.strip()])

    def ResolveAll(self):
        return True


class FakeMail:
    def __init__(self):
        self.To = ""
        self.SendUsingAccount = None
        self.Recipients = FakeRecipients(self)
        self.sent = False

    def Send(self):
        self.sent = True


class FakeFolder:
    def __init__(self):
        self.Items = self
        self.created = []

    def Add(self, kind):
        assert kind == 0
        mail = FakeMail()
        self.created.append(mail)
        return mail


class FakeApp:
    def __init__(self, fail=False):
        self.fail = fail
        self.created = []

    def CreateItem(self, kind):
        if self.fail:
            raise outlook_error()
        mail = FakeMail()
        self.created.append(mail)
        return mail


def make_session(app, drafts=None):
    account = FakeAccount("yo@agasesor.com", drafts)
    return _OutlookSession(app, account, "yo@agasesor.com", FakePywintypes)


def test_envio_normal_con_createitem():
    app = FakeApp()
    make_session(app).send("cliente@ejemplo.com", "Asunto", "Cuerpo")
    assert app.created[0].sent
    assert app.created[0].SendUsingAccount.SmtpAddress == "yo@agasesor.com"


def test_si_createitem_falla_usa_borradores_de_la_cuenta():
    drafts = FakeFolder()
    make_session(FakeApp(fail=True), drafts).send("cliente@ejemplo.com", "Asunto", "Cuerpo")
    assert len(drafts.created) == 1 and drafts.created[0].sent


def test_si_todo_falla_muestra_el_texto_de_outlook():
    class BrokenFolder(FakeFolder):
        def Add(self, kind):
            raise outlook_error("No se puede crear el elemento.")

    with pytest.raises(SendFailed) as info:
        make_session(FakeApp(fail=True), BrokenFolder()).send("c@ejemplo.com", "A", "B")
    assert "No se puede crear el elemento." in str(info.value)
    assert not getattr(info.value, "uncertain", False)


def test_com_text_sin_detalle():
    assert _com_text(FakeComError(-1, "Algo falló", None, None)) == "Algo falló"
