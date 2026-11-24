"""Ocorrências térmicas derivadas da série de cada sensor, em horário de medição."""
from dataclasses import dataclass
from datetime import datetime
from math import isfinite


SCHEMA = """
    CREATE INDEX IF NOT EXISTS readings_sensor_time ON readings(sensor, measured_at);
    CREATE TABLE IF NOT EXISTS thermal_rules (
        sensor TEXT PRIMARY KEY, high REAL NOT NULL, recovery REAL NOT NULL, gap_seconds REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS thermal_state (
        sensor TEXT PRIMARY KEY, last_at TEXT NOT NULL, streak INTEGER NOT NULL, start_at TEXT, peak REAL
    );
    CREATE TABLE IF NOT EXISTS thermal_events (
        sensor TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, status TEXT NOT NULL, peak REAL NOT NULL,
        PRIMARY KEY(sensor, started)
    );
    CREATE INDEX IF NOT EXISTS thermal_event_order ON thermal_events(started DESC, sensor);
    CREATE TABLE IF NOT EXISTS thermal_gaps (
        sensor TEXT NOT NULL, before_at TEXT NOT NULL, after_at TEXT NOT NULL, PRIMARY KEY(sensor, before_at)
    );
    CREATE INDEX IF NOT EXISTS thermal_gap_order ON thermal_gaps(after_at DESC, sensor);
"""


@dataclass(frozen=True)
class Rule:
    high: float = 28.0
    recovery: float = 26.0
    gap_seconds: float = 10.0

    def __post_init__(self):
        if not (-80 <= self.recovery < self.high <= 150):
            raise ValueError("Os limites devem estar entre -80 e 150 °C e a recuperação deve ser menor que a alta.")
        if not isfinite(self.gap_seconds) or self.gap_seconds <= 0:
            raise ValueError("O intervalo máximo deve ser positivo e finito.")


def rule_for(db, sensor):
    row = db.execute("SELECT high,recovery,gap_seconds FROM thermal_rules WHERE sensor=?", (sensor,)).fetchone()
    return Rule(**dict(row)) if row else Rule()


