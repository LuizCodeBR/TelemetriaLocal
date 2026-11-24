"""Presença por sensor no relógio da central. Cada janela é de observação contínua."""
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import freshness

PERIOD = 5
TIMEOUT = 15
SCHEMA = """
    CREATE TABLE IF NOT EXISTS comm_windows (
        id INTEGER PRIMARY KEY, started TEXT NOT NULL, seen TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS comm_contacts (
        sensor TEXT PRIMARY KEY, window_id INTEGER NOT NULL, last_contact TEXT NOT NULL, round_id TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS comm_events (
        id INTEGER PRIMARY KEY, sensor TEXT NOT NULL, window_id INTEGER NOT NULL, last_contact TEXT,
        detected TEXT NOT NULL, ended TEXT, outcome TEXT
    );
    CREATE TABLE IF NOT EXISTS comm_sensor_windows (
        window_id INTEGER NOT NULL, sensor TEXT NOT NULL, classified_from TEXT NOT NULL, first_contact TEXT,
        PRIMARY KEY (window_id, sensor), CHECK (first_contact IS NULL OR first_contact >= classified_from)
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS comm_actions (
        id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES comm_events(id),
        request_key TEXT NOT NULL, at TEXT NOT NULL, body TEXT,
        UNIQUE(event_id, request_key),
        CHECK ((request_key='visto' AND body IS NULL) OR
               (length(request_key)=32 AND body IS NOT NULL AND length(body) BETWEEN 1 AND 1000))
    );
    CREATE INDEX IF NOT EXISTS comm_actions_by_event ON comm_actions(event_id,id);
    CREATE TABLE IF NOT EXISTS comm_incident_identity (
        event_id INTEGER PRIMARY KEY REFERENCES comm_events(id),
        token TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16)))),
        actions_before_recovery INTEGER CHECK (actions_before_recovery >= 0)
    );
    CREATE TRIGGER IF NOT EXISTS comm_assign_identity AFTER INSERT ON comm_events BEGIN
        INSERT INTO comm_incident_identity(event_id) VALUES(NEW.id);
    END;
    CREATE TRIGGER IF NOT EXISTS comm_recovery_order AFTER UPDATE OF ended ON comm_events
    WHEN OLD.ended IS NULL AND NEW.ended IS NOT NULL AND NEW.outcome='retomada' BEGIN
        UPDATE comm_incident_identity SET actions_before_recovery=
            (SELECT count(*) FROM comm_actions WHERE event_id=NEW.id) WHERE event_id=NEW.id;
    END;
    CREATE UNIQUE INDEX IF NOT EXISTS comm_one_open ON comm_events(sensor) WHERE ended IS NULL;
    CREATE INDEX IF NOT EXISTS comm_sensor_history ON comm_events(sensor, id DESC);
    CREATE INDEX IF NOT EXISTS comm_recent_end ON comm_events(ended DESC, id DESC) WHERE ended IS NOT NULL;
"""

STATES = {"online": "Em comunicação", "offline": "Sem comunicação", "waiting": "Aguardando sinal",
          "unknown": "Sem observação", "never": "Sem sinal registrado"}

OUTCOMES = {"retomada": "Comunicação retomada", "sem_observacao": "Observação interrompida", None: "Em aberto"}


def stamp(moment):
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def offset(at, seconds):
    return stamp(datetime.fromisoformat(at) + timedelta(seconds=seconds))


