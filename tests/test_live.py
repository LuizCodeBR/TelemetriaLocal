import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.request import urlopen

from telemetria.__main__ import run
from telemetria.dashboard import dashboard
from telemetria.storage import History, Outbox
from telemetria.transport import broker, run_sensor, station


async def eventually(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.05)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.history = History(self.directory / "central.sqlite3")
        self.outbox = Outbox(self.directory / "sensor.sqlite3")

    async def test_sensor_em_execucao_mede_durante_indisponibilidade(self):
        async with broker(0) as port:
            pass
        worker = asyncio.create_task(run_sensor(self.outbox, "sala-01", port, 0.18))
        try:
            await eventually(lambda: self.outbox.count("sala-01") >= 5)
            offline = self.outbox.batch("sala-01")
            self.assertEqual(self.history.snapshot()["total"], 0)
            async with station(self.history, port):
                wanted = {(r.id, r.measured_at) for r in offline}
                offline_ids = {r.id for r in offline}

                def arrived():
                    stored = {(r["id"], r["measured_at"]) for r in self.history.snapshot()["rows"]}
                    pending = {r.id for r in self.outbox.batch("sala-01")}
                    return wanted <= stored and pending.isdisjoint(offline_ids)

                await eventually(arrived)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_iniciar_entrega_dados_ao_painel_ao_vivo(self):
        async with broker(0) as mqtt:
            with dashboard(self.history, 0) as web:
                pass
        args = SimpleNamespace(modo="iniciar", dados=self.directory, mqtt=mqtt, web=web,
                               sensor="sala-ensaio", intervalo=0.2)
        platform = asyncio.create_task(run(args))
        try:
            await eventually(lambda: self.history.snapshot()["total"] >= 2)
            with urlopen(f"http://127.0.0.1:{web}/", timeout=2) as response:
                page = response.read().decode()
            self.assertIn("sala-ensaio", page)
            self.assertIn("Ao vivo", page)
            self.assertIn('http-equiv="refresh"', page)
        finally:
            platform.cancel()
            await asyncio.gather(platform, return_exceptions=True)
