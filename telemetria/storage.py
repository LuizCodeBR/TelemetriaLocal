"""Contrato da leitura, fila do sensor e histórico do coletor."""
import asyncio
import json
import logging
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


from . import alerts, communication, freshness, maintenance


BUSY_TIMEOUT = 5


class ReadingConflict(ValueError):
    """A mesma identidade já tem outro conteúdo persistido."""


async def database_call(operation, *args, **kwargs):
    """Tenta novamente somente em caso de contenção temporária do SQLite."""
    delay = 0.1
    while True:
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Aguarda o fechamento desta conexão. Um commit sem recibo admite reenvio.
            await asyncio.gather(task, return_exceptions=True)
            raise
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0)
            if (code & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            if delay == 0.1:
                logging.getLogger(__name__).warning("SQLite ocupado em %s. Aguardando liberação.", operation.__qualname__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1)


def utc_now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def valid_sensor(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value) is not None


def json_bytes(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


@dataclass(frozen=True)
class Reading:
    sensor: str
    id: str
    measured_at: str
    temperature: float
    humidity: float

    def __post_init__(self):
        if not valid_sensor(self.sensor) or not isinstance(self.id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise ValueError("Identificação inválida")
        if not isinstance(self.measured_at, str):
            raise ValueError("Horário UTC inválido")
        moment = datetime.fromisoformat(self.measured_at)
        if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
            raise ValueError("A medição precisa de horário UTC")
        object.__setattr__(self, "measured_at", moment.astimezone(UTC).isoformat(timespec="microseconds"))
        for name, lower, upper in (("temperature", -80, 150), ("humidity", 0, 100)):
            value = getattr(self, name)
            if type(value) not in (float, int) or not lower <= value <= upper:
                raise ValueError(f"Medida inválida: {name}")
            object.__setattr__(self, name, float(value) or 0.0)

    def encode(self):
        return json_bytes(asdict(self))

    @classmethod
    def decode(cls, payload):
        if len(payload) > 2048:
            raise ValueError("Leitura grande demais")
        record = json.loads(payload)
        if not isinstance(record, dict) or record.keys() != cls.__dataclass_fields__.keys():
            raise ValueError("Campos de leitura inválidos")
        return cls(**record)

    @classmethod
    def sample(cls, sensor, moment=None):
        moment = moment or datetime.now(UTC)
        # Duas ondas lentas simulam variações ambientais sem hardware.
        t = moment.timestamp()
        return cls(sensor, uuid4().hex, moment.isoformat(),
                   round(24 + 2.4 * math.sin(t / 30), 2),
                   round(52 + 7.5 * math.cos(t / 45), 2))


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            self.initialize(db)

    def initialize(self, db):
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(self.schema)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=BUSY_TIMEOUT)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.row_factory = sqlite3.Row
            with db:
                yield db


class Outbox(Database):
    schema = """
        CREATE TABLE IF NOT EXISTS pending (
            sensor TEXT NOT NULL, id TEXT NOT NULL, payload BLOB NOT NULL,
            PRIMARY KEY (sensor, id)
        );
        CREATE INDEX IF NOT EXISTS pending_by_sensor ON pending(sensor);
        CREATE TABLE IF NOT EXISTS conflicts (
            sensor TEXT NOT NULL, id TEXT NOT NULL, marked_at TEXT NOT NULL,
            PRIMARY KEY (sensor, id)
        );
    """

    active = """NOT EXISTS (
        SELECT 1 FROM conflicts WHERE conflicts.sensor=pending.sensor AND conflicts.id=pending.id
    )"""

    def put(self, reading):
        payload = reading.encode()
        key = (reading.sensor, reading.id)
        with self.connect() as db:
            inserted = db.execute("""
                INSERT INTO pending (sensor, id, payload) VALUES (?, ?, ?)
                ON CONFLICT(sensor, id) DO NOTHING
            """, (*key, payload))
            if not inserted.rowcount:
                stored = db.execute("SELECT payload FROM pending WHERE sensor=? AND id=?", key).fetchone()
                if stored["payload"] != payload:
                    raise ReadingConflict("Mesmo identificador com conteúdo diferente na fila")

    def batch(self, sensor, limit=50):
        with self.connect() as db:
            rows = db.execute(f"SELECT payload FROM pending WHERE sensor=? AND {self.active} ORDER BY rowid LIMIT ?",
                              (sensor, limit))
            return [Reading.decode(row["payload"]) for row in rows]

    def acknowledge(self, reading):
        with self.connect() as db:
            deleted = db.execute(f"DELETE FROM pending WHERE sensor=? AND id=? AND payload=? AND {self.active}",
                                 (reading.sensor, reading.id, reading.encode()))
            return deleted.rowcount == 1

    def mark_conflict(self, reading):
        # A cópia original fica na fila. A marca persistente a retira dos próximos lotes.
        with self.connect() as db:
            db.execute("""
                INSERT INTO conflicts (sensor, id, marked_at)
                SELECT sensor, id, ? FROM pending WHERE sensor=? AND id=? AND payload=?
                ON CONFLICT(sensor, id) DO NOTHING
            """, (utc_now(), reading.sensor, reading.id, reading.encode()))

    def conflicts(self, sensor):
        with self.connect() as db:
            rows = db.execute("""
                SELECT pending.payload, conflicts.marked_at FROM pending
                JOIN conflicts USING (sensor, id) WHERE pending.sensor=?
                ORDER BY marked_at DESC, pending.id LIMIT 50
            """, (sensor,))
            return [(Reading.decode(row["payload"]), row["marked_at"]) for row in rows]

    def count(self, sensor):
        with self.connect() as db:
            return db.execute(f"SELECT count(*) FROM pending WHERE sensor=? AND {self.active}", (sensor,)).fetchone()[0]


class History(Database):
    def __init__(self, path, *, received_clock=None):
        self.received_clock = received_clock
        super().__init__(path)

    receipt_index = "CREATE INDEX IF NOT EXISTS readings_by_receipt ON readings(received_at, sensor, id)"
    schema = """
        CREATE TABLE IF NOT EXISTS readings (
            sensor TEXT NOT NULL, id TEXT NOT NULL, measured_at TEXT NOT NULL,
            temperature REAL NOT NULL, humidity REAL NOT NULL, received_at TEXT NOT NULL,
            PRIMARY KEY (sensor, id)
        );
        CREATE INDEX IF NOT EXISTS readings_by_time ON readings(measured_at DESC, sensor, id);
    """ + receipt_index + ";\n" + alerts.SCHEMA + communication.SCHEMA + maintenance.SCHEMA + freshness.SCHEMA

    @staticmethod
    def _schema_version(db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9):
            raise ValueError(f"Versão de banco incompatível com esta central: {version}.")
        return version

    def initialize(self, db):
        version = self._schema_version(db)
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            db.execute("PRAGMA journal_mode=WAL")
        if version == 9:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='readings_by_receipt'").fetchone():
                db.execute(self.receipt_index)
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='comm_actions_by_time'").fetchone():
                db.execute(maintenance.ACTION_TIME_INDEX)
            return
        # Inclui o DDL e o recálculo inicial no mesmo commit. Outro processo pode
        # concluir a migração enquanto este aguarda a reserva de escrita.
        db.executescript("BEGIN IMMEDIATE;\n" + self.schema)
        version = self._schema_version(db)
        if version == 0:
            for row in db.execute("SELECT DISTINCT sensor FROM readings"):
                alerts.rebuild(db, row["sensor"], alerts.rule_for(db, row["sensor"]))
        if version < 4:
            communication.restore_evidence(db)
        if version < 6:
            db.execute("""INSERT INTO comm_incident_identity(event_id)
                          SELECT e.id FROM comm_events e WHERE NOT EXISTS
                          (SELECT 1 FROM comm_incident_identity i WHERE i.event_id=e.id)""")
        if "enrolled" not in {row["name"] for row in db.execute("PRAGMA table_info(freshness_watch)")}:
            db.execute("ALTER TABLE freshness_watch ADD COLUMN enrolled INTEGER NOT NULL DEFAULT 1 CHECK(enrolled IN (0,1))")
        db.execute("PRAGMA user_version=9")

    def configure(self, sensor, *, high=None, recovery=None, gap_seconds=None):
        if not valid_sensor(sensor):
            raise ValueError("Identificação de sensor inválida.")
        changes = {k: v for k, v in {"high": high, "recovery": recovery, "gap_seconds": gap_seconds}.items()
                   if v is not None}
        with self.connect() as db:
            if changes:
                db.execute("BEGIN IMMEDIATE")
            previous = alerts.rule_for(db, sensor)
            rule = replace(previous, **changes)
            if rule != previous:
                db.execute("""
                    INSERT INTO thermal_rules VALUES(?,?,?,?) ON CONFLICT(sensor) DO UPDATE SET
                    high=excluded.high, recovery=excluded.recovery, gap_seconds=excluded.gap_seconds
                """, (sensor, rule.high, rule.recovery, rule.gap_seconds))
                alerts.rebuild(db, sensor, rule)
            return rule

    def occurrences(self, sensor):
        with self.connect() as db:
            db.execute("BEGIN")
            return alerts.report(db, sensor)

    @staticmethod
    def _already_recorded(db, reading):
        row = db.execute("SELECT sensor,id,measured_at,temperature,humidity FROM readings WHERE sensor=? AND id=?",
                         (reading.sensor, reading.id)).fetchone()
        if row is None:
            return False
        if Reading(**dict(row)) != reading:
            raise ReadingConflict("Mesmo identificador com conteúdo diferente")
        return True

    def record(self, reading):
        with self.connect() as db:
            if self._already_recorded(db, reading):
                return "repetida"
            db.execute("BEGIN IMMEDIATE")
            # A consulta inicial pode ter coincidido com uma inserção ainda não confirmada.
            if self._already_recorded(db, reading):
                return "repetida"
            received = communication.stamp(self.received_clock()) if self.received_clock else utc_now()
            db.execute("""
                INSERT INTO readings VALUES (:sensor, :id, :measured_at, :temperature, :humidity, :received_at)
            """, {**asdict(reading), "received_at":received})
            alerts.apply_reading(db, reading)
            freshness.accept_reading(db,reading,received,communication.TIMEOUT)
            return "gravada"

    def communication(self, sensor=None, *, live=False):
        with self.connect() as db:
            db.execute("BEGIN")
            return communication.report(db, sensor, live)

    def snapshot(self, *, live=False):
        with self.connect() as db:
            db.execute("BEGIN")
            stats = db.execute("SELECT count(*) AS total, max(received_at) AS last_received FROM readings").fetchone()
            rows = db.execute("""
                SELECT * FROM readings ORDER BY measured_at DESC, sensor, temperature DESC, id LIMIT 60
            """).fetchall()
            trend = []
            if rows:
                trend = db.execute("""
                    SELECT measured_at, MAX(temperature) AS temperature FROM readings WHERE sensor=?
                    GROUP BY measured_at ORDER BY measured_at DESC LIMIT 60
                """, (rows[0]["sensor"],)).fetchall()
            defaults = alerts.Rule()
            rules = db.execute("""
                WITH sensors AS (SELECT sensor FROM thermal_state UNION SELECT sensor FROM thermal_rules)
                SELECT sensor, coalesce(high, ?) AS high, coalesce(recovery, ?) AS recovery,
                       coalesce(gap_seconds, ?) AS gap_seconds, last_at
                FROM sensors LEFT JOIN thermal_rules USING(sensor) LEFT JOIN thermal_state USING(sensor) ORDER BY sensor
            """, (defaults.high, defaults.recovery, defaults.gap_seconds))
            comm = communication.report(db,live=live)
            return dict(stats) | {"freshness":freshness.room_states(db,comm), "rows": [dict(row) for row in rows], "trend": [dict(row) for row in reversed(trend)],
                                  "rules": [dict(row) for row in rules],
                                  "maintenance": maintenance.active_by_room(db, maintenance.clock(db, None)) if live else {},
                                  "communication": comm} | alerts.report(db)