@contextmanager
def exclusive_observer(path):
    # O sistema operacional libera a trava também na saída abrupta do processo.
    with Path(str(path) + ".monitor.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OSError("Outra central já observa esta pasta de dados.") from error
        yield


def close_window(db, window_id, at):
    freshness.close_window(db,window_id,at)
    db.execute("UPDATE comm_events SET ended=?, outcome='sem_observacao' WHERE window_id=? AND ended IS NULL",
               (at, window_id))
    db.execute("UPDATE comm_windows SET closed=1 WHERE id=?", (window_id,))


def restore_evidence(db):
    """Recupera apenas instantes comprovados pela versão anterior."""
    db.execute("""
        INSERT OR IGNORE INTO comm_sensor_windows(window_id,sensor,classified_from,first_contact)
        SELECT window_id,sensor,MIN(at),MIN(contact) FROM (
            SELECT window_id,sensor,last_contact AS at,last_contact AS contact FROM comm_contacts
            UNION ALL SELECT window_id,sensor,last_contact,last_contact FROM comm_events WHERE last_contact IS NOT NULL
            UNION ALL SELECT window_id,sensor,detected,NULL FROM comm_events
            UNION ALL SELECT window_id,sensor,ended,ended FROM comm_events WHERE outcome='retomada'
        ) GROUP BY window_id,sensor
    """)


def new_window(db, at):
    return db.execute("INSERT INTO comm_windows(started,seen) VALUES(?,?)", (at, at)).lastrowid


class Observer:
    """Um escritor por base. Avanço, detecção e retomada compartilham o mesmo commit."""
    def __init__(self, history, clock=None):
        self.history = history
        self.clock = clock
        self.window = None

    def start(self, now):
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            at = stamp(self.clock() if self.clock is not None else now)
            prev = db.execute("SELECT * FROM comm_windows ORDER BY id DESC LIMIT 1").fetchone()
            if prev:
                at = max(at, prev["seen"])
                if not prev["closed"]:
                    close_window(db, prev["id"], prev["seen"])
            window = new_window(db, at)
        self.window = window

    def stop(self):
        if self.window is None:
            return
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT seen FROM comm_windows WHERE id=? AND closed=0", (self.window,)).fetchone()
            if current:
                close_window(db, self.window, current["seen"])
        self.window = None

    def advance(self, now, sensor=None, round_id=None):
        at = stamp(now)
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            window = db.execute("SELECT * FROM comm_windows WHERE id=? AND closed=0", (self.window,)).fetchone()
            if window is None:
                raise RuntimeError("Observação encerrada")
            at = max(at, window["seen"])
            # A reserva de escrita pode ter demorado. Só agora sabemos se a janela ainda é contínua.
            acquired = max(at, stamp(self.clock())) if self.clock is not None else at
            if acquired >= offset(window["seen"], TIMEOUT):
                close_window(db, self.window, window["seen"])
                next_window = new_window(db, acquired)
            else:
                next_window = self.window
                db.execute("UPDATE comm_windows SET seen=? WHERE id=?", (at, self.window))
                overdue = db.execute("""
                    SELECT c.sensor, c.window_id, c.last_contact FROM comm_contacts c
                    WHERE (CASE WHEN c.window_id=? THEN c.last_contact ELSE ? END) <= ?
                    AND NOT EXISTS (SELECT 1 FROM comm_events e WHERE e.sensor=c.sensor AND e.ended IS NULL)
                """, (self.window, window["started"], offset(at, -TIMEOUT))).fetchall()
                for c in overdue:
                    last = c["last_contact"] if c["window_id"] == self.window else None
                    detected = offset(last or window["started"], TIMEOUT)
                    db.execute("INSERT INTO comm_events(sensor,window_id,last_contact,detected) VALUES(?,?,?,?)",
                               (c["sensor"], self.window, last, detected))
                    db.execute("INSERT OR IGNORE INTO comm_sensor_windows VALUES(?,?,?,NULL)",
                               (self.window, c["sensor"], detected))
                can_observe = not freshness.paused(db,self.window,at)
                if sensor is not None:
                    changed = db.execute("""
                        INSERT INTO comm_contacts VALUES(?,?,?,?) ON CONFLICT(sensor) DO UPDATE SET
                        window_id=excluded.window_id,last_contact=excluded.last_contact,round_id=excluded.round_id
                        WHERE comm_contacts.round_id<>excluded.round_id OR comm_contacts.window_id<>excluded.window_id
                    """, (sensor, self.window, at, round_id)).rowcount
                    if changed:
                        db.execute("""
                            INSERT INTO comm_sensor_windows VALUES(?,?,?,?) ON CONFLICT(window_id,sensor)
                            DO UPDATE SET first_contact=excluded.first_contact WHERE first_contact IS NULL
                        """, (self.window, sensor, at, at))
                        db.execute("UPDATE comm_events SET ended=?,outcome='retomada' WHERE sensor=? AND ended IS NULL",
                                   (at, sensor))
                        if can_observe:
                            freshness.enroll(db,sensor,self.window,at)
                if can_observe:
                    freshness.inspect_watches(db,self.window,at)
        self.window = next_window


def report(db, sensor=None, live=False, *, now=None):
    window = db.execute("SELECT * FROM comm_windows ORDER BY id DESC LIMIT 1").fetchone()
    observing = bool(window and not window["closed"])
    if observing and live:
        observing = stamp(now or datetime.now(UTC)) < offset(window["seen"], TIMEOUT)
    where = "" if sensor is None else " WHERE names.sensor=?"
    args = () if sensor is None else (sensor,)
    contacts = db.execute("""
        WITH names AS (SELECT sensor FROM comm_contacts UNION SELECT sensor FROM thermal_state)
        SELECT names.sensor,c.last_contact,c.window_id,e.detected FROM names
        LEFT JOIN comm_contacts c ON c.sensor=names.sensor
        LEFT JOIN comm_events e ON e.sensor=names.sensor AND e.ended IS NULL
    """ + where + " ORDER BY names.sensor", args)
    sensors = []
    for row in contacts:
        if not row["last_contact"]:
            state = "never"
        elif not observing:
            state = "unknown"
        elif row["detected"]:
            state = "offline"
        elif row["window_id"] == window["id"]:
            state = "online"
        else:
            state = "waiting"
        sensors.append({"sensor": row["sensor"], "last_contact": row["last_contact"], "state": state})
    if sensor is None:
        open_sql = "SELECT * FROM comm_events WHERE ended IS NULL ORDER BY id DESC"
        events = db.execute(open_sql if observing else open_sql + " LIMIT 50").fetchall()
        events += db.execute("""
            SELECT * FROM comm_events WHERE ended IS NOT NULL ORDER BY ended DESC, id DESC LIMIT 50
        """).fetchall()
    else:
        events = db.execute("SELECT * FROM comm_events WHERE sensor=? ORDER BY id DESC", (sensor,)).fetchall()
    gaps_sql = """SELECT a.seen AS started,b.started AS ended FROM comm_windows a
                  JOIN comm_windows b ON b.id=a.id+1 WHERE b.started>a.seen ORDER BY b.id DESC"""
    gaps = [dict(row) for row in db.execute(gaps_sql + (" LIMIT 20" if sensor is None else ""))]
    if window and not observing:
        gaps.insert(0, {"started": window["seen"], "ended": None})
    events = [dict(e) for e in events]
    if window and not observing:
        for event in events:
            if event["ended"] is None:
                event.update(ended=window["seen"], outcome="sem_observacao")
        if sensor is None:
            # Um encerramento projetado na consulta e o mesmo encerramento já gravado têm a mesma ordem.
            events.sort(key=lambda e: (e["ended"], e["id"]), reverse=True)
            events = events[:50]
    return {"seen": window["seen"] if window else None, "observing": observing, "sensors": sensors,
            "events": events, "gaps": gaps}
