import asyncio
from contextlib import asynccontextmanager, closing
import sqlite3
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

import mqttools

from telemetria import transport

from telemetria.dashboard import dashboard
from telemetria.demo import demonstrate
from telemetria.storage import History, Outbox, Reading, ReadingConflict, database_call
from telemetria.transport import broker, client, deliver, flush, station, synchronize, topic

SENSOR = "sala-01"


class Databases:
    def setUp(self):
        temp = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        temp.mkdir(parents=True, exist_ok=True)
        isolated = tempfile.TemporaryDirectory(dir=temp)
        self.addCleanup(isolated.cleanup)
        self.directory = Path(isolated.name)
        self.outbox = Outbox(self.directory / "sensor.sqlite3")
        self.history = History(self.directory / "central.sqlite3")
        self.reading = Reading.sample(SENSOR)


class StorageTests(Databases, unittest.TestCase):
    def test_inclusao_e_remocao_concorrentes_sao_idempotentes(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(self.outbox.put, [self.reading] * 10))
            self.assertEqual(self.outbox.batch(SENSOR), [self.reading])
            with self.assertRaises(ReadingConflict):
                self.outbox.put(replace(self.reading, temperature=98))
            self.assertEqual(self.outbox.batch(SENSOR), [self.reading])
            removed = list(pool.map(self.outbox.acknowledge, [self.reading] * 10))
        self.assertEqual(sum(removed), 1)
        self.assertEqual(self.outbox.count(SENSOR), 0)

    def test_abre_fila_da_versao_anterior(self):
        path = self.directory / "antiga.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE pending (sensor TEXT NOT NULL, id TEXT NOT NULL, payload BLOB NOT NULL,
                       PRIMARY KEY (sensor, id))""")
            db.execute("INSERT INTO pending VALUES (?, ?, ?)", (SENSOR, self.reading.id, self.reading.encode()))
        upgraded = Outbox(path)
        self.assertEqual(upgraded.batch(SENSOR), [self.reading])
        self.assertEqual(upgraded.conflicts(SENSOR), [])
        upgraded.mark_conflict(self.reading)
        self.assertEqual(Outbox(path).conflicts(SENSOR)[0][0], self.reading)

    def test_dois_processos_gravam_a_mesma_leitura(self):
        script = """
