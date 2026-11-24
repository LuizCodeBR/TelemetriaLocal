"""Atualidade das medições no instante em que a central tem evidência."""
from . import alerts

SCHEMA = """
    CREATE INDEX IF NOT EXISTS freshness_valid_reading ON readings(sensor,measured_at DESC,received_at,id)
        WHERE measured_at<=received_at;
    CREATE TABLE IF NOT EXISTS freshness_watch (
        sensor TEXT PRIMARY KEY, window_id INTEGER NOT NULL, since TEXT NOT NULL, checked TEXT NOT NULL,
        measured TEXT, reading_id TEXT, enrolled INTEGER NOT NULL DEFAULT 1 CHECK(enrolled IN (0,1)),
        CHECK(checked>=since),
        CHECK((measured IS NULL)=(reading_id IS NULL))
    );
    CREATE INDEX IF NOT EXISTS freshness_last_check ON freshness_watch(checked);
    CREATE TABLE IF NOT EXISTS freshness_events (
        id INTEGER PRIMARY KEY, sensor TEXT NOT NULL, window_id INTEGER NOT NULL, detected TEXT NOT NULL,
        threshold REAL NOT NULL CHECK(threshold>0), baseline TEXT, ended TEXT,
        outcome TEXT CHECK(outcome IN ('atualizada','sem_observacao')),
        recovery_id TEXT, recovery_measured TEXT, recovery_received TEXT,
        CHECK(ended IS NULL OR ended>=detected),
        CHECK((outcome IS NULL)=(ended IS NULL)),
        CHECK((recovery_id IS NOT NULL)=(outcome IS 'atualizada')),
        CHECK((recovery_measured IS NULL)=(recovery_id IS NULL)),
        CHECK((recovery_received IS NULL)=(recovery_id IS NULL))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS freshness_one_open ON freshness_events(sensor) WHERE ended IS NULL;
    CREATE INDEX IF NOT EXISTS freshness_history ON freshness_events(sensor,detected DESC,id DESC);
    CREATE INDEX IF NOT EXISTS freshness_recent ON freshness_events(detected DESC,id DESC);
    CREATE INDEX IF NOT EXISTS freshness_closed_time ON freshness_events(ended DESC,id DESC) WHERE ended IS NOT NULL;
    CREATE TABLE IF NOT EXISTS freshness_break (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), window_id INTEGER NOT NULL, noticed TEXT NOT NULL
    );
"""


# O fim de uma ocorrência aberta é limitado à evidência da sua própria janela.
INTERVALS = """SELECT e.*,coalesce(e.ended,max(c.seen,coalesce(w.checked,c.seen))) AS until
               FROM freshness_events e JOIN comm_windows c ON c.id=e.window_id
               LEFT JOIN freshness_watch w ON w.sensor=e.sensor AND w.window_id=e.window_id"""


def period_events(db, sensor, start, end, stagnant):
    rows = db.execute("SELECT detected AS start,until,outcome FROM (" + INTERVALS + """)
                       WHERE sensor=:sensor AND detected<:end AND
                         (until>:start OR detected>=:start OR (until=:start AND outcome='atualizada'))
                       ORDER BY detected,id""",{"sensor":sensor,"start":start,"end":end})
    return [{"start":r["start"],"until":r["until"],
             "outcome":r["outcome"] or ("sem_observacao" if stagnant else None)} for r in rows]


def latest_valid(db, sensor, at):
    return db.execute("""SELECT id,measured_at FROM readings WHERE sensor=?
                         AND received_at<=? AND measured_at<=received_at
                         ORDER BY measured_at DESC,received_at,id LIMIT 1""",(sensor,at)).fetchone()


def paused(db, window, observed):
    """Uma leitura na lacuna já confirmou a perda da observação em todas as salas."""
    return db.execute("""SELECT 1 FROM freshness_break
                         WHERE singleton=1 AND window_id=? AND noticed>?""", (window,observed)).fetchone() is not None


def enroll(db, sensor, window, at):
    previous = db.execute("SELECT window_id,checked,enrolled FROM freshness_watch WHERE sensor=?", (sensor,)).fetchone()
    if previous and previous["window_id"] == window and previous["enrolled"]:
        return
    since = max(at, previous["checked"]) if previous else at
    last = latest_valid(db, sensor, since)
    db.execute("""INSERT INTO freshness_watch(sensor,window_id,since,checked,measured,reading_id) VALUES(?,?,?,?,?,?)
                  ON CONFLICT(sensor) DO UPDATE SET window_id=excluded.window_id,since=excluded.since,
                  checked=excluded.checked,measured=excluded.measured,reading_id=excluded.reading_id,enrolled=1""",
               (sensor,window,since,since,last["measured_at"] if last else None,last["id"] if last else None))


