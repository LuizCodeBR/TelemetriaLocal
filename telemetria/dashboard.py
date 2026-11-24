"""Painel servido no navegador e relatório HTML que abre direto do disco."""
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sqlite3
from pathlib import Path
from string import Template
from threading import Thread
from urllib.parse import urlsplit

from . import deliveries, freshness_report, incidents, maintenance, timeline

from .alerts import seconds_between
from .communication import OUTCOMES, STATES

STATUS = {"aberta": "Aberta na última medição", "recuperada": "Recuperada", "lacuna": "Interrompida por lacuna"}

TEMPLATE = Template(Path(__file__).with_name("painel.html").read_text(encoding="utf-8"))


def number(value):
    return f"{value:.2f}".replace(".", ",")


def instant(value):
    moment = datetime.fromisoformat(value)
    label = moment.strftime("%d/%m/%Y %H:%M:%S")
    return label + (f",{moment.microsecond:06d}".rstrip("0") if moment.microsecond else "")


def duration(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, whole_seconds = divmod(rest, 60)
    remainder = whole_seconds + (seconds - int(seconds))
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if remainder or not parts:
        parts.append(f"{remainder:.9g}".replace(".", ",") + " s")
    return " ".join(parts)


def alert_cells(events):
    lines = []
    for event in events:
        status = event["status"]
        until = event["observed_until"]
        lines.append(
            f'<tr><th scope="row">{escape(event["sensor"])}</th><td><span class="status {status}">{STATUS[status]}</span></td>'
            f'<td>{instant(event["started"])}</td><td>{instant(until)}</td>'
            f'<td>{duration(seconds_between(event["started"], until))}</td><td>{number(event["peak"])} °C</td></tr>'
        )
    return "".join(lines) or '<tr><td colspan="6">Nenhuma ocorrência de temperatura neste histórico.</td></tr>'


def rule_cells(rules, live):
    lines = []
    now = datetime.now(UTC)
    for rule in rules:
        at = rule["last_at"]
        last = instant(at) if at else "Aguardando medição"
        if live and at and (now - datetime.fromisoformat(at)).total_seconds() > rule["gap_seconds"]:
            last += " · sem medição recente"
        lines.append(
            f'<tr><th scope="row">{escape(rule["sensor"])}</th><td>{number(rule["high"])} °C</td>'
            f'<td>{number(rule["recovery"])} °C</td><td>{duration(rule["gap_seconds"])}</td><td>{last}</td></tr>'
        )
    return "".join(lines) or '<tr><td colspan="5">Aguardando sensores.</td></tr>'


def communication_fields(data, windows=None, freshness=None):
    states = []
    for row in data["sensors"]:
        contact = instant(row["last_contact"]) if row["last_contact"] else "—"
        state = row["state"]
        window = (windows or {}).get(row["sensor"])
        planned = (" · Em manutenção até " + instant(window["end"]) + " UTC · " + escape(window["reason"])) if window else ""
        measurement = (freshness or {}).get(row["sensor"])
        quality = freshness_report.STATES[measurement["freshness"]] if measurement else "—"
        measured = instant(measurement["measured"]) if measurement and measurement["measured"] else "—"
        states.append(f'<tr><th scope="row">{escape(row["sensor"])}</th>'
                      f'<td><span class="status {state}">{STATES[state]}</span>{planned}</td><td>{contact}</td>'
                      f'<td>{quality}</td><td>{measured}</td></tr>')
    interruptions = []
    for row in data["events"]:
        last = instant(row["last_contact"]) if row["last_contact"] else "—"
        end = instant(row["ended"]) if row["ended"] else "—"
        until = row["ended"] or data["seen"]
        elapsed = duration(seconds_between(row["detected"], until))
        interruptions.append(f'<tr><th scope="row">{escape(row["sensor"])}</th><td>{OUTCOMES[row["outcome"]]}</td>'
                             f'<td>{last}</td><td>{instant(row["detected"])}</td><td>{end}</td><td>{elapsed}</td></tr>')
    gaps = [f'<li>{instant(g["started"])} UTC até {instant(g["ended"]) + " UTC" if g["ended"] else "nova observação"}</li>'
            for g in data["gaps"]]
    return {
        "presence_asof": ("Observado até " + instant(data["seen"]) + " UTC") if data["seen"] else "Aguardando a central",
        "presence_rows": "".join(states) or '<tr><td colspan="5">Aguardando o primeiro sensor.</td></tr>',
        "outages": "".join(interruptions) or '<tr><td colspan="6">Nenhuma interrupção registrada.</td></tr>',
        "blind_periods": "".join(gaps) or "<li>Nenhum intervalo identificado.</li>",
    }


def render(snapshot, live):
    rows = snapshot["rows"]
    last = rows[0] if rows else None
    sensor = escape(last["sensor"]) if last else "Aguardando sensor"
    points = [[datetime.fromisoformat(row["measured_at"]).timestamp(), row["temperature"]] for row in snapshot["trend"]]
    cells = "".join(
        f'<tr><th scope="row">{escape(row["sensor"])}</th><td>{instant(row["measured_at"])}</td>'
        f'<td>{number(row["temperature"])} °C</td><td>{number(row["humidity"])} %</td>'
        f'<td>{instant(row["received_at"])}</td></tr>' for row in rows
    )
    contact = "Aguardando a primeira leitura"
    if snapshot["last_received"]:
        received = datetime.fromisoformat(snapshot["last_received"])
        if live:
            seconds = max(0, int((datetime.now(UTC) - received).total_seconds()))
            contact = f"Último recebimento há {seconds} s"
        else:
            contact = f'Último recebimento em {instant(snapshot["last_received"])} UTC'
    return TEMPLATE.substitute(
        **communication_fields(snapshot["communication"], snapshot.get("maintenance"),snapshot.get("freshness")),
        history_link=('<nav aria-label="Relatórios"><a href="/historico">Histórico por sensor</a>'
                      '<a href="/entregas">Atraso de entrega</a><a href="/incidentes">Incidentes</a><a href="/manutencao">Manutenção</a><a href="/atualidade">Atualidade das medições</a></nav>' if live else ""),
        refresh='<meta http-equiv="refresh" content="5">' if live else "",
        mode="Ao vivo" if live else "Demonstração", contact=contact, sensor=sensor,
        total=snapshot["total"], temp=number(last["temperature"]) if last else "—",
        humidity=number(last["humidity"]) if last else "—", points=json.dumps(points),
        events=alert_cells(snapshot["events"]), rules=rule_cells(snapshot["rules"], live),
        gap_count=len(snapshot["gaps"]),
        gaps="".join(f'<li><strong>{escape(g["sensor"])}</strong> · {instant(g["before_at"])} a '
                     f'{instant(g["after_at"])} UTC · {duration(seconds_between(g["before_at"], g["after_at"]))}</li>'
                     for g in snapshot["gaps"]) or "<li>Nenhuma lacuna entre medições.</li>",
        rows=cells or '<tr><td colspan="5">As medições aparecerão aqui quando o sensor enviar dados.</td></tr>',
    )


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, history, **kwargs):
        self.history = history
        super().__init__(*args, **kwargs)

    def do_GET(self):
        url = urlsplit(self.path)
        content_type = "text/html; charset=utf-8"
        if url.path == "/":
            body = render(self.history.snapshot(live=True), live=True).encode()
        elif url.path == "/historico":
            body = timeline.page(timeline.sensors(self.history)).encode()
        elif url.path == "/atualidade":
            body = freshness_report.page().encode()
        elif url.path == "/api/atualidade":
            try:
                sensor = freshness_report.query_sensor(url.query)
                data = freshness_report.capture(self.history,sensor)
            except ValueError:
                self.send_error(400,"Filtro de sala invalido")
                return
            content_type = "application/json; charset=utf-8"
            body = json.dumps(data,ensure_ascii=False).encode()
        elif url.path == "/manutencao":
            body = maintenance.page().encode()
        elif url.path == "/api/manutencao":
            content_type = "application/json; charset=utf-8"
            body = json.dumps(maintenance.agenda(self.history)).encode()
        elif url.path == "/incidentes":
            body = incidents.page().encode()
        elif url.path == "/api/incidentes":
            try:
                data = incidents.capture(self.history, **incidents.filters(url.query))
            except ValueError:
                self.send_error(400, "Filtro de incidentes invalido")
                return
            content_type = "application/json; charset=utf-8"
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        elif url.path == "/entregas":
            body = deliveries.page().encode()
        elif url.path == "/api/entregas":
            try:
                data = deliveries.capture(self.history, **deliveries.query_params(url.query))
            except ValueError:
                self.send_error(400, "Periodo de recebimento invalido")
                return
            content_type = "application/json; charset=utf-8"
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        elif url.path == "/api/sensores":
            content_type = "application/json; charset=utf-8"
            body = json.dumps(timeline.sensors(self.history)).encode()
        elif url.path == "/api/historico":
            try:
                data = timeline.period(self.history, **timeline.query_params(url.query))
            except ValueError:
                self.send_error(400, "Filtro de historico invalido")
                return
            content_type = "application/json; charset=utf-8"
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        else:
            self.send_error(404)
            return
        self.response_body(200, content_type, body)

    def do_POST(self):
        match = re.fullmatch(r"/api/incidentes/([1-9][0-9]{0,18})/(visto|nota)", self.path)
        maintenance_path = self.path in ("/api/manutencao", "/api/manutencao/cancelar")
        if not match and not maintenance_path:
            self.send_error(404)
            return
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update(("localhost", "127.0.0.1"))
        host = self.headers.get("Host", "").lower()
        if host not in hosts or self.headers.get("Origin", "http://" + host) != "http://" + host:
            self.send_error(403)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(415)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= 16384:
                raise ValueError
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Dados inválidos.")
            if maintenance_path:
                result = maintenance.update(self.history, data, cancel=self.path.endswith("/cancelar"))
            else:
                token = data.pop("ocorrencia", "")
                result = incidents.record(self.history, incidents.event_number(match[1]), match[2], data,
                                          expected_token=token if isinstance(token, str) else "")
        except maintenance.Conflict as exc:
            code = "origem_alterada" if isinstance(exc, maintenance.OriginChanged) else "conflito_agenda"
            self.response_body(409, "application/json; charset=utf-8", json.dumps({"error":str(exc),"code":code}).encode())
            return
        except incidents.IncidentChanged:
            self.response_body(409, "application/json; charset=utf-8", b'{"error":"incidente_alterado"}')
            return
        except incidents.NoteConflict:
            self.send_error(409)
            return
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        except KeyError:
            self.send_error(404)
            return
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            self.send_error(503)
            return
        self.response_body(201 if result["created"] else 200, "application/json; charset=utf-8",
                           json.dumps(result).encode())

    def response_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@contextmanager
def dashboard(history, port):
    with ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, history=history)) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        worker.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            worker.join()
