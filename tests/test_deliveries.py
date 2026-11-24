"""Consulta por primeiro recebimento, base existente e instantâneo entre salas."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from telemetria import deliveries
from telemetria.communication import Observer, stamp
from telemetria.dashboard import dashboard
from telemetria.storage import History, Reading


def at(seconds=0, micros=0):
    return datetime(2026, 1, 15, 14, tzinfo=UTC) + timedelta(seconds=seconds, microseconds=micros)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "central.sqlite3"
        self.now = at()
        self.h = History(self.path, received_clock=lambda: self.now)

    def record(self, name, measured, received):
        self.now = received
        row = Reading.sample(name, measured)
        self.h.record(row)
        return row

    def test_inicio_inclusivo_fim_exclusivo_reenvio_e_relogio(self):
        first = self.record("b", at(-50), at(0, 1))
        early = self.record("b", at(-1), at(0))
        skew = self.record("b", at(20, 1), at(1))
        self.record("a", at(0), at(2))
        self.now = at(3)
        self.assertEqual(self.h.record(first), "repetida")
        report = deliveries.capture(self.h, stamp(at(0, 1)), stamp(at(2)))
        a, b = report["rooms"]
        self.assertEqual(a, {"sensor": "a", "readings": []})
        self.assertEqual([r["id"] for r in b["readings"]], [first.id, skew.id])
        self.assertEqual(b["readings"][0]["received_at"], stamp(at(0, 1)))
        self.assertEqual(b["readings"][1]["measured_at"], stamp(at(20, 1)))
        self.assertNotIn(early.id, str(report))
        self.assertEqual(deliveries.capture(self.h, stamp(at(3)), stamp(at(4)))["rooms"][1]["readings"], [])

    def test_recebimentos_simultaneos_ordenam_por_id_e_isolam_salas(self):
        observer = Observer(self.h)
        observer.start(at())
        observer.advance(at(1), "so-presenca", "r1")
        for name, identifier in [("a", 2), ("b", 2), ("a", 1)]:
            self.h.record(Reading(name, f"{identifier:032x}", stamp(at(-1)), 20, 50))
        a, b, quiet = deliveries.capture(self.h, stamp(at()), stamp(at(2)))["rooms"]
        self.assertEqual([int(r["id"],16) for r in a["readings"]], [1, 2])
        self.assertEqual([int(r["id"],16) for r in b["readings"]], [2])
        self.assertEqual(quiet, {"sensor": "so-presenca", "readings": []})

    def test_base_04_acrescenta_indice_sem_alterar_primeiro_recebimento(self):
        reading = self.record("a", at(-3), at())
        with self.h.connect() as db:
            db.execute("DROP INDEX readings_by_receipt")
            db.execute("PRAGMA user_version=4")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(History, [self.path, self.path]))
        with self.h.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM readings WHERE received_at>=? AND received_at<?"
                              " ORDER BY received_at,sensor,id", (stamp(at()),stamp(at(2)))).fetchall()
            self.assertIn("readings_by_receipt", " ".join(r["detail"] for r in plan))
        row, = deliveries.capture(self.h, stamp(at()), stamp(at(1)))["rooms"][0]["readings"]
        self.assertEqual((row["id"], row["received_at"]), (reading.id, stamp(at())))

    def test_consulta_relativa_usa_relogio_da_central(self):
        before = self.record("a", at(-4000), at(-3600))
        self.record("a", at(-500), at())
        for hours in (1, 24):
            data = deliveries.capture(self.h, hours=hours, now=at(0, 743))
            self.assertEqual((data["start"], data["end"]), (stamp(at(-hours * 3600)), stamp(at())))
            self.assertEqual([r["id"] for r in data["rooms"][0]["readings"]], [before.id])

    def test_nova_sala_e_nova_leitura_entre_catalogo_e_entregas(self):
        self.record("a", at(), at())
        other = History(self.path, received_clock=lambda: at(2))
        ready, committed = Event(), Event()
        def writer():
            if not ready.wait(5):
                raise TimeoutError("O leitor não iniciou a consulta.")
            try:
                for name in ("b", "a"):
                    other.record(Reading.sample(name, at(1)))
            finally:
                committed.set()
        connect = self.h.connect
        observed = []
        @contextmanager
        def reader():
            with connect() as db:
                def pause(sql):
                    if "FROM readings" in sql and not ready.is_set():
                        ready.set()
                        observed.append(committed.wait(5))
                db.set_trace_callback(pause)
                yield db

        def counts():
            report = deliveries.capture(self.h, stamp(at()), stamp(at(3)))
            return [(r["sensor"], len(r["readings"])) for r in report["rooms"]]

        with ThreadPoolExecutor(max_workers=1) as pool:
            finished = pool.submit(writer)
            with patch.object(self.h, "connect", reader):
                self.assertEqual(counts(), [("a", 1)])
            finished.result(timeout=5)
        self.assertEqual(observed, [True])
        self.assertEqual(counts(), [("a", 2), ("b", 1)])

    def test_painel_http_e_validacao_dos_filtros(self):
        self.record("a", at(-1), at())
        with dashboard(self.h, 0) as port:
            base = f"http://127.0.0.1:{port}"
            with urlopen(base, timeout=3) as res:
                self.assertIn('href="/entregas"', res.read().decode())
            with urlopen(base + "/entregas", timeout=3) as res:
                self.assertIn("Exportar comparação CSV", res.read().decode())
            query = urlencode({"inicio": stamp(at()), "fim": stamp(at(1))})
            with urlopen(base + "/api/entregas?" + query, timeout=3) as res:
                self.assertEqual(res.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(res), deliveries.capture(self.h, stamp(at()), stamp(at(1))))
            for bad in ("periodo=0h", "periodo=1h&periodo=24h", "inicio=x&fim=y",
                        "sensor=a&periodo=1h", urlencode({"inicio": stamp(at()), "fim": stamp(at())})):
                with self.subTest(bad=bad), self.assertRaises(HTTPError) as exc:
                    urlopen(base + "/api/entregas?" + bad, timeout=3)
                self.assertEqual(exc.exception.code, 400)

    def test_salas_com_regra_salva_e_sem_primeira_entrega_entram_na_comparacao(self):
        self.h.configure("a-reserva", high=32)
        self.record("b-ativa", at(-2), at(1))
        self.h.configure("b-ativa", high=31)
        report = deliveries.capture(self.h, stamp(at()), stamp(at(2)))
        self.assertEqual([r["sensor"] for r in report["rooms"]], ["a-reserva", "b-ativa"])
        self.assertEqual([len(r["readings"]) for r in report["rooms"]], [0, 1])

    def test_janela_relativa_fixada_antes_da_leitura_do_catalogo(self):
        from telemetria import availability

        first = self.record("a", at(-3600), at(-3599))
        writer = History(self.path, received_clock=lambda: at(1))
        clock = at()
        original = availability.sensor_names

        def collecting_catalog(db):
            nonlocal clock
            names = original(db)
            writer.record(Reading.sample("b", at(1)))
            clock = at(3)
            return names

        with patch.object(availability, "sensor_names", collecting_catalog), patch.object(deliveries, "datetime") as source:
            source.now.side_effect = lambda tz: clock
            report = deliveries.capture(self.h, hours=1)
        self.assertEqual((report["start"], report["end"]), (stamp(at(-3600)), stamp(at())))
        self.assertEqual([(r["sensor"], [s["id"] for s in r["readings"]]) for r in report["rooms"]],
                         [("a", [first.id])])
        after = deliveries.capture(self.h, hours=1, now=at(3))
        self.assertEqual([(r["sensor"], len(r["readings"])) for r in after["rooms"]], [("a", 0), ("b", 1)])

    def test_duas_gravacoes_do_mesmo_id_preservam_o_primeiro_recebimento_por_sala(self):
        from threading import Barrier

        self.h.configure("a", high=34)
        self.h.configure("b", high=34)
        measuring = Reading("a", "1" * 32, stamp(at(-2)), 23, 50)
        locked, retry_started, release = Event(), Event(), Event()

        def first_clock():
            locked.set()
            if not release.wait(8):
                raise TimeoutError("A primeira gravação não foi liberada.")
            return at(1)

        first = History(self.path, received_clock=first_clock)
        retry = History(self.path, received_clock=lambda: at(5))
        other = History(self.path, received_clock=lambda: at(6))
        connect = retry.connect

        @contextmanager
        def second_connection():
            with connect() as db:
                db.set_trace_callback(lambda sql: retry_started.set() if sql == "BEGIN IMMEDIATE" else None)
                yield db

        together = Barrier(2)
        def record_second(room):
            together.wait(timeout=5)
            if room == "a":
                return retry.record(measuring)
            return other.record(Reading("b", measuring.id, measuring.measured_at, 23, 50))

        def receipts(start, end):
            report = deliveries.capture(self.h, stamp(at(start)), stamp(at(end)))
            return [(r["sensor"], [(s["id"], s["received_at"]) for s in r["readings"]]) for r in report["rooms"]]

        with patch.object(retry, "connect", second_connection), ThreadPoolExecutor(max_workers=3) as pool:
            original = pool.submit(first.record, measuring)
            try:
                self.assertTrue(locked.wait(5))
                duplicate = pool.submit(record_second, "a")
                independent = pool.submit(record_second, "b")
                self.assertTrue(retry_started.wait(5))
                self.assertEqual(receipts(0, 7), [("a", []), ("b", [])])
            finally:
                release.set()
            self.assertEqual([job.result(timeout=7) for job in (original, duplicate, independent)],
                             ["gravada", "repetida", "gravada"])
        self.assertEqual(receipts(0, 7), [("a", [(measuring.id, stamp(at(1)))]),
                                          ("b", [(measuring.id, stamp(at(6)))])])
        self.assertEqual(receipts(5, 6), [("a", []), ("b", [])])
