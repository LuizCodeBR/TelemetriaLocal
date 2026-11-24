"""Auditoria dos registros de operador sob contenção, reinício e mudança de pasta."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from urllib.error import HTTPError

import test_incidents as base
from telemetria import incidents
from telemetria.communication import Observer, stamp
from telemetria.dashboard import dashboard
from telemetria.storage import History


class IncidentPersistenceTests(base.IncidentFixture):
    def test_reenvio_confirmado_enquanto_outra_conexao_escreve(self):
        note = {"chave":"b"*32, "texto":"Anotação original"}
        self.action("visto", {}, 19)
        self.action("nota", note, 20)
        connect = self.history.connect

        @contextmanager
        def impatient():
            with connect() as db:
                db.execute("PRAGMA busy_timeout=0")
                yield db
        with connect() as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(self.history, "connect", impatient):
                for kind, payload, when in [("visto",{},19), ("nota",note,20)]:
                    with self.subTest(kind=kind):
                        self.assertEqual(self.action(kind,payload,100), {"at":stamp(self.at(when)),"created":False})
        self.assertEqual(len(self.report()["detail"]["actions"]), 2)

    def test_relogio_recuado_respeita_ultima_observacao_da_queda(self):
        self.monitor.advance(self.at(23), "sala-02", "23")
        self.assertEqual(self.action("visto", {}, 12)["at"], stamp(self.at(23)))
        self.assertEqual(self.action("nota", {"chave":"b"*32,"texto":"Conferido"}, 13)["at"],stamp(self.at(23)))
        self.monitor.advance(self.at(24), "sala-01", "voltou")
        self.assertEqual(self.report()["detail"]["ended"],stamp(self.at(24)))

    def test_aba_antiga_com_mesmo_numero_de_queda_em_outra_pasta(self):
        original = self.report()["detail"].get("token","base-anterior")
        other = History(self.path.with_name("outra.sqlite3"))
        monitor = Observer(other)
        monitor.start(self.at(0))
        monitor.advance(self.at(0),"sala-01","inicial")
        for sec in (8,15,16):
            monitor.advance(self.at(sec))
        with dashboard(other,0) as port:
            url = f"http://127.0.0.1:{port}"
            for action, payload in [("visto",{}),("nota",{"chave":"b"*32,"texto":"Da pasta antiga"})]:
                with self.subTest(action=action):
                    with self.assertRaises(HTTPError) as failure:
                        base.IncidentTests.post(url,f"/api/incidentes/{self.event}/{action}",
                                                {**payload,"ocorrencia":original})
                    self.assertEqual(failure.exception.code,409)
        other_event = incidents.capture(other, now=self.at(16))["detail"]
        self.assertEqual(other_event["id"],str(self.event))
        self.assertEqual(other_event["actions"],[])
        self.assertIsNone(other_event["seen_at"])

    def test_interrupcao_do_processo_distingue_antes_e_depois_do_commit(self):
        script = """
import os, sys
from contextlib import contextmanager
from datetime import datetime
from telemetria import incidents
from telemetria.communication import Observer
from telemetria.storage import History
path, event, moment, mode = sys.argv[1:]
history = History(path)
at = datetime.fromisoformat(moment)
connect = history.connect
@contextmanager
def interrupted():
    with connect() as db:
        db.create_function("parar", 0, lambda: os._exit(42))
        if mode in ("nota","visto"):
            db.execute("CREATE TEMP TRIGGER corte AFTER INSERT ON comm_actions BEGIN SELECT parar(); END")
        if mode == "retomada":
            db.execute("CREATE TEMP TRIGGER corte AFTER UPDATE ON comm_incident_identity BEGIN SELECT parar(); END")
        yield db
history.connect = interrupted
if mode == "retomada":
    monitor = Observer(history)
    with connect() as db:
        monitor.window = db.execute("SELECT window_id FROM comm_events WHERE id=?", (event,)).fetchone()[0]
    monitor.advance(at, "sala-01", "retomada")
else:
    kind = "visto" if mode == "visto" else "nota"
    payload = {} if kind == "visto" else {"chave":"b"*32, "texto":"Resistiu ao reinício"}
    incidents.record(history,int(event),kind,payload,now=at)
