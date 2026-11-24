"""Agendamentos de manutenção no relógio UTC da central."""
import re
from datetime import UTC, datetime

from . import availability
from .communication import stamp


ACTION_TIME_INDEX = "CREATE INDEX IF NOT EXISTS comm_actions_by_time ON comm_actions(at)"
SCHEMA = """
    CREATE TABLE IF NOT EXISTS maintenance_origin (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT NOT NULL
    );
    INSERT OR IGNORE INTO maintenance_origin VALUES (1, lower(hex(randomblob(16))));
    CREATE TABLE IF NOT EXISTS maintenance_windows (
        key TEXT PRIMARY KEY NOT NULL, sensor TEXT NOT NULL, start TEXT NOT NULL,
        end TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, canceled_at TEXT,
        CHECK(created_at < start AND start < end),
        CHECK(canceled_at IS NULL OR (created_at <= canceled_at AND canceled_at < start))
    );
    CREATE INDEX IF NOT EXISTS maintenance_room_period
        ON maintenance_windows(sensor,end,start) WHERE canceled_at IS NULL;
    CREATE INDEX IF NOT EXISTS maintenance_creation ON maintenance_windows(created_at);
    CREATE INDEX IF NOT EXISTS maintenance_cancellation ON maintenance_windows(canceled_at);
""" + ACTION_TIME_INDEX + ";\n"


class Conflict(ValueError):
    """O estado da agenda mudou ou o horário já está ocupado."""


class OriginChanged(Conflict):
    """A central aberta usa outra base de dados."""


def origin(db):
    return db.execute("SELECT token FROM maintenance_origin WHERE singleton=1").fetchone()[0]


def clock(db, now):
    # Um ajuste do relógio não deve permitir reclassificar uma queda já observada.
    row = db.execute("""SELECT (SELECT seen FROM comm_windows ORDER BY id DESC LIMIT 1),
                        (SELECT max(created_at) FROM maintenance_windows),
                        (SELECT max(canceled_at) FROM maintenance_windows),
                        (SELECT max(at) FROM comm_actions),
                        (SELECT max(checked) FROM freshness_watch)""").fetchone()
    return max(stamp(now or datetime.now(UTC)), *(t or "" for t in row))


def agenda(history, *, now=None):
    with history.connect() as db:
        db.execute("BEGIN")
        token = origin(db)
        windows = [dict(row) for row in db.execute("SELECT * FROM maintenance_windows ORDER BY start,sensor,key")]
        return {"origin":token, "now":clock(db, now), "rooms":availability.sensor_names(db), "windows":windows}


def update(history, data, *, cancel=False, now=None):
    from .timeline import utc_value
    from .storage import valid_sensor
    fields = {"origin", "key"} if cancel else {"origin", "key", "sensor", "start", "end", "reason"}
    if set(data) != fields or any(not isinstance(value, str) for value in data.values()):
        raise ValueError("Campos de agendamento inválidos.")
    key = data["key"]
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise ValueError("Identificador da janela inválido.")
    if not cancel:
        sensor, reason = data["sensor"], data["reason"].strip()
        if not valid_sensor(sensor) or not 1 <= len(reason) <= 160 or not reason.isprintable():
            raise ValueError("Informe uma sala e um motivo de até 160 caracteres em uma linha.")
        start, end = utc_value(data["start"]), utc_value(data["end"])
        if end <= start:
            raise ValueError("O fim deve ser posterior ao início.")
    def lookup(db):
        row = db.execute("""SELECT o.token,w.* FROM maintenance_origin o
                            LEFT JOIN maintenance_windows w ON w.key=? WHERE o.singleton=1""", (key,)).fetchone()
        if row["token"] != data["origin"]:
            raise OriginChanged("A pasta da central mudou. Atualize a agenda.")
        if row["key"] is None:
            return row, None
        if cancel:
            receipt = {"created":False, "at":row["canceled_at"]} if row["canceled_at"] else None
        else:
            if (row["sensor"], row["start"], row["end"], row["reason"]) != (sensor, start, end, reason):
                raise Conflict("Este envio já corresponde a outra janela.")
            receipt = {"created":False, "at":row["created_at"]}
            if row["canceled_at"]:
                receipt["canceled_at"] = row["canceled_at"]
        return row, receipt

    with history.connect() as db:
        _, receipt = lookup(db)
        if receipt is not None:
            return receipt
        db.execute("BEGIN IMMEDIATE")
        row, receipt = lookup(db)
        if receipt is not None:
            return receipt
        at = clock(db, now)
        if cancel:
            if row["key"] is None:
                raise KeyError(key)
            if at >= row["start"]:
                raise Conflict("A janela já começou e seu histórico está preservado.")
            db.execute("UPDATE maintenance_windows SET canceled_at=? WHERE key=?", (at, key))
        else:
            if sensor not in availability.sensor_names(db):
                raise ValueError("Escolha uma sala conhecida pela central.")
            if start <= at:
                raise Conflict("Escolha um início futuro no relógio da central.")
            if db.execute("""SELECT 1 FROM maintenance_windows WHERE sensor=? AND canceled_at IS NULL
                             AND start<? AND end>? LIMIT 1""", (sensor, end, start)).fetchone():
                raise Conflict("A sala já tem uma janela nesse horário.")
            db.execute("""INSERT INTO maintenance_windows(key,sensor,start,end,reason,created_at)
                          VALUES(?,?,?,?,?,?)""", (key,sensor,start,end,reason,at))
        return {"created":True, "at":at}


def active_by_room(db, at):
    return {row["sensor"]:dict(row) for row in db.execute("""
        SELECT sensor,end,reason FROM maintenance_windows
        WHERE canceled_at IS NULL AND start<=? AND end>?""", (at, at))}


def incident_windows(db, sensor, start, until):
    return [dict(row) for row in db.execute("""SELECT start,end,reason FROM maintenance_windows
        WHERE sensor=? AND canceled_at IS NULL AND end>? AND (start<? OR (start=? AND ?=?))
        ORDER BY start""", (sensor, start, until, start, start, until))]


def page(example=None):
    from . import timeline
    template = (timeline.DIRECTORY / "manutencao.html").read_text(encoding="utf-8")
    script = (timeline.DIRECTORY / "manutencao.js").read_text(encoding="utf-8")
    return timeline.render_report(template, script, example)
