"""Consulta consistente da comunicação, atualidade e manutenção por sala."""
from datetime import UTC, datetime
from urllib.parse import parse_qs

from . import communication, freshness, maintenance, timeline
from .storage import valid_sensor

STATES = {"atualizada":"Medição atualizada", "desatualizada":"Medição desatualizada",
          "aguardando":"Aguardando medição atual", "sem_observacao":"Sem observação da central"}


def query_sensor(query):
    if not query:
        return None
    fields = parse_qs(query,strict_parsing=True,keep_blank_values=True,max_num_fields=1)
    if set(fields) != {"sala"} or not valid_sensor(fields["sala"][0]):
        raise ValueError("Filtro de sala inválido.")
    return fields["sala"][0]


def capture(history, sensor=None, *, now=None):
    if sensor is not None and not valid_sensor(sensor):
        raise ValueError("Sala inválida.")
    with history.connect() as db:
        db.execute("BEGIN")
        db.execute("SELECT id FROM comm_windows ORDER BY id DESC LIMIT 1").fetchone()
        moment = now or datetime.now(UTC)
        comm = communication.report(db,live=True,now=moment)
        rooms = freshness.room_states(db,comm)
        active = maintenance.active_by_room(db,maintenance.clock(db,moment))
        selected = [{**r,"maintenance":active.get(name)} for name,r in rooms.items()
                    if sensor is None or sensor == name]
        order = " ORDER BY e.detected DESC,e.id DESC"
        query = freshness.INTERVALS
        if sensor is None:
            rows = db.execute(query+" WHERE e.ended IS NULL"+order).fetchall()
            rows += db.execute(query+" WHERE e.ended IS NOT NULL ORDER BY e.ended DESC,e.id DESC LIMIT 50").fetchall()
            if not comm["observing"]:
                rows.sort(key=lambda row:(row["until"],row["id"]),reverse=True)
                rows = rows[:50]
            rows.sort(key=lambda row:(row["detected"],row["id"]),reverse=True)
        else:
            rows = db.execute(query+" WHERE e.sensor=?"+order,(sensor,)).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            if not event["ended"] and not comm["observing"]:
                event.update(ended=event["until"],outcome="sem_observacao")
            event["maintenance"] = maintenance.incident_windows(db,event["sensor"],event["detected"],event["until"])
            events.append(event)
        return {"rooms":list(rooms),"current":selected,"events":events,"seen":comm["seen"]}


def page(example=None):
    template = (timeline.DIRECTORY/"atualidade.html").read_text(encoding="utf-8")
    script = (timeline.DIRECTORY/"atualidade.js").read_text(encoding="utf-8")
    return timeline.render_report(template,script,{"states":STATES,"communication":communication.STATES,"example":example})
