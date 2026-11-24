"""Cobertura temporal por sala, migração conservadora e leitura concorrente do relatório."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from telemetria import timeline
from telemetria.communication import Observer, stamp
from telemetria.storage import History, Reading


def at(seconds):
    return datetime(2026, 1, 15, 14, tzinfo=UTC) + timedelta(seconds=seconds)


def span(start, end):
    return [stamp(at(start)), stamp(at(end))]


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "central.sqlite3"
        self.h = History(self.path)
        self.m = Observer(self.h)
        self.m.start(at(0))

    def read(self, start, end, now=100):
        return timeline.period(self.h, "a", *span(start, end), now=at(now))["availability"]

    def test_entrada_tardia_reenvio_e_leitura_sem_sinal(self):
        self.h.record(Reading.sample("sem-presenca", at(0)))
        self.m.advance(at(3), "a", "r1")
        self.m.advance(at(4), "a", "r1")
        self.m.advance(at(9), "a", "r2")
        self.m.advance(at(10))
        report = self.read(0, 10)
        self.assertEqual(report, {"rooms":[
            {"sensor":"a", "evaluated":[span(3,10)], "outages":[]},
            {"sensor":"sem-presenca", "evaluated":[], "outages":[]}], "blind":[]})
        with self.h.connect() as db:
            row = db.execute("SELECT first_contact,classified_from FROM comm_sensor_windows").fetchone()
            self.assertEqual(tuple(row), (stamp(at(3)),stamp(at(3))))

    def test_reinicio_sem_resposta_avalia_a_partir_da_deteccao_e_recorta_a_interrupcao(self):
        self.m.advance(at(0), "a", "r1")
        self.m.advance(at(8))
        self.m.stop()
        self.m.start(at(20))
        for t in (30,35,40):
            self.m.advance(at(t))
        self.m.advance(at(43), "a", "r2")
        self.m.advance(at(45))
        report = self.read(0,45)
        room, = report["rooms"]
        self.assertEqual(room["evaluated"], [span(0,8),span(35,45)])
        self.assertEqual(room["outages"], [span(35,43)])
        self.assertEqual(report["blind"], [span(8,20)])
        with self.h.connect() as db:
            row = db.execute("SELECT first_contact, classified_from FROM comm_sensor_windows ORDER BY window_id DESC").fetchone()
            self.assertEqual(tuple(row),(stamp(at(43)),stamp(at(35))))
        cut = self.read(38,42)["rooms"][0]
        self.assertEqual(cut["evaluated"], [span(38,42)])
        self.assertEqual(cut["outages"], [span(38,42)])
        self.assertFalse(self.read(43,44)["rooms"][0]["outages"])

    def test_parada_abrupta_termina_tempo_avaliado_no_ultimo_ponto(self):
        self.m.advance(at(0), "a", "r1")
        for t in (8,15,20):
            self.m.advance(at(t))
        report = self.read(10,55,now=42)
        self.assertEqual(report["rooms"][0]["evaluated"], [span(10,20)])
        self.assertEqual(report["rooms"][0]["outages"], [span(15,20)])
        self.assertEqual(report["blind"], [span(20,42)])
        self.assertEqual(self.read(43,55,now=42)["blind"], [])

    def test_migracao_preserva_somente_contatos_comprovados_em_cada_janela(self):
        self.m.advance(at(0), "a", "primeira")
        self.m.advance(at(5), "a", "ultima-antes-da-queda")
        self.m.advance(at(12))
        self.m.advance(at(20))
        self.m.advance(at(23), "a", "retorno")
        self.m.stop()
        self.m.start(at(30))
        self.m.advance(at(32), "a", "primeira-apos-reinicio")
        self.m.advance(at(36), "a", "ultima")
        self.m.advance(at(40))
        self.h.record(Reading.sample("apenas-medicao",at(3)))
        old_events = self.h.communication("a")["events"]
        with self.h.connect() as db:
            db.execute("DROP TABLE comm_sensor_windows")
            db.execute("PRAGMA user_version=3")
        self.h = History(self.path)
        result = self.read(0,40)
        self.assertEqual(result["rooms"][0]["evaluated"], [span(5,23),span(36,40)])
        self.assertEqual(result["rooms"][0]["outages"], [span(20,23)])
        self.assertEqual(result["rooms"][1]["evaluated"], [])
        self.assertEqual(result["blind"], [span(23,30)])
        self.assertEqual(self.h.communication("a")["events"],old_events)
        with self.h.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_primeira_presenca_e_avanco_usam_mesmo_commit(self):
        with self.h.connect() as db:
            db.execute("""CREATE TRIGGER interromper BEFORE INSERT ON comm_sensor_windows
                          BEGIN SELECT RAISE(ABORT,'ensaio'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.m.advance(at(4), "a", "r")
        with self.h.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comm_contacts").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT seen FROM comm_windows").fetchone()[0],stamp(at(0)))
            db.execute("DROP TRIGGER interromper")
        self.m.advance(at(4), "a", "r")
        self.m.advance(at(6))
        self.assertEqual(self.read(0,6)["rooms"][0]["evaluated"], [span(4,6)])

    def test_consulta_das_salas_enquanto_outra_conexao_grava_retomada(self):
        self.m.advance(at(0), "a", "r")
        for t in (8,15,18):
            self.m.advance(at(t), "b", str(t))
        other = History(self.path)
        self.m.history = other
        connect = self.h.connect
        wrote = []

        @contextmanager
        def reader():
            with connect() as db:
                def interleave(sql):
                    if "FROM comm_sensor_windows s JOIN" in sql and not wrote:
                        self.m.advance(at(22), "a", "volta")
                        wrote.append(True)
                db.set_trace_callback(interleave)
                yield db

        with patch.object(self.h,"connect",reader):
            before = timeline.period(self.h,"a",*span(0,25),now=at(23))
        after = timeline.period(self.h,"a",*span(0,25),now=at(23))
        self.assertTrue(wrote)
        for data, end in ((before,18),(after,22)):
            a,b = data["availability"]["rooms"]
            self.assertEqual(a["evaluated"],[span(0,end)])
            self.assertEqual(b["evaluated"],[span(8,end)])
            self.assertEqual(a["outages"],[span(15,end)])
            self.assertEqual(data["events"][0]["until"], stamp(at(end)))
        self.assertIsNone(before["events"][0]["outcome"])
        self.assertEqual(after["events"][0]["outcome"],"retomada")

    def test_relogio_recuado_preserva_observacoes_e_lacunas_ja_gravadas(self):
        self.m.advance(at(0), "a", "a1")
        for second in (8, 15, 20):
            self.m.advance(at(second), "b", str(second))
        self.m.advance(at(22), "a", "a2")
        self.m.stop()
        self.m.start(at(30))
        self.m.advance(at(32), "b", "b2")
        self.m.advance(at(36))
        for clock in (12, 22, 29, 36):
            with self.subTest(clock=clock):
                data = timeline.period(self.h, "a", *span(0, 50), now=at(clock))
                report = data["availability"]
                self.assertEqual(report["rooms"], [
                    {"sensor": "a", "evaluated": [span(0, 22)], "outages": [span(15, 22)]},
                    {"sensor": "b", "evaluated": [span(8, 22), span(32, 36)], "outages": []},
                ])
                self.assertEqual(report["blind"], [span(22, 30)])
                event, = data["events"]
                self.assertEqual([event["start"], event["until"]], span(15, 22))
                self.assertEqual(self.read(40, 50, now=clock)["blind"], [])

    def test_catalogo_ao_vivo_acompanha_entrada_das_salas(self):
        from json import load
        from urllib.request import urlopen
        from telemetria.dashboard import dashboard

        with dashboard(self.h, 0) as port:
            url = f"http://127.0.0.1:{port}/api/sensores"
            with urlopen(url, timeout=3) as response:
                self.assertEqual(load(response), [])
            self.m.advance(at(2), "b", "primeiro")
            self.h.record(Reading.sample("a", at(3)))
            with urlopen(url, timeout=3) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                names = load(response)
            self.assertEqual(names, ["a", "b"])
            self.assertEqual(names, [r["sensor"] for r in self.read(0, 4)["rooms"]])

    def test_escritor_reinicia_e_cadastra_outra_sala_no_meio_da_comparacao(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        self.m.advance(at(0), "a", "a1")
        for second in (6, 12, 18):
            self.m.advance(at(second), "b", str(second))
        checkpoint, committed = Event(), Event()
        writer_monitor = Observer(History(self.path))

        def writer():
            if not checkpoint.wait(5):
                raise TimeoutError("A consulta não chegou ao ponto de sincronização.")
            try:
                writer_monitor.start(at(24))
                writer_monitor.advance(at(25), "c", "c1")
                writer_monitor.advance(at(26), "a", "a2")
                writer_monitor.advance(at(28))
            finally:
                committed.set()

        original_connect = self.h.connect
        observations = []

        @contextmanager
        def reader():
            with original_connect() as db:
                def interleave(sql):
                    if "FROM comm_events e JOIN comm_windows" in sql and not checkpoint.is_set():
                        checkpoint.set()
                        observations.append(committed.wait(5))
                db.set_trace_callback(interleave)
                yield db

        with ThreadPoolExecutor(max_workers=1) as pool:
            finished = pool.submit(writer)
            with patch.object(self.h, "connect", reader):
                previous = timeline.period(self.h, "a", *span(0, 30), now=at(30))
            finished.result(timeout=5)
        self.assertEqual(observations, [True])
        fresh = timeline.period(self.h, "a", *span(0, 30), now=at(30))
        old, new = (result["availability"] for result in (previous, fresh))
        self.assertEqual([r["sensor"] for r in old["rooms"]], ["a", "b"])
        self.assertEqual([r["sensor"] for r in new["rooms"]], ["a", "b", "c"])
        self.assertEqual(old["rooms"][0]["evaluated"], [span(0, 18)])
        self.assertEqual(new["rooms"][0]["evaluated"], [span(0, 18), span(26, 28)])
        self.assertEqual(new["rooms"][2]["evaluated"], [span(25, 28)])
        self.assertEqual(old["rooms"][0]["outages"], [span(15, 18)])
        self.assertEqual(new["rooms"][0]["outages"], [span(15, 18)])
        self.assertEqual(old["blind"], [])
        self.assertEqual(new["blind"], [span(18, 24)])
        self.assertIsNone(previous["events"][0]["outcome"])
        self.assertEqual(fresh["events"][0]["outcome"], "sem_observacao")
