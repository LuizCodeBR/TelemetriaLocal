import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from urllib.request import urlopen

from telemetria.alerts import Rule
from telemetria.dashboard import dashboard, render
from telemetria.demo import demonstrate_alerts
from telemetria.storage import History, Reading, ReadingConflict


ORIGIN = datetime(2026, 1, 15, 14, 2, tzinfo=UTC)


def sample(offset, value, sensor="sala-01", identifier=None):
    stamp = ORIGIN + timedelta(seconds=offset)
    return Reading(sensor, f"{identifier if identifier is not None else offset:032x}", stamp.isoformat(), value, 55)


class AlertTests(unittest.TestCase):
    def setUp(self):
        temp = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        temp.mkdir(parents=True, exist_ok=True)
        work = tempfile.TemporaryDirectory(dir=temp)
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        self.history = History(self.directory / "central.sqlite3")

    def events(self, sensor="sala-01"):
        return self.history.occurrences(sensor)["events"]

    def put(self, pairs, sensor="sala-01"):
        for offset, value in pairs:
            self.history.record(sample(offset, value, sensor))

    def test_pico_isolado_e_pre_confirmacao_interrompida(self):
        self.put([(0, 29), (2, 28), (4, 27.99), (6, 28), (8, 28)])
        self.assertEqual(self.events(), [])
        self.history.record(sample(10, 28))
        self.assertEqual(self.events()[0]["started"], sample(6, 28).measured_at)

    def test_abertura_histerese_pico_e_fechamento_inclusivos(self):
        self.put([(0, 34), (2, 28), (4, 28), (6, 26.01)])
        event, = self.events()
        self.assertEqual((event["status"], event["peak"], event["ended"]), ("aberta", 34, None))
        self.assertEqual(event["observed_until"], sample(6, 26.01).measured_at)
        self.put([(8, 35), (10, 26)])
        event, = self.events()
        self.assertEqual((event["status"], event["peak"]), ("recuperada", 35))
        self.assertEqual((event["started"], event["ended"]), (sample(0, 34).measured_at, sample(10, 26).measured_at))

    def test_reabrir_banco_preserva_candidata_e_ocorrencia_aberta(self):
        self.put([(0, 28), (2, 30)])
        self.history = History(self.history.path)
        self.put([(4, 28)])
        self.assertEqual(self.events()[0]["status"], "aberta")
        self.history = History(self.history.path)
        self.put([(6, 29), (8, 25)])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["peak"], 30)

    def test_lacuna_interrompe_e_sequencia_precisa_recomecar(self):
        self.put([(0, 28), (10, 29), (20, 30), (31, 33), (33, 31)])
        event, = self.events()
        self.assertEqual(event["status"], "lacuna")
        self.assertEqual(event["ended"], sample(20, 30).measured_at)
        self.assertEqual(len(self.history.snapshot()["gaps"]), 1)
        self.put([(35, 29)])
        self.assertEqual([r["status"] for r in self.events()], ["aberta", "lacuna"])
        self.put([(25, 29)])
        event, = self.events()
        self.assertEqual((event["status"], event["peak"]), ("aberta", 33))
        self.assertEqual(self.history.snapshot()["gaps"], [])

    def test_lacuna_entre_amostras_altas_nao_confirma_sequencia_antiga(self):
        self.put([(0, 29), (2, 29), (13, 31), (15, 28)])
        self.assertEqual(self.events(), [])
        self.put([(17, 28)])
        event, = self.events()
        self.assertEqual(event["started"], sample(13, 31).measured_at)
        self.assertEqual(event["peak"], 31)

    def test_medicao_atrasada_pode_desfazer_uma_abertura(self):
        self.put([(0, 28), (4, 29), (6, 30), (8, 25)])
        self.assertEqual(len(self.events()), 1)
        self.put([(2, 25)])
        self.assertEqual(self.events(), [])

    def test_mesmo_instante_conta_uma_vez_e_prevalece_maior_temperatura(self):
        rows = [sample(0, 28, identifier=20), sample(0, 26, identifier=21), sample(0, 33, identifier=22)]
        for row in rows:
            self.history.record(row)
            self.assertEqual(self.history.record(row), "repetida")
        self.assertEqual(self.events(), [])
        self.put([(2, 28), (4, 28)])
        self.assertEqual(self.events()[0]["peak"], 33)
        with self.assertRaises(ReadingConflict):
            self.history.record(replace(rows[0], temperature=70))
        self.assertEqual(self.events()[0]["peak"], 33)

    def test_regra_persistida_e_recalculo_so_do_sensor_escolhido(self):
        for sensor in ("a", "b"):
            self.put([(0, 29), (2, 30), (4, 31), (6, 26.5)], sensor)
        before = self.events("b")
        self.history.configure("a", high=32)
        self.assertEqual(self.events("a"), [])
        self.assertEqual(self.events("b"), before)
        self.history.configure("a", high=29, recovery=27)
        reopened = History(self.history.path)
        self.assertEqual(reopened.configure("a"), Rule(29, 27, 10))
        self.assertEqual(reopened.occurrences("a")["events"][0]["status"], "recuperada")
        for changes in ({"gap_seconds": 0}, {"gap_seconds": float("inf")}, {"recovery": 29}, {"high": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reopened.configure("a", **changes)
        self.assertEqual(reopened.configure("a"), Rule(29, 27, 10))

    def test_duplicadas_e_fora_de_ordem_em_duas_conexoes(self):
        rows = [sample(i, v) for i, v in [(0, 28), (2, 31), (4, 28), (6, 26),
                                            (30, 30), (32, 35), (34, 32), (36, 25)]]
        baseline = History(self.directory / "ordenada.sqlite3")
        for row in rows:
            baseline.record(row)
        inputs = rows * 3
        random.Random(42).shuffle(inputs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.history.record, inputs))
        self.assertEqual(results.count("gravada"), 8)
        self.assertEqual(results.count("repetida"), 16)
        self.assertEqual(self.history.occurrences("sala-01"), baseline.occurrences("sala-01"))

    def test_configuracao_e_gravacoes_concorrentes(self):
        rows = [sample(i, 33 if i < 24 else 27) for i in range(0, 32, 2)]
        expected = History(self.directory / "referencia.sqlite3")
        expected.configure("sala-01", high=32, recovery=27, gap_seconds=5)
        for row in rows:
            expected.record(row)
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(self.history.record, row) for row in reversed(rows[:8])]
            jobs.append(pool.submit(self.history.configure, "sala-01", high=32, recovery=27, gap_seconds=5))
            jobs.extend(pool.submit(self.history.record, row) for row in reversed(rows[8:]))
            for job in jobs:
                job.result()
        self.assertEqual(self.events(), expected.occurrences("sala-01")["events"])

    def test_falha_na_ocorrencia_reverte_medicao_e_estado(self):
        self.put([(0, 28), (2, 29)])
        with self.history.connect() as db:
            db.execute("""CREATE TRIGGER teste_falha BEFORE INSERT ON thermal_events
                          BEGIN SELECT RAISE(ABORT, 'falha simulada'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.history.record(sample(4, 30))
        self.assertEqual(self.history.snapshot()["total"], 2)
        self.assertEqual(self.events(), [])
        with self.history.connect() as db:
            db.execute("DROP TRIGGER teste_falha")
        self.assertEqual(self.history.record(sample(4, 30)), "gravada")
        self.assertEqual(self.events()[0]["peak"], 30)

    def test_importa_banco_anterior_com_mais_de_60_medicoes(self):
        path = self.directory / "anterior.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE readings (sensor TEXT,id TEXT,measured_at TEXT,temperature REAL,humidity REAL,
                          received_at TEXT, PRIMARY KEY(sensor,id))""")
            for n in range(99, -1, -1):
                row = sample(n, 29 if n < 3 else 25)
                db.execute("INSERT INTO readings VALUES(?,?,?,?,?,?)",
                           (row.sensor, row.id, row.measured_at, row.temperature, row.humidity, row.measured_at))
        restored = History(path)
        snapshot = restored.snapshot()
        self.assertEqual((snapshot["total"], len(snapshot["rows"])), (100, 60))
        self.assertEqual(snapshot["events"][0]["started"], sample(0, 29).measured_at)
        self.assertEqual(History(path).snapshot(), snapshot)

    def test_painel_lista_abertas_e_historico_completo_por_sensor(self):
        for n in range(52):
            base = n * 8
            self.put([(base, 28), (base+2, 30), (base+4, 32), (base+6, 26)])
        self.put([(0, 29), (2, 31), (4, 32)], "sala-02")
        summary = self.history.snapshot()
        self.assertEqual(len(summary["events"]), 51)
        self.assertEqual(summary["events"][0]["status"], "aberta")
        self.assertEqual(len(self.events()), 52)
        with dashboard(self.history, 0) as port:
            with urlopen(f"http://127.0.0.1:{port}/", timeout=4) as response:
                page = response.read().decode()
        for label in ("Ocorrências de temperatura", "Aberta na última medição", "Recuperada", "32,00 °C"):
            self.assertIn(label, page)

    def test_demo_mqtt_e_relatorio_offline(self):
        report = asyncio.run(demonstrate_alerts(self.directory))
        snap = History(report.parent / "central.sqlite3").snapshot()
        self.assertEqual((snap["total"], len(snap["events"]), len(snap["gaps"])), (33, 1, 0))
        html = report.read_text(encoding="utf-8")
        self.assertEqual(html, render(snap, live=False))
        for label in ("14:02:00", "14:07:00", "5 min", "31,40 °C", "Recuperada"):
            self.assertIn(label, html)
        self.assertNotIn("https://", html)