import sys
from telemetria.storage import History, Outbox, Reading
outbox, history = Outbox(sys.argv[1]), History(sys.argv[2])
print("pronto", flush=True)
row = Reading.decode(sys.stdin.buffer.readline())
outbox.put(row)
print(history.record(row), flush=True)
"""
        args = [sys.executable, "-B", "-c", script, str(self.outbox.path), str(self.history.path)]
        workers = []
        try:
            for _ in range(2):
                workers.append(subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, cwd=Path(__file__).resolve().parents[1]))
            for worker in workers:
                self.assertEqual(worker.stdout.readline().strip(), b"pronto")
            for worker in workers:
                worker.stdin.write(self.reading.encode() + b"\n")
                worker.stdin.flush()
            results = []
            for worker in workers:
                output, error = worker.communicate(timeout=15)
                self.assertEqual(worker.returncode, 0, error)
                results.append(output.strip())
            self.assertCountEqual(results, [b"gravada", b"repetida"])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()
        self.assertEqual(self.outbox.batch(SENSOR), [self.reading])
        self.assertEqual(self.history.snapshot()["total"], 1)

    def test_fila_sobrevive_reinicio(self):
        self.outbox.put(self.reading)
        self.assertEqual(Outbox(self.outbox.path).batch(SENSOR), [self.reading])

    def test_mesmo_id_em_sensores_diferentes(self):
        self.history.record(self.reading)
        self.history.record(replace(self.reading, sensor="outra-sala"))
        self.assertEqual(self.history.snapshot()["total"], 2)

    def test_id_com_valor_diferente_preserva_original(self):
        self.history.record(self.reading)
        with self.assertRaises(ValueError):
            self.history.record(replace(self.reading, temperature=100))
        self.assertEqual(self.history.snapshot()["rows"][0]["temperature"], self.reading.temperature)

    def test_duplicatas_concorrentes(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.history.record, [self.reading] * 12))
        self.assertEqual(results.count("gravada"), 1)
        self.assertEqual(results.count("repetida"), 11)
        self.assertEqual(self.history.snapshot()["total"], 1)

    def test_fila_isola_sensores_e_confere_conteudo(self):
        self.outbox.put(self.reading)
        other = replace(self.reading, sensor="outra-sala")
        self.outbox.put(other)
        self.outbox.acknowledge(replace(self.reading, temperature=100))
        self.assertEqual(self.outbox.count(SENSOR), 1)
        self.outbox.acknowledge(self.reading)
        self.assertEqual(self.outbox.batch(other.sensor), [other])

    def test_historico_ordenado_pela_medicao(self):
        earlier = Reading.sample(SENSOR, datetime(2025, 1, 1, tzinfo=UTC))
        later = Reading.sample(SENSOR, datetime(2025, 1, 2, tzinfo=UTC))
        self.history.record(later)
        self.history.record(earlier)
        self.assertEqual([row["id"] for row in self.history.snapshot()["rows"]], [later.id, earlier.id])
        before = self.history.snapshot()
        self.assertEqual(self.history.record(later), "repetida")
        self.assertEqual(before, self.history.snapshot())

    def test_rejeita_campos_invalidos(self):
        for patch in ({"humidity":101}, {"humidity":float("nan")}, {"temperature":True},
                      {"sensor":"sala/#"}, {"measured_at":"2025-01-01T10:00:00"}, {"id":""}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                replace(self.reading, **patch)
        for payload in (b"[]", b"{}", b"x"*2049):
            with self.subTest(payload=payload[:12]), self.assertRaises(ValueError):
                Reading.decode(payload)

    def test_painel_http_vazio_e_com_dados(self):
        with dashboard(self.history, 0) as port:
            url = f"http://127.0.0.1:{port}"
            with urlopen(url, timeout=3) as response:
                self.assertIn("Aguardando sensor", response.read().decode())
            self.history.record(self.reading)
            with urlopen(url, timeout=3) as response:
                page = response.read().decode()
            self.assertIn(SENSOR, page)
            self.assertIn("1</strong>", page)
            self.assertIn("Último recebimento", page)
            with self.assertRaises(HTTPError) as error:
                urlopen(url + "/inexistente", timeout=3)
            self.assertEqual(error.exception.code, 404)


async def until(condition):
    async with asyncio.timeout(12):
        while not condition():
            await asyncio.sleep(0.03)


class NetworkTests(Databases, unittest.IsolatedAsyncioTestCase):
    async def test_saida_abrupta_apos_commit_antes_da_baixa(self):
        # Mantém o WAL aberto e interrompe apenas o subprocesso deste teste.
        script = """
