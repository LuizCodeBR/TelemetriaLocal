import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
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
from telemetria.demo_communication import demonstrate_timeline
from telemetria.storage import History, Reading

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = datetime(2026, 1, 15, 14, tzinfo=UTC)


def at(s):
    return ORIGIN + timedelta(seconds=s)


class Bootstrap(HTMLParser):
    def __init__(self):
        super().__init__()
        self.recording = False
        self.data = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.recording = dict(attrs).get("id") == "bootstrap"

    def handle_data(self, data):
        if self.recording:
            self.data += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.recording = False


class TimelineTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.h = History(self.directory / "central.sqlite3", received_clock=lambda: at(32))
        self.m = Observer(self.h)

    def period(self, start, end, name="sala-01", *, now=45):
        return timeline.period(self.h, name, stamp(at(start)), stamp(at(end)), now=at(now))

    def incident(self):
        self.m.start(at(0))
        self.m.advance(at(0), "sala-01", "primeiro")
        for tick in (8, 15, 20):
            self.m.advance(at(tick))

    def test_intervalo_semiaberto_em_utc_separa_sensores_e_preserva_microssegundos(self):
        for t in (0, 1, 1.999999, 2):
            self.h.record(Reading.sample("sala-01", at(t)))
        self.h.record(Reading.sample("sala-02", at(1)))
        rows = self.period(1, 2)["rows"]
        self.assertEqual([r["measured_at"] for r in rows], [stamp(at(1)), stamp(at(1.999999))])
        self.assertEqual({r["received_at"] for r in rows}, {stamp(at(32))})
        self.assertEqual(self.period(9, 10)["rows"], [])

    def test_reenvio_nao_duplica_e_dois_ids_no_mesmo_instante_permanecem(self):
        original = replace(Reading.sample("sala-01", at(8)), id="f"*32)
        other = replace(original, id="a"*32, temperature=30)
        for row in (original, other, original):
            self.h.record(row)
        rows = self.period(8, 9)["rows"]
        self.assertEqual([r["id"] for r in rows], [other.id, original.id])
        self.assertEqual(rows[0]["temperature"], 30)

    def test_interrupcao_cruza_filtro_e_retomada_exatamente_no_inicio(self):
        self.incident()
        self.m.advance(at(30), "sala-01", "nova")
        expected = [{"start": stamp(at(15)), "until": stamp(at(30)), "outcome": "retomada"}]
        self.assertEqual(self.period(17, 19)["events"], expected)
        self.assertEqual(self.period(30, 31)["events"], expected)
        self.assertEqual(self.period(0, 15)["events"], [])
        self.assertEqual(self.period(30.000001, 31)["events"], [])
        self.assertEqual(self.period(17, 19, "sala-02")["events"], [])

    def test_interrupcao_aberta_para_no_ultimo_sinal_da_central_e_lacuna_e_global(self):
        self.incident()
        event, = self.period(18, 60, now=24)["events"]
        self.assertIsNone(event["outcome"])
        self.assertEqual(event["until"], stamp(at(20)))
        stale = self.period(18, 60, now=40)
        self.assertEqual(stale["events"][0]["outcome"], "sem_observacao")
        self.assertEqual(stale["gaps"][0]["until"], stamp(at(40)))
        self.assertEqual(self.period(41, 60, now=40)["gaps"], [])
        self.m.stop()
        self.m.start(at(45))
        for name in ("sala-01","sala-02"):
            gap, = self.period(25, 35, name)["gaps"]
            self.assertEqual((gap["start"], gap["until"]), (stamp(at(20)), stamp(at(45))))
            self.assertEqual(self.period(45, 46, name)["gaps"], [])

    def test_periodo_consulta_arquivo_alem_dos_limites_do_resumo(self):
        self.m.start(at(0))
        self.m.advance(at(0), "sala-01", "0")
        for i in range(1, 63):
            t = 16*i
            self.m.advance(at(t-8))
            self.m.advance(at(t), "sala-01", str(i))
            self.h.record(Reading.sample("sala-01", at(t)))
        self.assertEqual(len(self.period(0, 1000)["rows"]), 62)
        self.assertEqual(len(self.period(0, 1000)["events"]), 62)
        self.assertEqual(len(self.period(0, 17)["events"]), 1)
        self.assertEqual(len(self.h.snapshot()["communication"]["events"]), 50)

    def test_consulta_concorrente_mantem_leituras_e_eventos_no_mesmo_commit_visivel(self):
        self.incident()
        self.h.record(Reading.sample("sala-01", at(1)))
        other = History(self.h.path)
        connect = self.h.connect
        committed = []

        @contextmanager
        def concurrent_reader():
            with connect() as db:
                def after_rows(sql):
                    if "FROM readings" in sql and not committed:
                        other.record(Reading.sample("sala-01", at(2)))
                        self.m.advance(at(22), "sala-01", "volta")
                        committed.append(True)
                db.set_trace_callback(after_rows)
                yield db

        # O escritor usa conexões próprias enquanto o leitor conserva sua transação.
        self.m.history = other
        with patch.object(self.h, "connect", concurrent_reader):
            earlier = self.period(0, 30, now=23)
        self.assertTrue(committed)
        latest = self.period(0, 30, now=23)
        self.assertEqual([len(earlier["rows"]), len(latest["rows"])], [1, 2])
        self.assertIsNone(earlier["events"][0]["outcome"])
        self.assertEqual(latest["events"][0]["outcome"], "retomada")

    def test_http_pagina_catalogo_consulta_e_filtros_invalidos(self):
        self.h.record(Reading.sample("sala-01", at(1)))
        self.m.start(at(0))
        self.m.advance(at(0), "so-presenca", "rodada")
        with dashboard(self.h, 0) as port:
            url = f"http://127.0.0.1:{port}"
            with urlopen(url + "/", timeout=3) as r:
                self.assertIn('href="/historico"', r.read().decode())
            with urlopen(url + "/historico", timeout=3) as r:
                parser = Bootstrap()
                parser.feed(r.read().decode())
                self.assertEqual(json.loads(parser.data), {"sensors": ["sala-01","so-presenca"], "example": None})
            args = {"sensor":"sala-01","inicio":stamp(at(0)),"fim":stamp(at(2))}
            with urlopen(url + "/api/historico?" + urlencode(args), timeout=3) as r:
                self.assertEqual(r.headers.get_content_type(), "application/json")
                self.assertEqual(len(json.load(r)["rows"]), 1)
            bad = [urlencode(args | {"sensor":"sala/02"}), urlencode(args | {"fim":args["inicio"]}),
                   urlencode(args | {"inicio":"2026-01-15T14:00:00"}), "sensor=sala-01",
                   urlencode(args) + "&sensor=outro"]
            for query in bad:
                with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                    urlopen(url + "/api/historico?" + query, timeout=3)
                self.assertEqual(error.exception.code, 400)

    def test_demo_offline_contem_dois_sensores_atrasos_e_parada_real_da_central(self):
        path = asyncio.run(demonstrate_timeline(self.directory))
        html = path.read_text(encoding="utf-8")
        parser = Bootstrap()
        parser.feed(html)
        demo = json.loads(parser.data)["example"]
        first, second = (demo["data"][k] for k in ("sala-01","sala-02"))
        late = [r for r in first["rows"] if r["received_at"] == stamp(at(32))]
        self.assertEqual([r["measured_at"] for r in late], [stamp(at(t)) for t in range(5,31,5)])
        self.assertEqual([len(first["rows"]),len(second["rows"])], [8,7])
        self.assertEqual(first["events"], [{"start":stamp(at(15)), "until":stamp(at(32)), "outcome":"retomada"}])
        self.assertFalse(second["events"])
        self.assertEqual(first["gaps"], second["gaps"])
        self.assertEqual(first["gaps"], [{"start":stamp(at(32)), "until":stamp(at(42))}])
        self.assertNotIn("<script src=", html)
        self.assertNotIn("https://", html)