def inspect_watches(db, window, at, sensor=None):
    sql = """SELECT w.*,coalesce(r.gap_seconds,?) AS threshold FROM freshness_watch w
             LEFT JOIN thermal_rules r USING(sensor) LEFT JOIN freshness_events e
             ON e.sensor=w.sensor AND e.ended IS NULL WHERE w.window_id=? AND w.enrolled=1 AND e.id IS NULL"""
    args = [alerts.Rule().gap_seconds,window]
    if sensor is not None:
        sql += " AND w.sensor=?"
        args.append(sensor)
    for row in db.execute(sql,args).fetchall():
        checked = max(at,row["checked"])
        baseline = max(row["since"],row["measured"] or row["since"])
        if alerts.seconds_between(baseline,checked) > row["threshold"]:
            db.execute("""INSERT INTO freshness_events(sensor,window_id,detected,threshold,baseline)
                          VALUES(?,?,?,?,?)""",(row["sensor"],window,checked,row["threshold"],row["measured"]))


def accept_reading(db, reading, received, silence_limit):
    window = db.execute("SELECT id,started,seen,closed FROM comm_windows ORDER BY id DESC LIMIT 1").fetchone()
    if not window or window["closed"] or received < window["started"]:
        return
    if paused(db, window["id"], window["seen"]):
        return
    if alerts.seconds_between(window["seen"],received) >= silence_limit:
        close_window(db, window["id"], window["seen"])
        db.execute("""INSERT INTO freshness_break(singleton,window_id,noticed) VALUES(1,?,?)
                      ON CONFLICT(singleton) DO UPDATE SET window_id=excluded.window_id,noticed=excluded.noticed""",
                   (window["id"],received))
        return
    enroll(db, reading.sensor, window["id"], max(received,window["seen"]))
    watch = db.execute("SELECT measured,checked FROM freshness_watch WHERE sensor=?", (reading.sensor,)).fetchone()
    checked = max(received,window["seen"],watch["checked"])
    if reading.measured_at <= received and (not watch["measured"] or reading.measured_at > watch["measured"]):
        db.execute("UPDATE freshness_watch SET measured=?,reading_id=? WHERE sensor=?",
                   (reading.measured_at,reading.id,reading.sensor))
        age = alerts.seconds_between(reading.measured_at,checked)
        db.execute("""UPDATE freshness_events SET ended=?,outcome='atualizada',
                      recovery_id=?,recovery_measured=?,recovery_received=?
                      WHERE sensor=? AND ended IS NULL AND threshold>=?""",
                   (checked,reading.id,reading.measured_at,received,reading.sensor,age))
    db.execute("UPDATE freshness_watch SET checked=? WHERE sensor=?", (checked,reading.sensor))
    inspect_watches(db,window["id"],checked,reading.sensor)


def close_window(db, window, at):
    db.execute("UPDATE freshness_watch SET checked=max(checked,?),enrolled=0 WHERE window_id=? AND enrolled=1", (at,window))
    db.execute("""UPDATE freshness_events SET ended=(SELECT checked FROM freshness_watch w
                  WHERE w.sensor=freshness_events.sensor),outcome='sem_observacao'
                  WHERE window_id=? AND ended IS NULL""",(window,))


def room_states(db, comm):
    window = db.execute("SELECT id,seen FROM comm_windows ORDER BY id DESC LIMIT 1").fetchone()
    interrupted = window and paused(db,window["id"],window["seen"])
    result = {}
    for contact in comm["sensors"]:
        name = contact["sensor"]
        rule = alerts.rule_for(db,name)
        row = db.execute("""SELECT w.*,e.threshold FROM freshness_watch w
                            LEFT JOIN freshness_events e ON e.sensor=w.sensor AND e.ended IS NULL
                            WHERE w.sensor=?""",(name,)).fetchone()
        measured = row["measured"] if row else None
        current = bool(row and row["enrolled"] and window and row["window_id"] == window["id"])
        checked = max(row["checked"],comm["seen"]) if current else None
        threshold = row["threshold"] if current and row["threshold"] is not None else rule.gap_seconds
        state = "aguardando"
        if not comm["observing"] or interrupted:
            state = "sem_observacao"
        elif current and row["threshold"] is not None:
            state = "desatualizada"
        elif current and measured and alerts.seconds_between(measured,checked) <= threshold:
            state = "atualizada"
        result[name] = {**contact,"freshness":state,"measured":measured,"checked":checked,"threshold":threshold}
    return result