os._exit(43)
"""
        def cut(mode):
            result = subprocess.run([sys.executable,"-B","-c",script,str(self.path),str(self.event),
                                     stamp(self.at(20)),mode], cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode,43 if mode=="confirmada" else 42, result.stderr)
            return incidents.capture(History(self.path),now=self.at(20))["detail"]

        original = self.report()["detail"]
        for mode in ("visto","nota","retomada"):
            with self.subTest(mode=mode):
                self.assertEqual(cut(mode), original)
        persisted = cut("confirmada")
        self.assertEqual(persisted["actions"], [{"at":stamp(self.at(20)),"body":"Resistiu ao reinício"}])
        retry = self.action("nota",{"chave":"b"*32,"texto":"Resistiu ao reinício"},40)
        self.assertEqual(retry,{"at":stamp(self.at(20)),"created":False})
        self.monitor.advance(self.at(20),"sala-01","retomada")
        final = self.report()["detail"]
        self.assertEqual((final["recovery_position"],final["outcome"]), (1,"retomada"))
        self.assertEqual(final["token"],original["token"])
        with self.history.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_migracao_preserva_acoes_antigas_e_a_ordem_das_proximas_retomadas(self):
        from concurrent.futures import ThreadPoolExecutor
        self.action("visto",{},20)
        self.monitor.advance(self.at(20),"sala-01","retorno")
        self.action("nota",{"chave":"1"*32,"texto":"Nota anterior à revisão"},20)
        self.monitor.advance(self.at(25),"sala-01","25")
        self.monitor.advance(self.at(31),"sala-01","31")
        open_id = int(self.report(room="sala-02")["detail"]["id"])
        self.assertNotEqual(open_id,self.event)
        with self.history.connect() as db:
            db.execute("DROP TRIGGER comm_recovery_order")
            db.execute("DROP TRIGGER comm_assign_identity")
            db.execute("DROP TABLE comm_incident_identity")
            db.execute("PRAGMA user_version=5")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(History,[self.path,self.path]))
        legacy = self.report(selected=self.event)["detail"]
        self.assertIsNone(legacy["recovery_position"])
        self.assertEqual([a["body"] for a in legacy["actions"]],[None,"Nota anterior à revisão"])
        self.assertFalse(self.action("visto",{},35)["created"])
        incidents.record(self.history,open_id,"visto",{},now=self.at(32))
        self.monitor.advance(self.at(32),"sala-02","voltou")
        newer = incidents.capture(History(self.path),selected=open_id,now=self.at(33))["detail"]
        self.assertEqual(newer["recovery_position"],1)
        self.assertNotEqual(newer["token"],legacy["token"])
        self.assertEqual(self.report(selected=self.event)["detail"]["token"],legacy["token"])

    def test_leitor_e_escritores_durante_o_commit_da_retomada(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        self.action("nota",{"chave":"a"*32,"texto":"Antes"},20)
        monitor_history = History(self.path)
        self.monitor.history = monitor_history
        connect = monitor_history.connect
        reserved, release = Event(), Event()

        @contextmanager
        def paused_recovery():
            with connect() as db:
                def hold():
                    reserved.set()
                    if not release.wait(8):
                        raise TimeoutError("O ensaio não liberou a retomada.")
                    return 1
                db.create_function("aguardar",0,hold)
                db.execute("""CREATE TEMP TRIGGER ensaio_retomada AFTER UPDATE OF actions_before_recovery
                              ON comm_incident_identity BEGIN SELECT aguardar(); END""")
                yield db

        def write(waiting, kind, data):
            other = History(self.path)
            connect_other = other.connect
            @contextmanager
            def observed():
                with connect_other() as db:
                    db.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
                    yield db
            with patch.object(other,"connect",observed):
                return incidents.record(other,self.event,kind,data,now=self.at(20))

        with patch.object(monitor_history,"connect",paused_recovery), ThreadPoolExecutor(max_workers=3) as pool:
            finish = pool.submit(self.monitor.advance,self.at(20),"sala-01","retorno")
            try:
                self.assertTrue(reserved.wait(5))
                visto_wait, note_wait = Event(), Event()
                visto = pool.submit(write,visto_wait,"visto",{})
                note = pool.submit(write,note_wait,"nota",{"chave":"b"*32,"texto":"Depois"})
                self.assertTrue(visto_wait.wait(5) and note_wait.wait(5))
                before = self.report()["detail"]
                self.assertIsNone(before["outcome"])
                self.assertIsNone(before["seen_at"])
                self.assertEqual([a["body"] for a in before["actions"]],["Antes"])
            finally:
                release.set()
            finish.result(timeout=5)
            self.assertTrue(visto.result(timeout=5)["created"])
            self.assertTrue(note.result(timeout=5)["created"])
        after = incidents.capture(History(self.path),selected=self.event,now=self.at(21))["detail"]
        self.assertEqual((after["outcome"],after["recovery_position"]),("retomada",1))
        self.assertEqual(after["actions"][0]["body"], "Antes")
        self.assertCountEqual([a["body"] for a in after["actions"][1:]],[None,"Depois"])
        self.assertEqual({a["at"] for a in after["actions"]},{after["ended"]})
