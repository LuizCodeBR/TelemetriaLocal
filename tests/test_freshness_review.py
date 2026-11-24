"""Confronta o histórico de atualidade com a ordem dos commits e com os dados brutos."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
import unittest
from unittest.mock import patch

import test_freshness as original
from telemetria import timeline
from telemetria.communication import Observer, stamp
from telemetria.storage import History, ReadingConflict


class FreshnessReviewTests(unittest.TestCase):
    setUp = original.FreshnessTests.setUp
    at = original.FreshnessTests.at
    contact = original.FreshnessTests.contact
    reading = original.FreshnessTests.reading
    read = original.FreshnessTests.read
    stale = original.FreshnessTests.stale

    def test_reinicio_com_relogio_recuado_preserva_medicao_ja_confirmada(self):
        self.stale()
        self.moment = self.at(24)
        confirmed = self.reading(23,2)
        self.h.record(confirmed)
        first, = self.read(24)["events"]
        self.h = History(self.path,received_clock=lambda:self.moment)
        self.monitor = Observer(self.h)
        self.monitor.start(self.at(14))
        self.contact(15)
        data = self.read(16)
        self.assertEqual(data["current"][0]["measured"],confirmed.measured_at)
        self.assertEqual(data["current"][0]["freshness"],"atualizada")
        self.moment = self.at(20)
        self.assertEqual(self.h.record(self.reading(17,3)),"gravada")
        self.assertEqual(self.h.record(confirmed),"repetida")
        self.contact(24); self.contact(31)
        self.assertEqual(self.read(31)["events"],[first])
        self.contact(35)
        next_event, preserved = self.read(35)["events"]
        self.assertEqual(preserved,first)
        self.assertEqual(next_event["baseline"],confirmed.measured_at)

    def test_recibo_apos_silencio_preserva_lacuna_mesmo_se_o_relogio_voltar(self):
        self.stale()
        self.moment = self.at(18)
        self.h.record(self.reading(-2,2))
        self.moment = self.at(30)  # 15 segundos exatos sem verificação de presença
        self.h.record(self.reading(30,4,"b"))
        before, = self.read(30,"a")["events"]
        self.assertEqual((before["outcome"],before["until"]),("sem_observacao",stamp(self.at(18))))
        self.h = History(self.path,received_clock=lambda:self.moment)
        self.moment = self.at(20)
        self.assertEqual(self.h.record(self.reading(19,3)),"gravada")
        self.assertEqual(self.h.record(self.reading(19,3)),"repetida")
        reverted = self.read(20,"a")
        self.assertEqual(reverted["events"],[before])
        self.assertEqual(reverted["current"][0]["freshness"],"sem_observacao")
        band, = timeline.period(self.h,"a",stamp(self.at(14)),stamp(self.at(24)),now=self.at(20))["freshness"]
        self.assertEqual((band["until"],band["outcome"]),(before["until"],before["outcome"]))
        self.contact(24); self.contact(30)
        self.assertEqual(self.read(30,"a")["events"],[before])
        self.contact(38); self.contact(41)
        self.assertEqual(len(self.read(41,"a")["events"]),2)
        latest = self.reading(40,5)
        self.h.record(latest)
        second,old = self.read(41,"a")["events"]
        self.assertEqual(old,before)
        self.assertEqual((second["detected"],second["recovery_id"]),(stamp(self.at(41)),latest.id))

    def test_ultimas_cinquenta_incluem_recuperacao_recente_de_ocorrencia_antiga(self):
        self.stale()
        self.contact(15,"b")
        self.h.configure("b",gap_seconds=.5)
        self.h.record(self.reading(15,1,"b"))
        for i in range(52):
            sec=17+2*i
            self.contact(sec,"b")
            self.h.record(self.reading(sec,i+2,"b"))
        before=self.read(sec)
        self.assertEqual(len(before["events"]),51)
        projected=self.read(sec+15)
        self.assertEqual(len(projected["events"]),50)
        self.assertIn("a",[e["sensor"] for e in projected["events"]])
        self.moment=self.at(sec+1)
        self.h.record(self.reading(sec+1,2))
        last=self.read(sec+1)["events"]
        self.assertEqual(len(last),50)
        self.assertEqual({e["id"] for e in last},
                         {e["id"] for e in sorted(self.read(sec+1,"a")["events"]+self.read(sec+1,"b")["events"],
                                                  key=lambda e:(e["ended"],e["id"]),reverse=True)[:50]})

    def test_encerramento_do_processo_antes_e_depois_do_commit_com_reenvio(self):
        self.stale()
        code = """
import os,sys,json
from datetime import datetime
from contextlib import contextmanager
from telemetria.storage import History,Reading
path,mode,when,payload=sys.argv[1:]
h=History(path,received_clock=lambda:datetime.fromisoformat(when))
connect=h.connect
@contextmanager
def hooked():
    with connect() as db:
        if mode=="durante":
            db.create_function("parar",0,lambda:os._exit(71))
            db.execute("CREATE TEMP TRIGGER ensaio AFTER UPDATE OF outcome ON freshness_events BEGIN SELECT parar(); END")
        yield db
