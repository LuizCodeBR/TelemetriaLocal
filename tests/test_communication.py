import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import mqttools

from telemetria.communication import Observer, exclusive_observer, stamp
from telemetria.demo_communication import demonstrate_communication, wait_contact
from telemetria.storage import History, Outbox, Reading
from telemetria.transport import PRESENCE_REQUEST, client, run_sensor, station, topic

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 1, 15, 14, tzinfo=UTC)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / ".cache" / "testes"
        root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.h = History(self.work / "central.sqlite3")
        self.m = Observer(self.h)
        self.m.start(at(0))

    def pulse(self, second, sensor="a", token=None):
        self.m.advance(at(second), sensor, str(second) if token is None else token)

    def data(self, sensor="a"):
        return self.h.communication(sensor)

    def test_prazo_exato_e_retorno_unico(self):
        self.pulse(0)
        for sec in (5, 10, 14.999999):
            self.m.advance(at(sec))
        self.assertEqual(self.data()["events"], [])
        self.m.advance(at(15))
        self.m.advance(at(20))
        self.m.advance(at(25))
        self.assertEqual(self.data()["sensors"][0]["state"], "offline")
        self.pulse(32)
        self.pulse(33, token="32")
        event, = self.data()["events"]
        self.assertEqual([event[k] for k in ("last_contact", "detected", "ended", "outcome")],
                         [stamp(at(0)), stamp(at(15)), stamp(at(32)), "retomada"])
        self.assertEqual(self.data()["sensors"][0]["last_contact"], stamp(at(32)))

    def test_sinal_exatamente_no_prazo_e_sinal_antes_do_prazo(self):
        self.pulse(0, "a")
        self.pulse(0, "b")
        self.m.advance(at(8))
        self.pulse(14.999999, "b")
        self.pulse(15, "a")
        event, = self.data()["events"]
        self.assertEqual(event["detected"], event["ended"])
        self.assertEqual(self.data("b")["events"], [])

    def test_temperatura_atrasada_e_duplicada_nao_significam_presenca(self):
        self.pulse(0)
        self.m.advance(at(9))
        self.m.advance(at(16))
        before = self.data()
        r = Reading.sample("a", at(3))
        self.assertEqual(self.h.record(r), "gravada")
        self.assertEqual(self.h.record(r), "repetida")
        self.assertEqual(self.data(), before)
        self.h.record(Reading.sample("sem-presenca", at(17)))
        self.assertEqual(self.data("sem-presenca")["sensors"][0]["state"], "never")
        self.assertFalse(self.data("sem-presenca")["events"])

    def test_reinicio_durante_interrupcao_delimita_tempo_observado(self):
        self.pulse(0)
        for sec in (5,10,15,20):
            self.m.advance(at(sec))
        self.m.stop()
        self.assertEqual(self.data()["sensors"][0]["state"], "unknown")
        self.m = Observer(History(self.h.path))
        self.m.start(at(100))
        self.assertEqual(self.data()["sensors"][0]["state"], "waiting")
        self.assertEqual(self.data()["gaps"], [{"started": stamp(at(20)), "ended": stamp(at(100))}])
        self.m.advance(at(110))
        self.m.advance(at(115))
        self.pulse(120)
        new, old = self.data()["events"]
        self.assertEqual((old["ended"],old["outcome"]), (stamp(at(20)),"sem_observacao"))
        self.assertEqual((new["detected"],new["ended"],new["last_contact"]), (stamp(at(115)), stamp(at(120)),None))

    def test_pausa_do_monitor_nao_vira_queda_do_sensor(self):
        self.pulse(0)
        self.m.advance(at(5))
        self.pulse(45)
        self.assertEqual(self.data()["events"], [])
        self.assertEqual(self.data()["sensors"][0]["state"], "waiting")
        self.assertEqual(self.data()["gaps"], [{"started": stamp(at(5)), "ended": stamp(at(45))}])
        self.pulse(46)
        self.assertEqual(self.data()["sensors"][0]["state"], "online")

    def test_consulta_ao_vivo_durante_parada_abrupta_usa_ultimo_ponto_salvo(self):
        self.pulse(0)
        for second in (8, 15, 18):
            self.m.advance(at(second))
        with patch("telemetria.communication.datetime", wraps=datetime) as clock:
            clock.now.return_value = at(33)
            live = self.h.communication("a", live=True)
        self.assertFalse(live["observing"])
        self.assertEqual(live["sensors"][0]["state"], "unknown")
        self.assertEqual(live["events"][0]["ended"], stamp(at(18)))
        self.assertEqual(live["events"][0]["outcome"], "sem_observacao")
        self.assertEqual(live["gaps"], [{"started": stamp(at(18)), "ended": None}])
        self.assertIsNone(self.data()["events"][0]["ended"])

    def test_interrupcao_abrupta_do_processo_preserva_ponto_de_observacao(self):
        path = self.work / "abrupta.sqlite3"
        script = """
import os, sys
from datetime import UTC,datetime,timedelta
from telemetria.storage import History
from telemetria.communication import Observer,exclusive_observer
base=datetime(2026,1,15,14,tzinfo=UTC)
with exclusive_observer(sys.argv[1]):
    m=Observer(History(sys.argv[1]))
    m.start(base)
    m.advance(base,'a','primeira')
    for sec in (5,10,15,18): m.advance(base+timedelta(seconds=sec))
    os._exit(23)
"""
        child = subprocess.run([sys.executable, "-B", "-c", script, str(path)], cwd=ROOT, capture_output=True, timeout=12)
        self.assertEqual(child.returncode, 23, child.stderr)
        h = History(path)
        with exclusive_observer(path):
            m = Observer(h)
            m.start(at(180))
            data = h.communication("a")
            self.assertEqual(data["gaps"][0], {"started": stamp(at(18)), "ended": stamp(at(180))})
            self.assertEqual(data["events"][0]["outcome"], "sem_observacao")
            self.assertEqual(data["events"][0]["ended"], stamp(at(18)))
            m.stop()

    def test_gravacao_atomica_do_retorno(self):
        self.pulse(0)
        self.m.advance(at(8))
        self.m.advance(at(16))
        before = self.data()
        with self.h.connect() as db:
            db.execute("""CREATE TRIGGER falha_no_retorno BEFORE UPDATE OF ended ON comm_events
                          BEGIN SELECT RAISE(ABORT,'simulado'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pulse(18)
        self.assertEqual(self.data(), before)
        with self.h.connect() as db:
            db.execute("DROP TRIGGER falha_no_retorno")
        self.pulse(18)
        event, = self.data()["events"]
        self.assertEqual(event["ended"], stamp(at(18)))

    def test_duas_centrais_na_mesma_base(self):
        with exclusive_observer(self.h.path):
            with self.assertRaises(OSError):
                with exclusive_observer(self.h.path):
                    self.fail("Duas centrais adquiriram o mesmo banco")
        with exclusive_observer(self.h.path):
            self.assertEqual(self.data()["sensors"], [])

    def test_migracao_do_esquema_um_nao_inventa_sinais(self):
        path = self.work / "anterior.sqlite3"
        h = History(path)
        r = Reading.sample("anterior", at(0))
        h.record(r)
        with closing(sqlite3.connect(path)) as db, db:
            for name in ("comm_events","comm_contacts","comm_windows"):
                db.execute(f"DROP TABLE {name}")
            db.execute("PRAGMA user_version=1")
        restored = History(path).snapshot()
        self.assertEqual(restored["total"], 1)
        self.assertEqual(restored["communication"]["sensors"][0]["state"], "never")
        self.assertEqual(restored["communication"]["gaps"], [])

    def test_historico_inteiro_por_sensor_e_resumo_no_painel(self):
        self.pulse(0)
        t = 0
        for _ in range(52):
            self.m.advance(at(t+8))
            self.pulse(t+16)
            t += 16
        self.assertEqual(len(self.data()["events"]), 52)
        self.assertEqual(len(self.h.snapshot()["communication"]["events"]), 50)

    def test_demo_real_com_dois_sensores_e_relatorio_offline(self):
        report = asyncio.run(demonstrate_communication(self.work))
        html = report.read_text(encoding="utf-8")
        for label in ("sala-01", "sala-02", "14:00:15", "14:00:32", "17 s", "Comunicação retomada"):
            self.assertIn(label, html)
        self.assertNotIn("https://", html)
        h = History(report.parent / "central.sqlite3")
        self.assertEqual(h.snapshot()["total"], 13)
        self.assertEqual(h.communication("sala-02")["events"], [])
        self.assertEqual(len(h.communication("sala-01")["events"]), 1)

    def test_sensor_normal_e_monitor_funcionam_sem_painel(self):
        async def check():
            moment = at(0)
            q = Outbox(self.work / "sensor.sqlite3")
            h = History(self.work / "real.sqlite3")
            async with station(h, 0, clock=lambda: moment, check_interval=.025) as port:
                sender = asyncio.create_task(run_sensor(q, "normal", port, .2))
                try:
                    await wait_contact(h, "normal", moment)
                    moment = at(5)
                    await wait_contact(h, "normal", moment)
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                for second in (10,15,20):
                    moment = at(second)
                    async with asyncio.timeout(5):
                        while h.communication("normal")["seen"] != stamp(moment):
                            await asyncio.sleep(.025)
                self.assertEqual(h.communication("normal")["events"][0]["detected"], stamp(at(20)))
        asyncio.run(check())

    def test_mqtt_descarta_resposta_antiga_e_repetida(self):
        async def check():
            h = History(self.work / "mqtt.sqlite3")
            moment = at(0)
            async with station(h, 0, clock=lambda: moment, check_interval=.025) as port:
                async with client(port, PRESENCE_REQUEST) as sensor:
                    async with asyncio.timeout(4):
                        first = (await sensor.messages.get()).message
                    sensor.publish(mqttools.Message(topic("a","presenca"), first))
                    await wait_contact(h,"a",moment)
                    moment = at(3)
                    sensor.publish(mqttools.Message(topic("a","presenca"), first))
                    sensor.publish(mqttools.Message(topic("marcador","presenca"), first))
                    await wait_contact(h,"marcador",moment)
                    self.assertEqual(h.communication("a")["sensors"][0]["last_contact"], stamp(at(0)))
                    moment = at(5)
                    async with asyncio.timeout(4):
                        second = (await sensor.messages.get()).message
                    self.assertNotEqual(first, second)
                    sensor.publish(mqttools.Message(topic("a","presenca"), first))
                    sensor.publish(mqttools.Message(topic("marcador","presenca"), second))
                    await wait_contact(h,"marcador",moment)
                    self.assertEqual(h.communication("a")["sensors"][0]["last_contact"], stamp(at(0)))
        asyncio.run(check())