import os, sqlite3, sys
from telemetria.storage import History, Outbox, Reading
queue, hist = Outbox(sys.argv[1]), History(sys.argv[2])
pin = sqlite3.connect(hist.path)
pin.execute("BEGIN")
pin.execute("SELECT count(*) FROM readings").fetchone()
reading = Reading.decode(sys.stdin.buffer.read())
queue.put(reading)
hist.record(reading)
uncommitted = sqlite3.connect(queue.path)
uncommitted.execute("DELETE FROM pending")
os._exit(23)
"""
        run = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-B", "-c", script, str(self.outbox.path), str(self.history.path)],
            input=self.reading.encode(), capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(run.returncode, 23, run.stderr)
        reopened = Outbox(self.outbox.path)
        history = History(self.history.path)
        self.assertEqual(reopened.batch(SENSOR), [self.reading])
        before = history.snapshot()
        self.assertEqual(before["total"], 1)
        async with station(history, 0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                self.assertEqual(await flush(sender, reopened, SENSOR), 1)
        self.assertEqual(reopened.count(SENSOR), 0)
        after = history.snapshot()
        # A central foi ligada e desligada. Os dados térmicos confirmados devem ser idênticos.
        self.assertEqual({k: v for k, v in after.items() if k != "communication"},
                         {k: v for k, v in before.items() if k != "communication"})

    async def test_dois_remetentes_mesmo_lote_atravessam_limite_de_50(self):
        rows = [Reading.sample(SENSOR) for _ in range(53)]
        for row in rows:
            self.outbox.put(row)
        self.history.record(rows[0])  # Commit anterior cujo recibo se perdeu.
        gate = Barrier(2)

        class SameBatch(Outbox):
            def batch(self, sensor, limit=50):
                snapshot = super().batch(sensor, limit)
                gate.wait(timeout=8)
                return snapshot

        queues = [SameBatch(self.outbox.path), SameBatch(self.outbox.path)]
        async with station(self.history, 0) as port:
            async with client(port, topic(SENSOR, "recibos")) as a, client(port, topic(SENSOR, "recibos")) as b:
                counts = await asyncio.gather(flush(a, queues[0], SENSOR), flush(b, queues[1], SENSOR))
                self.assertEqual(sum(counts), 50)
                self.assertEqual(await flush(a, self.outbox, SENSOR), 3)
        snapshot = self.history.snapshot()
        self.assertEqual(snapshot["total"], 53)
        self.assertEqual({(r["id"], r["measured_at"]) for r in snapshot["rows"]},
                         {(r.id, r.measured_at) for r in rows})
        self.assertEqual(self.outbox.count(SENSOR), 0)

    async def test_conflito_persistente_libera_o_proximo_id(self):
        self.history.record(self.reading)
        altered = replace(self.reading, humidity=99)
        good = Reading.sample(SENSOR)
        self.outbox.put(altered)
        self.outbox.put(good)
        async with station(self.history, 0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                with self.assertLogs("telemetria.transport", level="WARNING"):
                    self.assertEqual(await flush(sender, self.outbox, SENSOR), 1)
        restored = Outbox(self.outbox.path)
        restored.put(altered)
        restored.mark_conflict(altered)
        self.assertFalse(restored.acknowledge(altered))
        self.assertEqual(restored.count(SENSOR), 0)
        self.assertEqual([r for r, _ in restored.conflicts(SENSOR)], [altered])
        self.assertEqual(restored.conflicts("outro"), [])
        self.assertEqual(self.history.snapshot()["total"], 2)

    async def test_coletor_ocupado_continua_online_e_confirma_apos_commit(self):
        busy = asyncio.Event()
        loop = asyncio.get_running_loop()
        record = self.history.record

        def observe(row):
            try:
                return record(row)
            except sqlite3.OperationalError:
                loop.call_soon_threadsafe(busy.set)
                raise

        self.outbox.put(self.reading)
        with patch("telemetria.storage.BUSY_TIMEOUT", 0.05), patch.object(self.history, "record", observe):
            async with station(self.history, 0) as port:
                async with client(port, topic(SENSOR, "recibos")) as sender:
                    with closing(sqlite3.connect(self.history.path)) as lock:
                        lock.execute("BEGIN IMMEDIATE")
                        sending = asyncio.create_task(flush(sender, self.outbox, SENSOR))
                        await asyncio.wait_for(busy.wait(), 2)
                        self.assertFalse(sending.done())
                        self.assertEqual(self.outbox.batch(SENSOR), [self.reading])
                        self.assertEqual(self.history.snapshot()["total"], 0)
                        lock.rollback()
                    self.assertEqual(await asyncio.wait_for(sending, 4), 1)
        self.assertEqual(self.history.snapshot()["total"], 1)

    async def test_fila_ocupada_tenta_persistir_a_mesma_amostra(self):
        seen = []
        busy = asyncio.Event()
        loop = asyncio.get_running_loop()
        put = self.outbox.put

        def observe(row):
            seen.append(row)
            try:
                return put(row)
            except sqlite3.OperationalError:
                loop.call_soon_threadsafe(busy.set)
                raise

        with patch("telemetria.storage.BUSY_TIMEOUT", 0.05), patch.object(self.outbox, "put", observe):
            with closing(sqlite3.connect(self.outbox.path)) as lock:
                lock.execute("BEGIN IMMEDIATE")
                sampler = asyncio.create_task(transport.sample_forever(self.outbox, SENSOR, 20))
                try:
                    await asyncio.wait_for(busy.wait(), 2)
                    lock.rollback()
                    await until(lambda: self.outbox.count(SENSOR) == 1)
                finally:
                    sampler.cancel()
                    await asyncio.gather(sampler, return_exceptions=True)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(set(seen), {self.outbox.batch(SENSOR)[0]})

    async def test_erro_sql_permanente_e_propagado(self):
        def bad_query():
            with self.outbox.connect() as db:
                db.execute("SELECT * FROM tabela_inexistente")
        with self.assertRaises(sqlite3.OperationalError):
            await asyncio.wait_for(database_call(bad_query), 2)

    async def test_queda_da_sessao_do_coletor_restabelece_assinatura(self):
        listen = transport.receive_loop
        calls = 0
        reconnected = asyncio.Event()

        async def lose_first(connection, history):
            nonlocal calls
            calls += 1
            if calls == 1:
                await connection.stop()
            else:
                reconnected.set()
            await listen(connection, history)

        with patch.object(transport, "receive_loop", lose_first):
            async with station(self.history, 0) as port:
                await asyncio.wait_for(reconnected.wait(), 5)
                async with client(port, topic(SENSOR, "recibos")) as sender:
                    self.assertEqual(await deliver(sender, self.reading), "gravada")
        self.assertEqual(calls, 2)

    async def test_sensor_reconecta_sem_ter_de_gerar_leitura(self):
        connections = asyncio.Queue()

        @asynccontextmanager
        async def observe(port, sub):
            async with client(port, sub) as connection:
                if sub == topic(SENSOR, "recibos"):
                    connections.put_nowait(port)
                yield connection

        with patch.object(transport, "client", observe):
            job = None
            try:
                async with station(self.history, 0) as port:
                    job = asyncio.create_task(synchronize(self.outbox, SENSOR, port, 0.05, 0.1))
                    self.assertEqual(await asyncio.wait_for(connections.get(), 3), port)
                async with station(self.history, port):
                    self.assertEqual(await asyncio.wait_for(connections.get(), 6), port)
                    self.assertEqual(self.outbox.count(SENSOR), 0)
            finally:
                if job is not None:
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)

    async def test_interrupcao_no_meio_do_lote_preserva_pendentes(self):
        readings = [Reading.sample(SENSOR) for _ in range(6)]
        for reading in readings:
            self.outbox.put(reading)

        async def interrupt_after_commit(connection, reading, timeout):
            result = await deliver(connection, reading, timeout)
            if reading.id == readings[2].id:
                await connection.stop()
                raise ConnectionError("Interrupção antes de baixar o terceiro recibo")
            return result

        async with station(self.history, 0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                with patch.object(transport, "deliver", interrupt_after_commit), self.assertRaises(ConnectionError):
                    await flush(sender, self.outbox, SENSOR)
        self.assertEqual(self.history.snapshot()["total"], 3)
        reopened = Outbox(self.outbox.path)
        self.assertEqual(reopened.batch(SENSOR), readings[2:])
        async with station(self.history, port):
            async with client(port, topic(SENSOR, "recibos")) as sender:
                self.assertEqual(await flush(sender, reopened, SENSOR), 4)
        self.assertEqual(reopened.count(SENSOR), 0)
        self.assertEqual(self.history.snapshot()["total"], 6)

    async def test_recibo_de_conflito_de_outro_conteudo_nao_isola_medicao(self):
        self.outbox.put(self.reading)
        different = replace(self.reading, humidity=98)
        async with broker(0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                sender.publish(mqttools.Message(topic(SENSOR, "recibos"), transport.receipt(different, "conflito")))
                with self.assertRaises(TimeoutError):
                    await flush(sender, self.outbox, SENSOR, timeout=0.2)
        self.assertEqual(self.outbox.batch(SENSOR), [self.reading])
        self.assertFalse(self.outbox.conflicts(SENSOR))

    async def test_reconexao_automatica_apos_queda_real(self):
        self.outbox.put(self.reading)
        async with station(self.history, 0) as port:
            job = asyncio.create_task(synchronize(self.outbox, SENSOR, port, 0.05, 0.15))
            try:
                await until(lambda: self.outbox.count(SENSOR) == 0)
            except BaseException:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                raise
        try:
            batch = [Reading.sample(SENSOR) for _ in range(10)]
            for reading in batch:
                self.outbox.put(reading)
            # Força também uma tentativa com a central realmente indisponível.
            with self.assertRaises(OSError):
                async with client(port, topic(SENSOR, "recibos")):
                    self.fail("A central deveria estar desligada")
            self.assertEqual(self.outbox.count(SENSOR), 10)
            self.assertEqual(self.history.snapshot()["total"], 1)
            async with station(History(self.history.path), port):
                await until(lambda: self.outbox.count(SENSOR) == 0)
            expected = {(r.id, r.measured_at) for r in [self.reading, *batch]}
            actual = {(r["id"], r["measured_at"]) for r in self.history.snapshot()["rows"]}
            self.assertEqual(actual, expected)
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_reinicio_sensor_apos_commit_sem_recibo(self):
        self.outbox.put(self.reading)
        async with station(self.history, 0) as port:
            # O sensor envia e encerra sem tratar o recibo.
            async with client(port, topic(SENSOR, "recibos")) as first:
                first.publish(mqttools.Message(topic(SENSOR, "leituras"), self.reading.encode()))
                await until(lambda: self.history.snapshot()["total"] == 1)
        restored = Outbox(self.outbox.path)
        self.assertEqual(restored.count(SENSOR), 1)
        async with station(History(self.history.path), port):
            async with client(port, topic(SENSOR, "recibos")) as second:
                self.assertEqual(await flush(second, restored, SENSOR), 1)
                self.assertEqual(await deliver(second, self.reading), "repetida")
        self.assertEqual(restored.count(SENSOR), 0)
        self.assertEqual(self.history.snapshot()["total"], 1)

    async def test_sem_coletor_recibo_incorreto_nao_limpa_fila(self):
        self.outbox.put(self.reading)
        async with broker(0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                sender.publish(mqttools.Message(topic(SENSOR, "recibos"), b'{"id":"antigo"}'))
                with self.assertRaises(TimeoutError):
                    await flush(sender, self.outbox, SENSOR, timeout=0.25)
        self.assertEqual(self.outbox.batch(SENSOR), [self.reading])

    async def test_conflito_na_central_nao_confirma_entrega(self):
        self.history.record(self.reading)
        altered = replace(self.reading, temperature=105)
        self.outbox.put(altered)
        async with station(self.history, 0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                with self.assertLogs("telemetria.transport", level="WARNING"):
                    self.assertEqual(await flush(sender, self.outbox, SENSOR), 0)
        self.assertEqual(self.outbox.batch(SENSOR), [])
        self.assertEqual([row for row, _ in self.outbox.conflicts(SENSOR)], [altered])
        self.assertEqual(self.history.snapshot()["rows"][0]["temperature"], self.reading.temperature)

    async def test_topico_incompativel_recusado_coletor_continua(self):
        async with station(self.history, 0) as port:
            async with client(port, topic(SENSOR, "recibos")) as sender:
                with self.assertLogs("telemetria.transport", level="WARNING"):
                    sender.publish(mqttools.Message(topic("outra-sala", "leituras"), self.reading.encode()))
                    self.assertEqual(await deliver(sender, self.reading), "gravada")
        self.assertEqual(self.history.snapshot()["total"], 1)

    async def test_demonstracao_repetivel_e_relatorio_offline(self):
        reports = [await demonstrate(self.directory) for _ in range(2)]
        self.assertNotEqual(reports[0].parent, reports[1].parent)
        for report in reports:
            history = History(report.parent / "central.sqlite3")
            self.assertEqual(history.snapshot()["total"], 10)
            page = report.read_text(encoding="utf-8")
            self.assertIn("Demonstração", page)
            self.assertNotIn('http-equiv="refresh"', page)
            self.assertNotIn("https://", page)
