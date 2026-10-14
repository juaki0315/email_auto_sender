"""AG Asesor — Envío de correos a clientes. Punto de entrada y backend local (Flask).

Uso:
    python app.py              # Windows: Outlook real. Otros sistemas: simulado.
    python app.py --simulado   # fuerza el modo simulado (no envía nada)
"""

import argparse
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import socket
import sys
import threading
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

import core
from database import ConflictError, Database, DuplicateError, backup_file
from mailer import MailerUnavailable, SendFailed, create_mailer

APP_ID = "agasesor-cuotas"
APP_NAME = "AG Asesores — Envío de correos"
VERSION = "1.1.0"
DEFAULT_PORT = 8765
# "sender" vacío: la cuenta de envío se elige en la pestaña Configuración.
DEFAULT_CONFIG = {"sender": "", "port": DEFAULT_PORT}

log = logging.getLogger("agasesor")

STATUS_LABELS = {
    "pending": "Pendiente",
    "sending": "En proceso",
    "accepted": "Aceptado por Outlook",
    "error": "Error",
    "uncertain": "No confirmado",
}


# --- Rutas de archivos -------------------------------------------------------

def resource_dir() -> Path:
    """Carpeta de plantillas y estáticos (dentro del ejecutable si está empaquetado)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir() -> Path:
    """Carpeta de datos del usuario. Fuera del ejecutable para que sobreviva a actualizaciones."""
    if os.environ.get("AGASESOR_DATA_DIR"):
        d = Path(os.environ["AGASESOR_DATA_DIR"])
    elif getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        d = Path(base) / "AGAsesorCuotas"
    else:
        d = Path(__file__).resolve().parent / "instance"
    d.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def load_config(directory: Path) -> dict:
    path = directory / "config.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        cfg = {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    except (ValueError, OSError):
        log.error("config.json no válido; se usan valores por defecto")
        cfg = dict(DEFAULT_CONFIG)
    cfg["port"] = int(cfg["port"])
    return cfg


def save_config(directory: Path, cfg: dict):
    path = directory / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --- Aplicación --------------------------------------------------------------

def create_app(mailer, db: Database, port=None, synchronous=False, csrf_token=None, save_sender=None):
    res = resource_dir()
    app = Flask(__name__, template_folder=str(res / "templates"), static_folder=str(res / "static"))
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    token = csrf_token or secrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"} if port else {"localhost", "127.0.0.1"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    send_lock = threading.Lock()

    # --- Seguridad ---------------------------------------------------------

    @app.before_request
    def protect():
        # Evita ataques de DNS rebinding: solo se aceptan nombres locales.
        if request.host not in allowed_hosts:
            abort(400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and origin not in allowed_origins:
                abort(403)
            if not request.is_json:
                abort(415)
            sent = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                abort(403)

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    @app.errorhandler(415)
    def http_error(e):
        return jsonify(error="Solicitud no permitida."), e.code

    @app.errorhandler(ConflictError)
    def conflict(e):
        return jsonify(error=str(e)), 409

    @app.errorhandler(core.ValidationError)
    def invalid(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(Exception)
    def unexpected(e):
        log.exception("Error no controlado")
        return jsonify(error="Se produjo un error interno. Consulte el archivo de registro."), 500

    # --- Páginas -------------------------------------------------------------

    @app.get("/")
    def index():
        today = datetime.now()
        return render_template(
            "index.html", csrf_token=token, simulation=mailer.is_simulation,
            sender=mailer.sender, months=core.MONTHS, year=today.year,
            month=today.month, app_name=APP_NAME, version=VERSION)

    @app.get("/api/ping")
    def ping():
        return jsonify(app=APP_ID)

    @app.get("/api/status")
    def status():
        try:
            info = mailer.check()
            ok, message = True, f"Cuenta de envío: {info['account']}"
        except MailerUnavailable as e:
            ok, message = False, str(e)
        return jsonify(ok=ok, message=message, simulation=mailer.is_simulation, sender=mailer.sender,
                       active_batch=db.active_batch_id(), uncertain=db.count_uncertain())

    # --- Cuenta de envío -----------------------------------------------------

    @app.get("/api/accounts")
    def accounts():
        try:
            found = mailer.list_accounts()
            ok, message = True, ""
        except MailerUnavailable as e:
            found, ok, message = [], False, str(e)
        return jsonify(ok=ok, message=message, accounts=found, current=mailer.sender,
                       simulation=mailer.is_simulation)

    @app.post("/api/settings/sender")
    def set_sender():
        data = request.get_json(silent=True) or {}
        try:
            address = core.normalize_email(data.get("sender")).lower()
        except core.ValidationError as e:
            return jsonify(error=str(e)), 400
        if not send_lock.acquire(blocking=False):
            return jsonify(error="No se puede cambiar la cuenta mientras se envía una tanda."), 409
        try:
            if db.active_batch_id():
                return jsonify(error="No se puede cambiar la cuenta mientras se envía una tanda."), 409
            try:
                available = mailer.list_accounts()
            except MailerUnavailable as e:
                return jsonify(error=str(e)), 503
            if address not in available:
                return jsonify(error=f"La cuenta {address} no está configurada en Outlook."), 400
            mailer.set_sender(address)
            if save_sender:
                save_sender(address)
            log.info("Cuenta de envío cambiada")
            return jsonify(ok=True, sender=address)
        finally:
            send_lock.release()

    # --- Plantillas -----------------------------------------------------------

    def describe_template(t):
        fields = core.template_fields(t["subject"], t["body"])
        return {"id": t["id"], "name": t["name"], "subject": t["subject"], "body": t["body"],
                "fields": [{"name": f, "label": core.field_label(f), "amount": f == core.AMOUNT_FIELD}
                           for f in fields],
                "updated_at": t["updated_at"]}

    def json_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise core.ValidationError("Datos no válidos.")
        return data

    @app.get("/api/templates")
    def templates():
        return jsonify(templates=[describe_template(t) for t in db.list_templates()],
                       builtin=list(core.BUILTIN_FIELDS), amount_field=core.AMOUNT_FIELD)

    @app.post("/api/templates")
    def create_template():
        t = core.validate_template(json_body())
        tid = db.save_template(None, t["name"], t["subject"], t["body"])
        log.info("Plantilla %s creada", tid)
        return jsonify(template=describe_template(db.get_template(tid))), 201

    @app.put("/api/templates/<int:template_id>")
    def update_template(template_id):
        t = core.validate_template(json_body())
        db.save_template(template_id, t["name"], t["subject"], t["body"])
        log.info("Plantilla %s modificada", template_id)
        return jsonify(template=describe_template(db.get_template(template_id)))

    @app.delete("/api/templates/<int:template_id>")
    def delete_template(template_id):
        if not db.delete_template(template_id):
            return jsonify(error="La plantilla no existe o ya se había eliminado."), 404
        log.info("Plantilla %s eliminada", template_id)
        return jsonify(ok=True)

    # --- Clientes -------------------------------------------------------------

    def client_from_request():
        data = json_body()
        return core.normalize_name(data.get("name")), core.normalize_email(data.get("email"))

    @app.get("/api/clients")
    def clients():
        return jsonify(clients=db.list_clients())

    @app.post("/api/clients")
    def create_client():
        name, email = client_from_request()
        cid = db.save_client(None, name, email)
        return jsonify(client={"id": cid, "name": name, "email": email}), 201

    @app.put("/api/clients/<int:client_id>")
    def update_client(client_id):
        name, email = client_from_request()
        db.save_client(client_id, name, email)
        return jsonify(client={"id": client_id, "name": name, "email": email})

    @app.delete("/api/clients/<int:client_id>")
    def delete_client(client_id):
        if not db.delete_client(client_id):
            return jsonify(error="El cliente no existe o ya se había eliminado."), 404
        return jsonify(ok=True)

    # --- Tanda ---------------------------------------------------------------

    def parse_request():
        """Valida la petición de vista previa o envío.

        Los nombres y correos se toman de la lista de clientes guardada; de la
        petición solo se usan los ids seleccionados y los valores de los campos.
        Devuelve (datos, periodo, plantilla, destinatarios, errores) con
        errores = {id_cliente: {campo: mensaje}}.
        """
        data = json_body()
        period = core.parse_period(data.get("month"), data.get("year"))
        template = db.get_template(data.get("template_id")) if isinstance(data.get("template_id"), int) else None
        if not template:
            raise core.ValidationError("Elija una plantilla. La seleccionada no existe o se ha eliminado.")
        items = data.get("recipients")
        if not isinstance(items, list) or not items:
            raise core.ValidationError("Seleccione al menos un cliente.")
        if len(items) > core.MAX_CLIENTS:
            raise core.ValidationError(f"No se pueden enviar más de {core.MAX_CLIENTS} correos en una tanda.")
        ids = [it.get("client_id") if isinstance(it, dict) else None for it in items]
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or len(set(ids)) != len(ids):
            raise core.ValidationError("Selección de clientes no válida.")

        saved = db.get_clients(ids)
        fields = core.template_fields(template["subject"], template["body"])
        recipients, errors = [], {}
        for cid, item in zip(ids, items):
            client = saved.get(cid)
            if not client:
                errors[cid] = {"_": "Este cliente ya no existe. Actualice la lista."}
                continue
            values, amount, errs = core.validate_fields(fields, item.get("fields"))
            if errs:
                errors[cid] = errs
            else:
                recipients.append(core.Client(client["name"], client["email"], amount, values, cid))
        return data, period, template, recipients, errors

    def amount_text(cents):
        return None if cents is None else core.format_amount(cents)

    def describe_conflicts(conflicts):
        out = []
        for c in conflicts:
            out.append({
                "email": c["email"], "name": c["name"], "status": c["status"],
                "status_label": STATUS_LABELS[c["status"]], "attempted_at": c["attempted_at"],
                "batch_id": c["batch_id"], "amount": amount_text(c["amount_cents"]),
                "resendable": c["status"] == "accepted",
            })
        return out

    def render_for(template, period, r):
        values = {"nombre": r.name, "periodo": period.label, **r.fields}
        return core.render_template(template["subject"], template["body"], values)

    @app.post("/api/preview")
    def preview():
        _, period, template, recipients, errors = parse_request()
        has_amount = core.AMOUNT_FIELD in core.template_fields(template["subject"], template["body"])
        total = sum(r.amount_cents or 0 for r in recipients)
        resp = {
            "valid": not errors,
            "errors": {str(k): v for k, v in errors.items()},
            "summary": {
                "rows": len(recipients) + len(errors),
                "valid_count": len(recipients),
                "error_count": len(errors),
                "has_amount": has_amount,
                "total": core.format_amount(total) if has_amount else None,
                "period_label": period.label,
                "period_key": period.key,
                "template_name": template["name"],
                "template_updated_at": template["updated_at"],
            },
        }
        if not errors:
            conflicts = db.find_conflicts(period.key, template["comm_type"], [r.email_key for r in recipients])
            resp["conflicts"] = describe_conflicts(conflicts)
            resp["recipients"] = [{"name": r.name, "email": r.email, "amount": amount_text(r.amount_cents)}
                                  for r in recipients]
            subject, body = render_for(template, period, recipients[0])
            resp["sample"] = {"to": recipients[0].email, "subject": subject, "body": body}
        return jsonify(resp)

    @app.post("/api/send")
    def send():
        data, period, template, recipients, errors = parse_request()
        if errors:
            return jsonify(error="Corrija los errores de los clientes seleccionados antes de enviar."), 400
        if data.get("confirm") is not True:
            return jsonify(error="Falta la confirmación del envío."), 400
        # La plantilla no puede haber cambiado desde la vista previa que se confirmó.
        if data.get("template_updated_at") != template["updated_at"]:
            return jsonify(error="La plantilla se ha modificado desde la revisión. Vuelva a revisar el envío."), 409
        resend_ack = data.get("resend_ack") or []
        if not isinstance(resend_ack, list) or not all(isinstance(x, str) for x in resend_ack):
            return jsonify(error="Datos no válidos."), 400

        if not send_lock.acquire(blocking=False):
            return jsonify(error="Ya hay una tanda enviándose."), 409
        started = False
        try:
            if db.active_batch_id():
                return jsonify(error="Ya hay una tanda enviándose."), 409
            # La cuenta queda fijada al crear la tanda aunque luego se cambie la selección.
            sender = mailer.sender
            try:
                mailer.check(sender)
            except MailerUnavailable as e:
                return jsonify(error=str(e)), 503
            try:
                batch_id = db.create_batch(period.key, template["comm_type"], mailer.name, recipients,
                                           resend_ack, sender=sender, template=template)
            except DuplicateError as e:
                return jsonify(error=str(e), conflicts=describe_conflicts(e.conflicts)), 409
            log.info("Tanda %s creada: %s destinatarios, periodo %s, plantilla %s",
                     batch_id, len(recipients), period.key, template["id"])
            # A partir de aquí run_batch se encarga de liberar el bloqueo.
            started = True
            if synchronous:
                run_batch(batch_id, sender)
            else:
                threading.Thread(target=run_batch, args=(batch_id, sender), daemon=True).start()
            return jsonify(batch_id=batch_id), 202
        finally:
            if not started:
                send_lock.release()

    def run_batch(batch_id, sender):
        try:
            _run_batch(batch_id, sender)
        except Exception:
            log.exception("Fallo inesperado en la tanda %s", batch_id)
            db.abort_pending(batch_id, "No enviado: la tanda se detuvo por un error interno.")
        finally:
            db.finish_batch(batch_id)
            send_lock.release()
            log.info("Tanda %s finalizada", batch_id)

    def _run_batch(batch_id, sender):
        try:
            with mailer.session(sender) as session:
                _send_all(batch_id, session)
        except MailerUnavailable as e:
            # Outlook no se pudo abrir: nada de esta tanda llegó a entregarse.
            db.abort_pending(batch_id, f"No enviado: {e}")

    def _send_all(batch_id, session):
        for r in db.pending_recipients(batch_id):
            # Se genera a partir de lo guardado (copia de la plantilla y valores):
            # lo registrado es lo enviado.
            period = core.Period.from_key(r["period"])
            values = {**json.loads(r["fields"] or "{}"), "nombre": r["name"], "periodo": period.label}
            subject, body = core.render_template(r["subject_tpl"], r["body_tpl"], values)
            if not db.mark_sending(r["id"]):
                continue
            try:
                session.send(r["email"], subject, body)
            except SendFailed as e:
                db.mark_result(r["id"], "uncertain" if e.uncertain else "error", e.message)
                continue
            except MailerUnavailable as e:
                db.mark_result(r["id"], "error", str(e))
                db.abort_pending(batch_id, "No enviado: Outlook dejó de estar disponible.")
                return
            except Exception:
                log.exception("Error desconocido enviando (destinatario %s)", r["id"])
                db.mark_result(r["id"], "uncertain",
                               "Error inesperado durante el envío. Compruebe Elementos enviados en Outlook.")
                continue
            db.mark_result(r["id"], "accepted")

    @app.get("/api/batches/<int:batch_id>")
    def batch(batch_id):
        b = db.get_batch(batch_id)
        if not b:
            abort(404)
        counts = {s: 0 for s in STATUS_LABELS}
        for r in b["recipients"]:
            counts[r["status"]] += 1
            r["amount"] = amount_text(r.pop("amount_cents"))
            r["status_label"] = STATUS_LABELS[r["status"]]
        return jsonify(
            id=b["id"], status=b["status"], period_label=core.Period.from_key(b["period"]).label,
            finished=b["status"] != "in_progress", counts=counts, total=len(b["recipients"]),
            recipients=b["recipients"], mailer=b["mailer"], sender=b["sender"],
            template_name=b["template_name"])

    # --- Historial ------------------------------------------------------------

    @app.get("/api/history")
    def history():
        period_key = None
        if request.args.get("year") and request.args.get("month"):
            try:
                period_key = core.parse_period(request.args["month"], request.args["year"]).key
            except core.ValidationError as e:
                return jsonify(error=str(e)), 400
        st = request.args.get("status") or None
        if st and st not in STATUS_LABELS:
            return jsonify(error="Estado no válido."), 400
        rows = db.history(period_key, st)
        for r in rows:
            r["amount"] = amount_text(r.pop("amount_cents"))
            r["period_label"] = core.Period.from_key(r["period"]).label
            r["status_label"] = STATUS_LABELS[r["status"]]
        return jsonify(rows=rows)

    @app.post("/api/recipients/<uid>/resolve")
    def resolve(uid):
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("sent"), bool):
            return jsonify(error="Indique si el mensaje se envió o no."), 400
        if not db.resolve_uncertain(uid, data["sent"]):
            return jsonify(error="El registro no existe o ya no está pendiente de revisión."), 409
        return jsonify(ok=True)

    return app


# --- Arranque ----------------------------------------------------------------

def setup_logging(directory: Path):
    # El registro no incluye nombres, correos ni importes: solo identificadores.
    handler = logging.handlers.RotatingFileHandler(
        directory / "agasesor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def already_running(port) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:
        return False


def port_free(port) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--simulado", action="store_true", help="No envía correos reales.")
    parser.add_argument("--puerto", type=int, help="Puerto local (por defecto, el de config.json).")
    parser.add_argument("--no-navegador", action="store_true", help="No abrir el navegador.")
    args = parser.parse_args(argv)

    directory = data_dir()
    setup_logging(directory)
    cfg = load_config(directory)
    port = args.puerto or cfg["port"]
    url = f"http://127.0.0.1:{port}/"

    simulate = args.simulado or sys.platform != "win32"
    if not port_free(port):
        if already_running(port):
            print("La aplicación ya estaba abierta. Abriendo el navegador...")
            webbrowser.open(url)
            return
        print(f"El puerto {port} está ocupado por otro programa. Cambie 'port' en {directory / 'config.json'}.")
        input("Pulse Intro para salir.")
        sys.exit(1)

    # La simulación usa su propia base de datos para que nunca se mezcle con envíos reales.
    db_path = directory / ("app-simulado.db" if simulate else "app.db")
    # La copia se hace antes de abrir la base: así queda una previa a cualquier migración.
    if db_path.exists():
        try:
            backup_file(db_path, directory / "copias")
        except Exception:
            log.exception("No se pudo crear la copia de seguridad")
    db = Database(db_path)
    recovered = db.recover_interrupted()
    if recovered:
        log.warning("Se recuperaron %s destinatarios de una tanda interrumpida", recovered)

    mailer = create_mailer(simulate, cfg["sender"])

    def save_sender(address):
        cfg["sender"] = address
        save_config(directory, cfg)

    app = create_app(mailer, db, port=port, save_sender=save_sender)

    print("=" * 60)
    print(f" {APP_NAME}  v{VERSION}")
    if simulate:
        print(" *** MODO SIMULADO: no se enviará ningún correo real ***")
    print(f" Abierta en: {url}")
    print(f" Datos en:   {directory}")
    print(" Mantenga esta ventana abierta mientras use la aplicación.")
    print(" Para cerrar la aplicación, cierre esta ventana.")
    print("=" * 60)

    if not args.no_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    from waitress import serve
    serve(app, host="127.0.0.1", port=port, threads=4, ident=None)


if __name__ == "__main__":
    main()
