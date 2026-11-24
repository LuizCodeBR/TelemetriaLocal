"""Contratos do histórico na fronteira do período e durante commits concorrentes."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from telemetria import timeline
from telemetria.communication import Observer, stamp
from telemetria.dashboard import dashboard
from telemetria.storage import History, Reading


BASE = datetime(2026, 1, 15, 14, tzinfo=UTC)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


class TimelineReviewTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(folder.cleanup)
        self.h = History(Path(folder.name) / "central.sqlite3")

    def test_atalhos_http_usam_o_relogio_da_central_e_fim_exclusivo(self):
        end = at(86400.125)
        for offset in (-86400, -3600, -.000001, 0):
            self.h.record(Reading.sample("sala", end + timedelta(seconds=offset)))
        with dashboard(self.h, 0) as port, patch("telemetria.timeline.datetime", wraps=datetime) as clock:
            clock.now.return_value = end + timedelta(microseconds=743)
            for preset, hours, count in (("1h", 1, 2), ("24h", 24, 3)):
                with self.subTest(preset=preset):
                    url = f"http://127.0.0.1:{port}/api/historico?" + urlencode({"sensor":"sala","periodo":preset})
                    with urlopen(url, timeout=3) as r:
                        data = json.load(r)
                    self.assertEqual(data["start"], stamp(end-timedelta(hours=hours)))
                    self.assertEqual(data["end"], stamp(end))
                    self.assertEqual(len(data["rows"]), count)
                    manual = timeline.period(self.h, "sala", data["start"], data["end"], now=end)
                    self.assertEqual(data, manual)

    def test_consulta_rejeita_mistura_de_periodo_relativo_e_datas(self):
        with dashboard(self.h, 0) as port:
            for query in ("sensor=a&periodo=1h&inicio=2026-01-15", "sensor=a&periodo=5h",
                          "sensor=a&periodo=1h&periodo=24h"):
                with self.subTest(query=query), self.assertRaises(HTTPError) as err:
                    urlopen(f"http://127.0.0.1:{port}/api/historico?{query}", timeout=3)
                self.assertEqual(err.exception.code, 400)

    def test_no_inicio_da_lacuna_so_ha_interrupcao_se_houve_deteccao_ou_retomada(self):
        observer = Observer(self.h)
        observer.start(at(0))
        observer.advance(at(0), "sala", "1")
        for t in (8, 15, 20):
            observer.advance(at(t))
        view = lambda a,b: timeline.period(self.h, "sala", stamp(at(a)), stamp(at(b)), now=at(30))
        self.assertEqual(view(19,21)["events"][0]["until"], stamp(at(20)))
        self.assertEqual(view(20,21)["events"], [])
        observer.stop()
        self.assertEqual(view(20,21)["events"], [])
        self.assertEqual(view(20,21)["gaps"][0]["start"], stamp(at(20)))
        self.assertEqual(len(view(15,16)["events"]), 1)

    def test_deteccao_com_duracao_zero_e_retomada_na_fronteira(self):
        observer = Observer(self.h)
        observer.start(at(0))
        observer.advance(at(0), "a", "1")
        observer.advance(at(8))
        observer.advance(at(15), "a", "2")
        recut = lambda start, end: timeline.period(self.h,"a",stamp(at(start)),stamp(at(end)), now=at(15))["events"]
        self.assertEqual(recut(14,15), [])
        event, = recut(15,16)
        self.assertEqual(event, {"start":stamp(at(15)),"until":stamp(at(15)),"outcome":"retomada"})
        self.assertEqual(recut(15.000001,16), [])

    def test_captura_offline_de_duas_salas_usa_um_unico_estado_do_banco(self):
        names = ("sala-a","sala-b")
        for name in names:
            self.h.record(Reading.sample(name, at(1)))
        other = History(self.h.path)
        connect = self.h.connect
        committed = []

        @contextmanager
        def interleave():
            with connect() as db:
                def after_first(sql):
                    if "FROM readings" in sql and "'sala-b'" in sql and not committed:
                        for name in names:
                            other.record(Reading.sample(name, at(2)))
                        committed.append(True)
                db.set_trace_callback(after_first)
                yield db

        def capture():
            return timeline.capture_period(self.h, names, stamp(at(0)), stamp(at(5)), now=at(5))

        with patch.object(self.h, "connect", interleave):
            first = capture()
        self.assertTrue(committed)
        latest = capture()
        self.assertEqual([len(first[n]["rows"]) for n in names], [1,1])
        self.assertEqual([len(latest[n]["rows"]) for n in names], [2,2])

    def test_precisao_da_api_preserva_microssegundos_e_rejeita_truncamento(self):
        self.h.record(Reading.sample("a", at(.100000)))
        prefix = "2026-01-15T14:00:00."
        with self.assertRaises(ValueError):
            timeline.period(self.h, "a", stamp(at(0)), prefix+"1000001Z")
        data = timeline.period(self.h, "a", prefix+"100000000Z", prefix+"100001Z")
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["start"], stamp(at(.1)))
