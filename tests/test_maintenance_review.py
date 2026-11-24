"""Reenvio, término abrupto do processo e leituras concorrentes da agenda."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import subprocess
import sys
from threading import Event
from unittest import TestCase
from unittest.mock import patch

import test_maintenance as original
from telemetria import incidents, maintenance, timeline
from telemetria.communication import stamp
from telemetria.storage import History


class MaintenanceReviewTests(TestCase):
    setUp = original.MaintenanceTests.setUp
    at = original.MaintenanceTests.at
    window = original.MaintenanceTests.window
    cancel = original.MaintenanceTests.cancel

    def test_recibos_confirmados_com_outro_escritor_na_fila(self):
        first, second = self.window(), self.window(2,20,30)
        saved = maintenance.update(self.h,first,now=self.base)
        maintenance.update(self.h,second,now=self.base)
        canceled = self.cancel(second)
        connect = self.h.connect
        @contextmanager
        def immediately():
            with connect() as db:
                db.execute("PRAGMA busy_timeout=0")
                yield db
        with connect() as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(self.h,"connect",immediately):
                self.assertEqual(maintenance.update(self.h,first,now=self.at(25)),{"created":False,"at":saved["at"]})
                self.assertEqual(self.cancel(second,25),{"created":False,"at":canceled["at"]})
                replay = maintenance.update(self.h,second,now=self.at(25))
                self.assertEqual(replay["canceled_at"],canceled["at"])
                with self.assertRaises(maintenance.Conflict):
                    maintenance.update(self.h,{**second,"reason":"Outro"},now=self.base)
                with self.assertRaises(maintenance.Conflict):
                    self.cancel({**second,"origin":"outra"},25)

    def test_relogio_recuado_apos_nota_preserva_uma_janela_ja_iniciada(self):
        original_window = self.window(start=20,end=40)
        maintenance.update(self.h,original_window,now=self.base)
        for n in (8,15,16):
            self.m.advance(self.at(n),"b",str(n))
        event = incidents.capture(self.h,room="a",now=self.at(16))["detail"]
        incidents.record(self.h,int(event["id"]),"nota",{"texto":"Conferido","chave":"b"*32},now=self.at(27))
        self.h = History(self.path)
        self.assertEqual(maintenance.agenda(self.h,now=self.at(17))["now"],stamp(self.at(27)))
        with self.assertRaises(maintenance.Conflict):
            self.cancel(original_window,at=17)
        with self.assertRaises(maintenance.Conflict):
            maintenance.update(self.h,self.window(3,21,29,"b"),now=self.at(17))
        for n in (23,30):
            self.m.advance(self.at(n),"b",str(n))
        self.m.advance(self.at(32),"a","voltou")
        detail = incidents.capture(self.h,room="a",now=self.at(32))["detail"]
        self.assertEqual([w["reason"] for w in detail["maintenance"]],["Troca de bateria"])

    def test_abrir_base_080_preserva_recibos_janelas_e_origem(self):
        w = self.window()
        maintenance.update(self.h,w,now=self.base)
        self.cancel(w)
        with self.h.connect() as db:
            db.execute("DROP INDEX comm_actions_by_time")
        with ThreadPoolExecutor(2) as pool:
            instances=list(pool.map(History,[self.path,self.path]))
        for history in instances:
            data=maintenance.agenda(history,now=self.at(2))
            self.assertEqual(data["origin"],self.origin)
            self.assertEqual(data["windows"][0]["canceled_at"],stamp(self.at(1)))
            with history.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],9)
                self.assertTrue(db.execute("SELECT 1 FROM sqlite_schema WHERE name='comm_actions_by_time'").fetchone())

    def test_processo_interrompido_antes_e_depois_da_confirmacao(self):
        child = """
import json,os,sys
from contextlib import contextmanager
from datetime import datetime
from telemetria import maintenance
from telemetria.storage import History
path,raw,when,verb,phase=sys.argv[1:]
h=History(path)
connect=h.connect
@contextmanager
def hook():
    with connect() as db:
        if phase=="antes":
            db.create_function("interromper",0,lambda:os._exit(71))
            op="UPDATE OF canceled_at" if verb=="cancelar" else "INSERT"
            db.execute("CREATE TEMP TRIGGER parar AFTER "+op+" ON maintenance_windows BEGIN SELECT interromper(); END")
        yield db
h.connect=hook
maintenance.update(h,json.loads(raw),cancel=verb=="cancelar",now=datetime.fromisoformat(when))
os._exit(72)
"""
        w = self.window()
        def kill(verb,phase):
            payload = w if verb=="agendar" else {k:w[k] for k in ("key","origin")}
            proc = subprocess.run([sys.executable,"-B","-c",child,str(self.path),json.dumps(payload),
                                   stamp(self.at(1)),verb,phase],cwd=os.path.dirname(os.path.dirname(__file__)),
                                  timeout=12,capture_output=True)
            self.assertEqual(proc.returncode,71 if phase=="antes" else 72,proc.stderr)
            self.h = History(self.path)
            with self.h.connect() as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            return maintenance.agenda(self.h,now=self.at(2))["windows"]
        self.assertEqual(kill("agendar","antes"),[])
        row, = kill("agendar","depois")
        self.assertIsNone(row["canceled_at"])
        self.assertFalse(maintenance.update(self.h,w,now=self.at(2))["created"])
        row, = kill("cancelar","antes")
        self.assertIsNone(row["canceled_at"])
        row, = kill("cancelar","depois")
        self.assertEqual(row["canceled_at"],stamp(self.at(1)))
        self.assertFalse(self.cancel(w,at=3)["created"])
        self.assertEqual(len(maintenance.agenda(self.h,now=self.at(3))["windows"]),1)

    def test_relatorio_durante_substituicao_nao_combina_duas_versoes(self):
        old=self.window(1,20,40)
        maintenance.update(self.h,old,now=self.base)
        ready,done=Event(),Event()
        connect=self.h.connect
        other=History(self.path)
        def replace():
            if not ready.wait(5):
                raise TimeoutError("A leitura não chegou à consulta das janelas.")
            try:
                maintenance.update(other,{k:old[k] for k in ("key","origin")},cancel=True,now=self.at(3))
                maintenance.update(other,self.window(2,22,42),now=self.at(4))
            finally:
                done.set()
        @contextmanager
        def reading():
            with connect() as db:
                def pause(sql):
                    if "FROM maintenance_windows" in sql:
                        ready.set()
                        done.wait(5)
                db.set_trace_callback(pause)
                yield db
        def capture():
            return timeline.period(self.h,"a",stamp(self.at(19)),stamp(self.at(41)),now=self.at(5))
        with ThreadPoolExecutor(1) as pool:
            writer=pool.submit(replace)
            with patch.object(self.h,"connect",reading):
                before=capture()
            writer.result(timeout=8)
        for result,left,right in [(before,20,40),(capture(),22,42)]:
            room=result["availability"]["rooms"][0]
            self.assertEqual([(w["start"],w["end"]) for w in room["maintenance"]],
                             [(stamp(self.at(left)),stamp(self.at(right)))])
