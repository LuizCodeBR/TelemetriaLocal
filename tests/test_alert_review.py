"""Compara o histórico publicado com a série bruta e exercita fronteiras de transação."""
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

from telemetria import alerts
from telemetria.dashboard import render
from telemetria.storage import History, Reading, ReadingConflict

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 15, 14, 2, tzinfo=UTC)


def sample(identifier, offset, value, sensor="laboratorio"):
    when = (START + timedelta(seconds=offset)).isoformat(timespec="microseconds")
    return Reading(sensor, f"{identifier:032x}", when, value, 55)


def reference(rows, sensor, high=28, recovery=26, gap=10):
    """Oráculo em lote: divide por lacuna, procura trincas e depois a primeira recuperação."""
    maxima = {}
    for reading in rows:
        if reading.sensor == sensor:
            at = reading.measured_at
            maxima[at] = max(reading.temperature, maxima.get(at, reading.temperature))
    segments, gap_rows = [], []
    for at, value in sorted(maxima.items()):
        if segments:
            before = segments[-1][-1][0]
            distance = datetime.fromisoformat(at) - datetime.fromisoformat(before)
            if distance > timedelta(seconds=gap):
                gap_rows.append({"sensor": sensor, "before_at": before, "after_at": at})
                segments.append([])
        else:
            segments.append([])
        segments[-1].append((at, value))

    events = []
    for group_index, group in enumerate(segments):
        i = 0
        while i + 2 < len(group):
            if not all(value >= high for _, value in group[i:i + 3]):
                i += 1
                continue
            recovered = next((j for j in range(i + 3, len(group)) if group[j][1] <= recovery), None)
            end = recovered if recovered is not None else len(group) - 1
            status = "recuperada" if recovered is not None else ("lacuna" if group_index + 1 < len(segments) else "aberta")
            events.append({"sensor": sensor, "started": group[i][0], "ended": None if status == "aberta" else group[end][0],
                           "status": status, "peak": max(value for _, value in group[i:end + 1]),
                           "observed_until": group[end][0]})
            i = end + 1
    return {"events": events[::-1], "gaps": gap_rows[::-1]}


class AlertReviewTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / ".cache" / "testes"
        root.mkdir(parents=True, exist_ok=True)
        work = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.path = self.work / "central.sqlite3"
        self.history = History(self.path)

    def test_oraculo_independente_com_atrasos_empates_reenvios_e_reaberturas(self):
        # Mesma fonte, duas ordens de chegada e regras diferentes por sensor.
        profile = [25, 28, 30, 31, 27, 26, 33, 34, 35, 30, 26, 25]
        stream = []
        for i in range(48):
            offset = i * 2 + (i // 12) * 15
            for index, sensor in enumerate(("a", "b")):
                row = sample(i, offset + 0.125, profile[(i + index) % len(profile)], sensor)
                stream.append(row)
                if i % 5 == 0:
                    stream.append(replace(row, id=f"{1000+i:032x}", temperature=32))
                if i % 7 == 0:
                    stream.append(row)
        for seed in (16, 73):
            h = History(self.work / f"ordem-{seed}.sqlite3")
            h.configure("b", high=30, recovery=27, gap_seconds=8)
            entries = list(stream)
            random.Random(seed).shuffle(entries)
            committed = []
            for n, entry in enumerate(entries):
                h.record(entry)
                committed.append(entry)
                if n % 19 == 0 or n + 1 == len(entries):
                    h = History(h.path)
                    self.assertEqual(h.occurrences("a"), reference(committed, "a"))
                    self.assertEqual(h.occurrences("b"), reference(committed, "b", 30, 27, 8))
            self.assertEqual(h.snapshot()["total"], len({(r.sensor,r.id) for r in entries}))

    def test_recuperacao_empatada_pode_deixar_de_ser_fronteira(self):
        rows = [sample(i, i * 2, v) for i, v in enumerate((25,29,31,29,26,32,33,34,26))]
        for row in rows:
            self.history.record(row)
        self.assertEqual(len(self.history.occurrences("laboratorio")["events"]), 2)
        correction = replace(rows[4], id=f"{99:032x}", temperature=28)
        self.history.record(correction)
        expected = reference([*rows, correction], "laboratorio")
        self.assertEqual(len(expected["events"]), 1)
        self.assertEqual(self.history.occurrences("laboratorio"), expected)

    def test_recalculo_local_e_id_novo_abaixo_da_maxima(self):
        rows = [sample(i, 2*i, (25,28,29,30,26,25)[i % 6]) for i in range(120)]
        for row in rows:
            self.history.record(row)
        visited = []
        observe = alerts.State.observe

        def counted(state, db, sensor, rule, at, value):
            visited.append(at)
            return observe(state, db, sensor, rule, at, value)

        late = sample(130, 121, 31)
        equal_time = replace(rows[1], id=f"{131:032x}", temperature=20)
        with patch.object(alerts.State, "observe", counted):
            self.history.record(late)
            self.assertEqual(visited, [sample(0,i,0).measured_at for i in (120,121,122,124,126,128)])
            visited.clear()
            self.assertEqual(self.history.record(equal_time), "gravada")
            self.assertEqual(self.history.record(equal_time), "repetida")
            self.assertEqual(visited, [])
        self.assertEqual(self.history.occurrences("laboratorio"), reference(rows + [late, equal_time], "laboratorio"))
        self.assertEqual(self.history.snapshot()["total"], 122)

    def test_leitor_e_reenvio_durante_transacao_de_escrita(self):
        rows = [sample(i, i*2, v) for i,v in enumerate((25,28,30,31,26))]
        for row in rows:
            self.history.record(row)
        before = self.history.snapshot()
        entered, release = Event(), Event()
        original = alerts.State.observe

        def pause_after_observation(state, *args):
            original(state, *args)
            entered.set()
            if not release.wait(12):
                raise TimeoutError("Teste não liberou o escritor")

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(alerts.State, "observe", pause_after_observation):
            writer = pool.submit(self.history.configure, "laboratorio", high=40)
            try:
                self.assertTrue(entered.wait(5))
                with patch("telemetria.storage.BUSY_TIMEOUT", 0.15):
                    other = History(self.path)
                    self.assertEqual(other.snapshot(), before)
                    self.assertEqual(other.occurrences("laboratorio"), reference(rows, "laboratorio"))
                    self.assertEqual(other.record(rows[1]), "repetida")
                    with self.assertRaises(ReadingConflict):
                        other.record(replace(rows[1], temperature=70))
            finally:
                release.set()
            writer.result(timeout=8)
        self.assertEqual(other.occurrences("laboratorio"), reference(rows, "laboratorio", high=40))
        self.assertEqual(other.configure("laboratorio").high, 40)

    def test_nenhum_recibo_antes_de_resolver_a_primeira_gravacao_do_id(self):
        original = alerts.apply_reading
        for mode in ("igual", "conflito", "rollback"):
            with self.subTest(mode=mode):
                h = History(self.work / f"{mode}.sqlite3")
                first = sample(101, 5, 30)
                contender = first if mode == "igual" else replace(first, temperature=35)
                inserted, missed, release = Event(), Event(), Event()

                def pause(db, row):
                    original(db, row)
                    if not inserted.is_set():
                        inserted.set()
                        if not release.wait(12):
                            raise TimeoutError("Teste não liberou a primeira transação")
                        if mode == "rollback":
                            raise RuntimeError("gravação cancelada")

                class CompetingHistory(History):
                    def _already_recorded(self, db, reading):
                        found = super()._already_recorded(db, reading)
                        if not found:
                            missed.set()
                        return found

                other = CompetingHistory(h.path)
                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(alerts, "apply_reading", pause):
                    winner = pool.submit(h.record, first)
                    try:
                        self.assertTrue(inserted.wait(5))
                        pending = pool.submit(other.record, contender)
                        self.assertTrue(missed.wait(5))
                        self.assertFalse(pending.done())
                    finally:
                        release.set()
                    if mode == "rollback":
                        with self.assertRaises(RuntimeError):
                            winner.result(timeout=8)
                        self.assertEqual(pending.result(timeout=8), "gravada")
                    else:
                        self.assertEqual(winner.result(timeout=8), "gravada")
                        if mode == "igual":
                            self.assertEqual(pending.result(timeout=8), "repetida")
                        else:
                            with self.assertRaises(ReadingConflict):
                                pending.result(timeout=8)
                snap = h.snapshot()
                self.assertEqual(snap["total"], 1)
                self.assertEqual(snap["rows"][0]["temperature"], 35 if mode == "rollback" else 30)

    def test_um_retrato_do_painel_nao_mistura_dois_commits(self):
        rows = [sample(i, offset, temp) for i, (offset,temp) in enumerate(((0,29),(4,30),(6,31),(8,26)))]
        for row in rows:
            self.history.record(row)
        before = self.history.snapshot()
        self.assertEqual(len(before["events"]), 1)
        entered, release = Event(), Event()
        report = alerts.report

        def pause(db, sensor=None):
            entered.set()
            if not release.wait(12):
                raise TimeoutError("Teste não liberou o leitor")
            return report(db, sensor)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(alerts, "report", pause):
            reading = pool.submit(self.history.snapshot)
            try:
                self.assertTrue(entered.wait(5))
                self.history.record(sample(15, 2, 25))
            finally:
                release.set()
            self.assertEqual(reading.result(timeout=8), before)
        after = self.history.snapshot()
        self.assertEqual(after["total"], 5)
        self.assertEqual(after["events"], [])

    def test_dois_processos_com_atrasos_reenvios_e_mudancas_parciais_da_regra(self):
        rows = [sample(i, i*2, (25,30,31,35,29,26)[i % 6]) for i in range(36)]
        script = """
import sys, time
from pathlib import Path
from telemetria.storage import History, Reading
h = History(sys.argv[1])
side = int(sys.argv[2])
markers = [Path(sys.argv[1]).parent / ('pronto-' + str(i)) for i in (0, 1)]
markers[side].touch()
deadline = time.monotonic() + 10
while not markers[1-side].exists():
    if time.monotonic() > deadline:
        raise TimeoutError('Segundo escritor ausente')
    time.sleep(0.01)
for i, payload in enumerate(sys.stdin.buffer):
    if i == 8:
        h.configure('laboratorio', **({'high':30} if side == 0 else {'gap_seconds':5}))
    h.record(Reading.decode(payload))
"""
        args = [sys.executable, "-B", "-c", script, str(self.path)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = []
            for side, ordered in enumerate((rows, rows[::-1])):
                data = b"\n".join(r.encode() for r in ordered)
                jobs.append(pool.submit(subprocess.run, args + [str(side)], input=data, capture_output=True,
                                        timeout=25, cwd=ROOT))
            for job in jobs:
                result = job.result(timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
        h = History(self.path)
        self.assertEqual(h.snapshot()["total"], len(rows))
        self.assertEqual(h.configure("laboratorio"), alerts.Rule(30,26,5))
        self.assertEqual(h.occurrences("laboratorio"), reference(rows, "laboratorio", high=30, gap=5))

    def test_interrupcao_de_processo_durante_correcao_ou_mudanca_de_regra(self):
        rows = [sample(i, 2*i, v) for i,v in enumerate((25,28,30,32,29,26,25))]
        for row in rows:
            self.history.record(row)
        late = sample(21, 3, 35)
        before = self.history.snapshot()
        script = """
import os, sys
from telemetria import alerts
from telemetria.storage import History, Reading
h = History(sys.argv[1])
observe = alerts.State.observe
def stop(state, *args):
    observe(state, *args)
    if state.streak == 3:
        os._exit(23)
alerts.State.observe = stop
if sys.argv[2] == 'atraso':
    h.record(Reading.decode(sys.stdin.buffer.read()))
else:
    h.configure('laboratorio', high=27)
raise RuntimeError('A transação deveria ter sido interrompida')
"""
        for mode in ("atraso", "regra"):
            with self.subTest(mode=mode):
                p = subprocess.run([sys.executable, "-B", "-c", script, str(self.path), mode],
                                   input=late.encode(), capture_output=True, cwd=ROOT, timeout=15)
                self.assertEqual(p.returncode, 23, p.stderr)
                h = History(self.path)
                self.assertEqual(h.snapshot(), before)
                self.assertEqual(h.configure("laboratorio").high, 28)
        self.assertEqual(h.record(late), "gravada")
        self.assertEqual(h.record(late), "repetida")
        self.assertEqual(h.occurrences("laboratorio"), reference(rows + [late], "laboratorio"))

    def test_migracao_interrompida_preserva_base_e_pode_repetir(self):
        path = self.work / "legado.sqlite3"
        rows = [sample(i, i, 30) for i in range(3)]
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE readings(sensor TEXT,id TEXT,measured_at TEXT,temperature REAL,humidity REAL,
                       received_at TEXT, PRIMARY KEY(sensor,id))""")
            db.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?)", [
                (r.sensor,r.id,r.measured_at,r.temperature,r.humidity,r.measured_at) for r in rows])
        with patch.object(alerts.State, "save", side_effect=RuntimeError("interrupção controlada")):
            with self.assertRaises(RuntimeError):
                History(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM readings").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'thermal_%'").fetchone()[0], 0)
        self.assertEqual(History(path).occurrences("laboratorio"), reference(rows, "laboratorio"))

    def test_recusa_versao_desconhecida_antes_de_criar_tabelas(self):
        path = self.work / "futuro.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=10")
        with self.assertRaisesRegex(ValueError, "incompatível"):
            History(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)

    def test_cartao_e_grafico_usam_maxima_mesmo_com_mais_de_60_ids(self):
        rows = [sample(i, i/10, 29) for i in range(3)]
        for row in rows:
            self.history.record(row)
        for i in range(65):
            self.history.record(sample(i+20, 0.2, 25))
        winner = replace(sample(99, 0.2, 32), humidity=71)
        self.history.record(winner)
        view = self.history.snapshot()
        self.assertEqual(view["total"], 69)
        self.assertEqual(len(view["rows"]), 60)
        self.assertEqual(view["rows"][0]["id"], winner.id)
        self.assertEqual([r["temperature"] for r in view["trend"]], [29,29,32])
        self.assertEqual(view["events"][0]["peak"], 32)
        page = render(view, live=False)
        for visible in ("32,00</strong>", "71,00</strong>", "14:02:00,2", "0,2 s"):
            self.assertIn(visible,page)