def seconds_between(first, last):
    return (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()


@dataclass
class State:
    last_at: str | None = None
    streak: int = 0
    start_at: str | None = None
    peak: float | None = None

    def clear_sequence(self):
        self.streak, self.start_at, self.peak = 0, None, None

    def end(self, db, sensor, at, status):
        db.execute("UPDATE thermal_events SET ended=?, status=? WHERE sensor=? AND started=?",
                   (at, status, sensor, self.start_at))
        self.clear_sequence()

    def observe(self, db, sensor, rule, at, value):
        if self.last_at and seconds_between(self.last_at, at) > rule.gap_seconds:
            db.execute("INSERT INTO thermal_gaps VALUES (?,?,?)", (sensor, self.last_at, at))
            if self.streak == 3:
                self.end(db, sensor, self.last_at, "lacuna")
            else:
                self.clear_sequence()
        self.last_at = at

        if self.streak == 3:
            if value <= rule.recovery:
                self.end(db, sensor, at, "recuperada")
            elif value > self.peak:
                self.peak = value
                db.execute("UPDATE thermal_events SET peak=? WHERE sensor=? AND started=?",
                           (value, sensor, self.start_at))
            return

        if value < rule.high:
            self.clear_sequence()
            return
        if self.streak == 0:
            self.start_at, self.peak = at, value
        self.peak = max(self.peak, value)
        self.streak += 1
        if self.streak == 3:
            db.execute("INSERT INTO thermal_events VALUES (?,?,NULL,'aberta',?)",
                       (sensor, self.start_at, self.peak))

    def save(self, db, sensor):
        db.execute("""
            INSERT INTO thermal_state VALUES(?,?,?,?,?) ON CONFLICT(sensor) DO UPDATE SET
            last_at=excluded.last_at, streak=excluded.streak, start_at=excluded.start_at, peak=excluded.peak
        """, (sensor, self.last_at, self.streak, self.start_at, self.peak))


def replay(db, sensor, rule, rows):
    state = State()
    for at, value in rows:
        state.observe(db, sensor, rule, at, value)
    return state


def rebuild(db, sensor, rule):
    db.execute("DELETE FROM thermal_events WHERE sensor=?", (sensor,))
    db.execute("DELETE FROM thermal_gaps WHERE sensor=?", (sensor,))
    rows = db.execute("""
        SELECT measured_at, max(temperature) FROM readings WHERE sensor=?
        GROUP BY measured_at ORDER BY measured_at
    """, (sensor,))
    state = replay(db, sensor, rule, rows)
    if state.last_at:
        state.save(db, sensor)


def revise_interval(db, reading, rule, last_at):
    sensor, at = reading.sensor, reading.measured_at
    # Antes e depois de um instante cuja máxima atingiu a recuperação, nenhuma
    # sequência pode atravessar aquele instante. Empates usam todos os IDs.
    previous = db.execute("""
        SELECT measured_at FROM readings WHERE sensor=? AND measured_at < ?
        GROUP BY measured_at HAVING max(temperature) <= ? ORDER BY measured_at DESC LIMIT 1
    """, (sensor, at, rule.recovery)).fetchone()
    following = db.execute("""
        SELECT measured_at FROM readings WHERE sensor=? AND measured_at > ?
        GROUP BY measured_at HAVING max(temperature) <= ? ORDER BY measured_at LIMIT 1
    """, (sensor, at, rule.recovery)).fetchone()
    start = previous[0] if previous else db.execute(
        "SELECT min(measured_at) FROM readings WHERE sensor=?", (sensor,)).fetchone()[0]
    end = following[0] if following else last_at

    db.execute("DELETE FROM thermal_events WHERE sensor=? AND started BETWEEN ? AND ?", (sensor, start, end))
    # Uma lacuna que parte de end já pertence ao trecho seguinte.
    db.execute("DELETE FROM thermal_gaps WHERE sensor=? AND before_at>=? AND before_at<?", (sensor, start, end))
    rows = db.execute("""
        SELECT measured_at, max(temperature) FROM readings WHERE sensor=? AND measured_at BETWEEN ? AND ?
        GROUP BY measured_at ORDER BY measured_at
    """, (sensor, start, end))
    state = replay(db, sensor, rule, rows)
    if end == last_at:
        state.save(db, sensor)


def apply_reading(db, reading):
    rule = rule_for(db, reading.sensor)
    row = db.execute("SELECT last_at,streak,start_at,peak FROM thermal_state WHERE sensor=?",
                     (reading.sensor,)).fetchone()
    state = State(**dict(row)) if row else State()
    if state.last_at and reading.measured_at <= state.last_at:
        same_or_hotter = db.execute("""
            SELECT 1 FROM readings WHERE sensor=? AND measured_at=? AND id<>? AND temperature>=? LIMIT 1
        """, (reading.sensor, reading.measured_at, reading.id, reading.temperature)).fetchone()
        if not same_or_hotter:
            revise_interval(db, reading, rule, state.last_at)
    else:
        state.observe(db, reading.sensor, rule, reading.measured_at, reading.temperature)
        state.save(db, reading.sensor)


def report(db, sensor=None):
    # O painel mostra todas as abertas e as 50 últimas finalizadas. A consulta de
    # um sensor devolve seu histórico integral, inclusive lacunas mais antigas.
    base = """SELECT e.sensor,e.started,e.status,e.ended,e.peak,coalesce(e.ended,s.last_at) AS observed_until
              FROM thermal_events e JOIN thermal_state s ON s.sensor=e.sensor"""
    order = " ORDER BY e.started DESC, e.sensor"
    if sensor is None:
        opened = db.execute(base + " WHERE e.ended IS NULL" + order).fetchall()
        closed = db.execute(base + " WHERE e.ended IS NOT NULL" + order + " LIMIT 50").fetchall()
        events = opened + closed
        gaps = db.execute("SELECT * FROM thermal_gaps ORDER BY after_at DESC,sensor LIMIT 20")
    else:
        events = db.execute(base + " WHERE e.sensor=?" + order, (sensor,)).fetchall()
        gaps = db.execute("SELECT * FROM thermal_gaps WHERE sensor=? ORDER BY after_at DESC", (sensor,))
    return {"events": [dict(row) for row in events], "gaps": [dict(row) for row in gaps]}
