"""Ensaios de persistência e disputa de escrita do histórico de comunicação."""
import asyncio
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import mqttools

from telemetria.communication import Observer, exclusive_observer, report, stamp
from telemetria.demo_communication import wait_contact
from telemetria.storage import History
from telemetria.transport import PRESENCE_REQUEST, client, station, topic

ROOT = Path(__file__).resolve().parents[1]


def at(second):
    return datetime(2026, 1, 15, 14, tzinfo=UTC) + timedelta(seconds=second)


class TracedHistory(History):
    before_sql = None

    @contextmanager
    def connect(self):
        with super().connect() as db:
            if self.before_sql:
                db.set_trace_callback(self.before_sql)
            yield db


class CommunicationReviewTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "central.sqlite3"
        self.h = TracedHistory(self.path)

    def open_incident(self):
        m = Observer(self.h)
        m.start(at(0))
        m.advance(at(0), "a", "inicial")
        for t in (5, 10, 15):
            m.advance(at(t))
        return m

    def test_iniciar_durante_escrita_longa_registra_quando_conseguiu_observar(self):
        previous = Observer(self.h)
        previous.start(at(-10))
        previous.stop()
        attempted = threading.Event()

        async def check():
            moment = at(0)

            async def start_and_read():
                async with station(self.h, 0, clock=lambda: moment):
                    return self.h.communication()

            with self.h.connect() as writer:
                writer.execute("BEGIN IMMEDIATE")
                self.h.before_sql = lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None
                task = asyncio.create_task(start_and_read())
                try:
                    self.assertTrue(await asyncio.to_thread(attempted.wait, 5))
                    moment = at(40)
                finally:
                    self.h.before_sql = None
            data = await asyncio.wait_for(task, 8)
            self.assertEqual(data["gaps"], [{"started": stamp(at(-10)), "ended": stamp(at(40))}])
            self.assertEqual(data["seen"], stamp(at(40)))

        asyncio.run(check())

    def test_retomada_pendente_de_escrita_longa_requer_uma_nova_rodada(self):
        received = threading.Event()
        allow_write = threading.Event()
        attempts = 0
        retried = threading.Event()
        advance = Observer.advance

        def count_attempt(sql):
            nonlocal attempts
            if sql == "BEGIN IMMEDIATE":
                attempts += 1
                if attempts >= 2:
                    retried.set()

        def observe(monitor, now, sensor=None, round_id=None):
            if sensor == "a" and now == at(16):
                received.set()
                if not allow_write.wait(6):
                    raise RuntimeError("O escritor de teste não foi preparado")
            return advance(monitor, now, sensor, round_id)

        async def check():
            moment = at(0)
            async with station(self.h, 0, clock=lambda: moment, check_interval=.02) as port:
                async with client(port, PRESENCE_REQUEST) as sensor:
                    async with asyncio.timeout(10):
                        first = (await sensor.messages.get()).message
                        sensor.publish(mqttools.Message(topic("a", "presenca"), first))
                        await wait_contact(self.h, "a", moment)
                        for second in (5, 10, 15):
                            moment = at(second)
                            current = (await sensor.messages.get()).message
                        moment = at(16)
                        sensor.publish(mqttools.Message(topic("a", "presenca"), current))
                        try:
                            self.assertTrue(await asyncio.to_thread(received.wait, 5))
                            with self.h.connect() as writer:
                                writer.execute("BEGIN IMMEDIATE")
                                self.h.before_sql = count_attempt
                                allow_write.set()
                                self.assertTrue(await asyncio.to_thread(retried.wait, 5))
                                before = self.h.communication("a")
                                self.assertIsNone(before["events"][0]["ended"])
                                moment = at(40)
                        finally:
                            self.h.before_sql = None
                            allow_write.set()
                        fresh = (await sensor.messages.get()).message
                        self.assertNotEqual(fresh, current)
                        data = self.h.communication("a")
                        event, = data["events"]
                        self.assertEqual((event["ended"], event["outcome"]), (before["seen"], "sem_observacao"))
                        self.assertEqual(data["gaps"], [{"started": before["seen"], "ended": stamp(at(40))}])
                        self.assertEqual(data["sensors"][0]["state"], "waiting")
                        sensor.publish(mqttools.Message(topic("a", "presenca"), fresh))
                        await wait_contact(self.h, "a", moment)
                        self.assertEqual(self.h.communication("a")["events"], [event])

        with patch.object(Observer, "advance", observe), patch("telemetria.storage.BUSY_TIMEOUT", 0):
            asyncio.run(check())

    def test_painel_prioriza_retomada_recente_de_interrupcao_antiga(self):
        m = self.open_incident()
        m.advance(at(15), "b", "primeira")
        t = 15
        for i in range(52):
            m.advance(at(t + 8))
            t += 16
            m.advance(at(t), "b", str(i))
        m.advance(at(t + 1), "a", "retorno")
        panel = self.h.snapshot()["communication"]["events"]
        self.assertEqual(len(panel), 50)
        self.assertEqual((panel[0]["sensor"], panel[0]["ended"]), ("a", stamp(at(t + 1))))
        self.assertEqual(len(self.h.communication("b")["events"]), 52)

    def test_painel_sem_observacao_respeita_limite_e_desempate_apos_reinicio(self):
        m = Observer(self.h)
        m.start(at(0))
        for i in range(53):
            m.advance(at(0), f"sala-{i:02}", "primeira")
        m.advance(at(9))
        m.advance(at(15))
        self.assertEqual(len(self.h.communication()["events"]), 53)
        with patch("telemetria.communication.datetime", wraps=datetime) as clock:
            clock.now.return_value = at(30)
            stale = self.h.snapshot(live=True)["communication"]["events"]
        self.assertEqual(len(stale), 50)
        self.assertTrue(all(e["outcome"] == "sem_observacao" for e in stale))
        m.stop()
        Observer(self.h).start(at(40))
        self.assertEqual(self.h.snapshot()["communication"]["events"], stale)
        self.assertEqual(len(self.h.communication("sala-00")["events"]), 1)

    def test_espera_curta_preserva_horario_real_da_resposta(self):
        moment = at(16)
        m = self.open_incident()
        m.clock = lambda: moment
        attempting = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.h.connect() as writer:
                writer.execute("BEGIN IMMEDIATE")
                self.h.before_sql = lambda sql: attempting.set() if sql == "BEGIN IMMEDIATE" else None
                pending = pool.submit(m.advance, at(16), "a", "retorno")
                self.assertTrue(attempting.wait(5))
                moment = at(22)
                self.h.before_sql = None
            pending.result(timeout=6)
        data = self.h.communication("a")
        event, = data["events"]
        self.assertEqual((event["ended"], event["outcome"]), (stamp(at(16)), "retomada"))
        self.assertEqual(data["sensors"][0]["last_contact"], stamp(at(16)))
        self.assertEqual(data["gaps"], [])

    def test_duas_aberturas_do_banco_anterior_preservam_historico_e_criam_indice(self):
        m = self.open_incident()
        m.advance(at(18), "a", "retorno")
        m.advance(at(26))
        m.advance(at(33))
        before = self.h.communication()
        with self.h.connect() as db:
            db.execute("DROP INDEX comm_recent_end")
            db.execute("PRAGMA user_version=2")
        start = threading.Barrier(2)

        def upgrade():
            start.wait(timeout=5)
            return History(self.path).communication()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(upgrade) for _ in range(2)]
            for future in futures:
                self.assertEqual(future.result(timeout=8), before)
        with self.h.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            indices = {r["name"] for r in db.execute("PRAGMA index_list(comm_events)")}
            self.assertIn("comm_recent_end", indices)

    def test_leitor_durante_retomada_ve_apenas_transacoes_completas(self):
        m = self.open_incident()
        with self.h.connect() as reader:
            reader.execute("BEGIN")
            first = report(reader, "a")
            m.advance(at(18), "a", "retorno")
            self.assertEqual(report(reader, "a"), first)
        self.assertEqual(first["sensors"][0]["state"], "offline")
        self.assertIsNone(first["events"][0]["ended"])
        latest = self.h.communication("a")
        self.assertEqual(latest["sensors"][0]["state"], "online")
        self.assertEqual(latest["events"][0]["ended"], latest["sensors"][0]["last_contact"])

    def test_saida_abrupta_no_meio_da_retomada_e_depois_do_commit(self):
        script = """
import os, sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from telemetria.communication import Observer, exclusive_observer
from telemetria.storage import History

class CrashHistory(History):
    @contextmanager
    def connect(self):
        with super().connect() as db:
            db.create_function('interromper_teste', 0, lambda: os._exit(41))
            yield db

path, mode = sys.argv[1:]
base = datetime(2026, 1, 15, 14, tzinfo=UTC)
with exclusive_observer(path):
    h = CrashHistory(path)
    m = Observer(h)
    m.start(base)
    m.advance(base, 'a', 'inicio')
    for t in (8, 15):
        m.advance(base + timedelta(seconds=t))
    if mode == 'antes':
        with h.connect() as db:
            db.execute('CREATE TRIGGER interromper BEFORE UPDATE OF ended ON comm_events '
                       'BEGIN SELECT interromper_teste(); END')
    m.advance(base + timedelta(seconds=18), 'a', 'retorno')
    os._exit(42)
"""
        for mode, code in (("antes", 41), ("depois", 42)):
            with self.subTest(mode=mode):
                path = self.path.with_name(f"{mode}.sqlite3")
                proc = subprocess.run([sys.executable, "-B", "-c", script, str(path), mode],
                                      cwd=ROOT, capture_output=True, timeout=10)
                self.assertEqual(proc.returncode, code, proc.stderr)
                with exclusive_observer(path):
                    history = History(path)
                    with history.connect() as db:
                        db.execute("DROP TRIGGER IF EXISTS interromper")
                        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    Observer(history).start(at(40))
                    data = history.communication("a")
                event, = data["events"]
                if mode == "antes":
                    self.assertEqual((event["ended"], event["outcome"]), (stamp(at(15)), "sem_observacao"))
                    self.assertEqual(data["sensors"][0]["last_contact"], stamp(at(0)))
                else:
                    self.assertEqual((event["ended"], event["outcome"]), (stamp(at(18)), "retomada"))
                    self.assertEqual(data["sensors"][0]["last_contact"], stamp(at(18)))
                self.assertEqual(data["gaps"][0], {"started": event["ended"], "ended": stamp(at(40))})

    def test_outro_processo_nao_adquire_a_mesma_central(self):
        code = """
import sys
from telemetria.communication import exclusive_observer
try:
    with exclusive_observer(sys.argv[1]):
        print('adquiriu')
except OSError as error:
    print(str(error))
"""
        def attempt():
            result = subprocess.run([sys.executable, "-B", "-c", code, str(self.path)],
                                    cwd=ROOT, capture_output=True, check=True, timeout=8)
            return result.stdout.decode("utf-8").strip()

        with exclusive_observer(self.path):
            self.assertIn("Outra central", attempt())
        self.assertEqual(attempt(), "adquiriu")
