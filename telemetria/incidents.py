"""Acompanhamento humano das interrupções detectadas pela central."""
from datetime import UTC, datetime, timedelta
import re
from urllib.parse import parse_qs

from . import availability, communication, maintenance, timeline
from .storage import valid_sensor


def event_number(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", raw) or int(raw) > 2**63 - 1:
        raise ValueError("Ocorrência inválida.")
    return int(raw)


def filters(query):
    fields = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=4)
    if set(fields) - {"sala", "abertos", "pendentes", "id"} or any(len(v) != 1 for v in fields.values()):
        raise ValueError("Filtro inválido.")
    args = {key: values[0] for key, values in fields.items()}
    if "sala" in args and not valid_sensor(args["sala"]):
        raise ValueError("Sala inválida.")
    if any(args[key] != "1" for key in ("abertos", "pendentes") if key in args):
        raise ValueError("Filtro inválido.")
    return dict(room=args.get("sala"), open_only="abertos" in args, unseen_only="pendentes" in args,
                selected=event_number(args["id"]) if "id" in args else None)


class NoteConflict(ValueError):
    pass


class IncidentChanged(ValueError):
    pass


def record(history, event_id, action, payload, *, now=None, expected_token=None):
    if not isinstance(payload, dict):
        raise ValueError("Dados inválidos.")
    key, body = "visto", None
    if action == "nota" and set(payload) == {"texto", "chave"}:
        body, key = payload["texto"], payload["chave"]
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 1000:
            raise ValueError("Escreva uma nota de 1 a 1.000 caracteres.")
        body = body.strip()
        if any(ord(c) < 32 and c not in "\n\r\t" for c in body):
            raise ValueError("Caractere de controle inválido na nota.")
        body.encode("utf-8")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("Identificador de envio inválido.")
    elif action != "visto" or payload:
        raise ValueError("Ação inválida.")
    def lookup(db):
        row = db.execute("""SELECT e.detected, coalesce(e.ended,w.seen) AS observed, i.token,
                                   a.at AS saved_at, a.body AS saved_body
                            FROM comm_events e JOIN comm_windows w ON w.id=e.window_id
                            JOIN comm_incident_identity i ON i.event_id=e.id
                            LEFT JOIN comm_actions a ON a.event_id=e.id AND a.request_key=?
                            WHERE e.id=?""", (key, event_id)).fetchone()
        if row is None:
            raise KeyError(event_id)
        if expected_token is not None and row["token"] != expected_token:
            raise IncidentChanged("A ocorrência exibida pertence a outra sessão de dados.")
        if row["saved_at"] is not None and row["saved_body"] != body:
            raise NoteConflict("Este envio já foi usado com outra nota.")
        return row

    with history.connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        event = lookup(db)
        if event["saved_at"] is not None:
            return {"at":event["saved_at"], "created":False}
        db.execute("BEGIN IMMEDIATE")
        event = lookup(db)
        if event["saved_at"] is not None:
            return {"at":event["saved_at"], "created":False}
        last = db.execute("SELECT at FROM comm_actions WHERE event_id=? ORDER BY id DESC LIMIT 1",
                          (event_id,)).fetchone()
        at = max(communication.stamp(now or datetime.now(UTC)), event["detected"], event["observed"],
                 last["at"] if last else "")
        db.execute("INSERT INTO comm_actions(event_id,request_key,at,body) VALUES(?,?,?,?)",
                   (event_id, key, at, body))
        return {"at": at, "created": True}


def capture(history, *, room=None, open_only=False, unseen_only=False, selected=None, now=None):
    with history.connect() as db:
        db.execute("BEGIN")
        db.execute("SELECT 1 FROM comm_windows LIMIT 1").fetchone()
        clock = now or datetime.now(UTC)
        rooms = set(availability.sensor_names(db))
        rooms.update(row[0] for row in db.execute("SELECT sensor FROM thermal_rules"))
        sql = """SELECT e.id, e.sensor, e.detected, e.ended, e.outcome, w.seen, w.closed, a.at AS seen_at,
                        i.token,i.actions_before_recovery
                 FROM comm_events e JOIN comm_windows w ON w.id=e.window_id
                 JOIN comm_incident_identity i ON i.event_id=e.id
                 LEFT JOIN comm_actions a ON a.event_id=e.id AND a.request_key='visto'"""
        args = ()
        if room is not None:
            sql += " WHERE e.sensor=?"
            args = (room,)
        items, detail = [], None
        for row in db.execute(sql + " ORDER BY e.detected DESC, e.id DESC", args):
            ended, outcome = row["ended"], row["outcome"]
            if not ended and (row["closed"] or clock - datetime.fromisoformat(row["seen"]) >= timedelta(seconds=communication.TIMEOUT)):
                ended, outcome = row["seen"], "sem_observacao"
            event = {"id":str(row["id"]), "token":row["token"], "recovery_position":row["actions_before_recovery"], "sensor":row["sensor"], "detected":row["detected"],
                     "ended":ended, "outcome":outcome, "until":ended or row["seen"], "seen_at":row["seen_at"]}
            if selected == row["id"]:
                detail = event
            if (not open_only or not outcome) and (not unseen_only or not row["seen_at"]):
                items.append(event)
        if detail is None and items:
            detail = items[0]
        if detail is not None:
            actions = db.execute("SELECT at,body FROM comm_actions WHERE event_id=? ORDER BY id", (detail["id"],))
            detail = {**detail, "actions":[dict(action) for action in actions],
                      "maintenance":maintenance.incident_windows(db, detail["sensor"], detail["detected"], detail["until"])}
    return {"rooms":sorted(rooms), "items":items, "detail":detail}


def delivery_range(event):
    start, end = (datetime.fromisoformat(event[k]) for k in ("detected", "ended"))
    return [communication.stamp(t.replace(microsecond=t.microsecond // 1000 * 1000))
            for t in (start, end + timedelta(minutes=1))]


def page(example=None):
    return timeline.render_report(PAGE, SCRIPT, example)


PAGE = (timeline.DIRECTORY / "incidentes.html").read_text(encoding="utf-8")
SCRIPT = (timeline.DIRECTORY / "incidentes.js").read_text(encoding="utf-8")
