"""Confirmações reais via MQTT para leituras novas, antigas e repetidas."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from telemetria import freshness_report
from telemetria.communication import stamp
from telemetria.demo_communication import responding, wait_contact
from telemetria.storage import History, Outbox, Reading
from telemetria.transport import client, deliver, flush, station, topic


class FreshnessMqttTests(unittest.IsolatedAsyncioTestCase):
    async def test_fila_reenviada_com_sensor_ainda_em_comunicacao(self):
        cache=Path(__file__).resolve().parents[1]/".cache"/"testes"
        cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            origin=datetime(2026,1,15,14,20,tzinfo=UTC)
            def at(n):
                return origin+timedelta(seconds=n)
            moment=origin
            name="sensor-lab"
            history=History(Path(folder)/"central.sqlite3",received_clock=lambda:moment)
            queue=Outbox(Path(folder)/"fila.sqlite3")
            def reading(n,key):
                return Reading(name,f"{key:032x}",stamp(at(n)),24,52)
            async with station(history,0,clock=lambda:moment,check_interval=.04) as port:
                async with responding(name,port),client(port,topic(name,"recibos")) as link:
                    await wait_contact(history,name,moment)
                    self.assertEqual(await deliver(link,reading(0,1)),"gravada")
                    for n in (5,10,15):
                        moment=at(n)
                        await wait_contact(history,name,moment)
                    self.assertEqual(freshness_report.capture(history,now=moment)["current"][0]["state"],"online")
                    moment=at(20)
                    await wait_contact(history,name,moment)
                    old=reading(-10,2)
                    queue.put(old)
                    self.assertEqual(await flush(link,queue,name),1)
                    self.assertEqual(await deliver(link,old),"repetida")
                    self.assertIsNone(freshness_report.capture(history,now=moment)["events"][0]["outcome"])
                    moment=at(25)
                    await wait_contact(history,name,moment)
                    fresh=reading(24,3)
                    self.assertEqual(await deliver(link,fresh),"gravada")
                    self.assertEqual(await deliver(link,fresh),"repetida")
            final=freshness_report.capture(History(history.path),now=moment)
            event,=final["events"]
            self.assertEqual(event["recovery_id"],fresh.id)
            self.assertEqual(event["ended"],stamp(at(25)))
            self.assertEqual(queue.count(name),0)