h.connect=hooked
h.record(Reading(**json.loads(payload)))
os._exit(72)
"""
        r=self.reading(19,2)
        for mode,exit_code in (("durante",71),("depois",72)):
            proc=subprocess.run([sys.executable,"-B","-c",code,str(self.path),mode,stamp(self.at(20)),r.encode().decode()],
                                cwd=os.path.dirname(os.path.dirname(__file__)),timeout=15,capture_output=True)
            self.assertEqual(proc.returncode,exit_code,proc.stderr)
            self.h=History(self.path,received_clock=lambda:self.at(21))
            event,=self.read(21)["events"]
            with self.h.connect() as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
                row=db.execute("SELECT received_at FROM readings WHERE sensor=? AND id=?",(r.sensor,r.id)).fetchone()
            if mode=="durante":
                self.assertIsNone(row)
                self.assertIsNone(event["ended"])
            else:
                self.assertEqual(row["received_at"],event["recovery_received"])
                self.assertEqual(event["recovery_id"],r.id)
                self.assertEqual(self.h.record(r),"repetida")
                self.assertEqual(self.read(21)["events"],[event])

    def test_ordem_real_dos_commits_define_recuperacao_mesmo_com_reenvios(self):
        self.stale()
        gate=Barrier(6)
        batch=[self.reading(19,2),self.reading(18,3),self.reading(19,2),
               self.reading(1,4),self.reading(18,3),self.reading(25,5)]
        def submit(r):
            h=History(self.path,received_clock=lambda:self.at(20))
            gate.wait(6)
            return h.record(r)
        with ThreadPoolExecutor(len(batch)) as pool:
            receipts=list(pool.map(submit,batch))
        self.assertCountEqual(receipts,["gravada"]*4+["repetida"]*2)
        event,=self.read(20)["events"]
        with self.h.connect() as db:
            eligible=db.execute("""SELECT id,measured_at,received_at FROM readings
                     WHERE sensor='a' AND measured_at BETWEEN ? AND ? ORDER BY rowid""",
                     (stamp(self.at(10)),stamp(self.at(20)))).fetchall()
        self.assertEqual((event["recovery_id"],event["recovery_measured"],event["recovery_received"]),tuple(eligible[0]))
        self.assertEqual(self.read(20)["current"][0]["measured"],stamp(self.at(19)))

    def test_fila_de_escrita_reavalia_idade_e_duplicata_confirmada_pode_ler(self):
        self.stale()
        candidate=self.reading(10,2)
        self.moment=self.at(20)
        waiting=Event()
        connect=self.h.connect
        @contextmanager
        def tracked():
            with connect() as db:
                db.set_trace_callback(lambda sql:waiting.set() if sql=="BEGIN IMMEDIATE" else None)
                yield db
        with patch.object(self.h,"connect",tracked),ThreadPoolExecutor(1) as pool:
            with connect() as writer:
                writer.execute("BEGIN IMMEDIATE")
                pending=pool.submit(self.h.record,candidate)
                self.assertTrue(waiting.wait(5))
                self.moment=self.at(20.000001)
            self.assertEqual(pending.result(timeout=6),"gravada")
        @contextmanager
        def no_wait():
            with connect() as db:
                db.execute("PRAGMA busy_timeout=0")
                yield db
        with connect() as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(self.h,"connect",no_wait):
                self.assertEqual(self.h.record(candidate),"repetida")
                with self.assertRaises(ReadingConflict):
                    self.h.record(self.reading(20,2))
        self.assertIsNone(self.read(21)["events"][0]["ended"])

    def test_aberturas_simultaneas_da_base_090_preservam_recibo_e_ocorrencia_aberta(self):
        self.stale()
        self.moment = self.at(18)
        recovery = self.reading(18, 2)
        self.h.record(recovery)
        self.contact(24)
        self.contact(29)
        expected = self.read(29)
        self.assertEqual([event["outcome"] for event in expected["events"]], [None, "atualizada"])
        with self.h.connect() as db:
            db.execute("ALTER TABLE freshness_watch DROP COLUMN enrolled")
            db.execute("DROP TABLE freshness_break")
            db.execute("DROP INDEX freshness_closed_time")
            db.execute("PRAGMA user_version=8")
        gate = Barrier(2)
        def reopen(_):
            gate.wait(5)
            return History(self.path, received_clock=lambda: self.moment)
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(reopen, range(2))
        self.h = first
        self.assertEqual(self.read(29), expected)
        self.assertEqual(second.record(recovery), "repetida")
        self.moment = self.at(30)
        next_reading = self.reading(30, 3)
        second.record(next_reading)
        events = self.read(30)["events"]
        self.assertEqual([e["recovery_id"] for e in events], [next_reading.id, recovery.id])
        with first.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_falha_no_registro_da_lacuna_reverte_recibo_e_historico_das_duas_salas(self):
        import sqlite3
        self.contact(0, "b")
        self.stale()
        original_events = self.read(15)["events"]
        self.assertEqual({e["sensor"] for e in original_events}, {"a", "b"})
        with self.h.connect() as db:
            db.execute("""CREATE TRIGGER falhar BEFORE INSERT ON freshness_break
                          BEGIN SELECT RAISE(ABORT,'falha no marcador'); END""")
        self.moment = self.at(30)
        receipt = self.reading(30, 5, "b")
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.record(receipt)
        self.assertEqual(self.read(15)["events"], original_events)
        with self.h.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM readings WHERE id=?", (receipt.id,)).fetchone())
            db.execute("DROP TRIGGER falhar")
        self.assertEqual(self.h.record(receipt), "gravada")
        self.assertEqual(self.h.record(receipt), "repetida")
        closed = self.read(15)["events"]
        self.assertEqual([e["id"] for e in closed], [e["id"] for e in original_events])
        self.assertTrue(all(e["outcome"] == "sem_observacao" and e["ended"] == stamp(self.at(15))
                            and e["recovery_id"] is None for e in closed))
