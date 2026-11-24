"""Concorrência, persistência e limites da janela observada na agenda por sala."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from telemetria import incidents, maintenance, timeline
from telemetria.communication import Observer, stamp
from telemetria.dashboard import dashboard
from telemetria.storage import History


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "central.sqlite3"
        self.h = History(self.path)
        self.m = Observer(self.h)
        self.base = datetime(2026,1,15,14,tzinfo=UTC)
        self.m.start(self.base)
        for name in ("a", "b"):
            self.m.advance(self.base, name, "inicial")
        self.origin = maintenance.agenda(self.h, now=self.base)["origin"]

    def at(self, seconds):
        return self.base + timedelta(seconds=seconds)

    def window(self, key=1, start=10, end=20, sensor="a"):
        return {"origin":self.origin, "key":f"{key:032x}", "sensor":sensor, "reason":"Troca de bateria",
                "start":stamp(self.at(start)), "end":stamp(self.at(end))}

    def cancel(self, window, at=1):
        return maintenance.update(self.h, {k:window[k] for k in ("key","origin")}, cancel=True, now=self.at(at))

    def test_limites_validacao_e_isolamento(self):
        first = self.window()
        maintenance.update(self.h, first, now=self.base)
        for bad in (self.window(2,9,10.000001),self.window(3,19.999999,25),self.window(4,9,25)):
            with self.assertRaises(maintenance.Conflict):
                maintenance.update(self.h,bad,now=self.base)
        for good in (self.window(5,5,10), self.window(6,20,25), self.window(7,sensor="b")):
            maintenance.update(self.h,good,now=self.base)
        for bad in (self.window(8,start=0),self.window(9,sensor="fantasma"),self.window(10,start=20,end=20),
                    {**first,"reason":" "},{**first,"start":"ontem"},{**first,"end":"2026-01-15T14:33:00-03:00"}):
            with self.assertRaises(ValueError):
                maintenance.update(self.h,bad,now=self.base)
        with self.h.connect() as db:
            self.assertEqual(set(maintenance.active_by_room(db,stamp(self.at(10)))), {"a","b"})
            self.assertEqual(maintenance.active_by_room(db,stamp(self.at(25))), {})
        with self.assertRaises(maintenance.Conflict):
            self.cancel(first,at=10)

    def test_reenvios_e_reabertura_preservam_cancelamento_e_identidade(self):
        w = self.window()
        a = maintenance.update(self.h,w,now=self.base)
        first = self.cancel(w)
        self.h = History(self.path)
        self.assertEqual(maintenance.update(self.h,w,now=self.at(30)),
                         {"created":False,"at":a["at"],"canceled_at":first["at"]})
        self.assertEqual(self.cancel(w,at=30),{"created":False,"at":first["at"]})
        with self.assertRaises(maintenance.Conflict):
            maintenance.update(self.h,{**w,"reason":"Outro motivo"},now=self.base)
        maintenance.update(self.h,self.window(2),now=self.at(2))
        agenda = maintenance.agenda(self.h,now=self.at(2))
        self.assertEqual([r["canceled_at"] for r in agenda["windows"]],[stamp(self.at(1)),None])
        other = History(self.path.with_name("outra.sqlite3"))
        with self.assertRaises(maintenance.Conflict):
            maintenance.update(other,w,now=self.base)
        self.assertEqual(maintenance.agenda(other,now=self.base)["windows"],[])

    def test_troca_de_relogio_nao_reclassifica_observacao_confirmada(self):
        w = self.window()
        maintenance.update(self.h,w,now=self.base)
        self.m.advance(self.at(8))
        self.m.advance(self.at(15))
        with self.assertRaises(maintenance.Conflict):
            self.cancel(w,at=2)
        with self.assertRaises(maintenance.Conflict):
            maintenance.update(self.h,self.window(2,9,14,"b"),now=self.at(2))

    def test_janela_e_interrupcao_aparecem_no_mesmo_recorte_sem_incluir_lacuna(self):
        for w in (self.window(start=12,end=18),self.window(2,18,20),self.window(3,22,30),self.window(4,32,40)):
            maintenance.update(self.h,w,now=self.base)
        self.cancel(self.window(2),at=1)
        for t in (8,15,18,20):
            self.m.advance(self.at(t),"b",str(t))
        self.m.advance(self.at(22),"a","volta")
        self.m.stop()
        self.m.start(self.at(32))
        self.m.advance(self.at(33),"b","apos-reinicio")
        report = timeline.period(self.h,"a",stamp(self.base),stamp(self.at(35)),now=self.at(33))
        a,b = report["availability"]["rooms"]
        self.assertEqual(a["outages"],[[stamp(self.at(15)),stamp(self.at(22))]])
        self.assertEqual(b["outages"],[])
        self.assertEqual(report["availability"]["blind"],[[stamp(self.at(22)),stamp(self.at(32))]])
        detail = incidents.capture(self.h,room="a",now=self.at(33))["detail"]
        self.assertEqual([w["start"] for w in detail["maintenance"]],[stamp(self.at(12))])
        self.assertEqual([w["start"] for w in a["maintenance"]], [stamp(self.at(t)) for t in (12,22,32)])
        self.assertNotIn("maintenance",b)

    def test_http_duas_abas_disputam_horario_cancelam_e_retentam(self):
        # A API usa o relógio real e uma conexão independente por requisição.
        start = datetime.now(UTC) + timedelta(hours=1)
        w = {**self.window(), "start":stamp(start),"end":stamp(start+timedelta(minutes=20))}
        with dashboard(self.h,0) as port:
            url = f"http://127.0.0.1:{port}"
            def send(path, body):
                req = Request(url+path,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
                try:
                    with urlopen(req,timeout=8) as res:
                        return res.status,json.load(res)
                except HTTPError as err:
                    with err:
                        return err.code,None
            def race(path, payloads):
                gate = Barrier(len(payloads))
                def run(body):
                    gate.wait(timeout=5)
                    return send(path,body)
                with ThreadPoolExecutor(len(payloads)) as pool:
                    return list(pool.map(run,payloads))
            route = "/api/manutencao"
            requests = [w,{**w,"key":"2"*32},{**w,"key":"3"*32,"sensor":"b"}]
            result = race(route, requests)
            self.assertCountEqual([x[0] for x in result[:2]],[201,409])
            self.assertEqual(result[2][0],201)
            winner = requests[0 if result[0][0] == 201 else 1]
            self.assertEqual(send(route,winner)[0],200)
            cancel = {k:winner[k] for k in ("key","origin")}
            replies = race(route+"/cancelar",[cancel,cancel])
            self.assertCountEqual([r[0] for r in replies],[200,201])
            self.assertEqual(replies[0][1]["at"],replies[1][1]["at"])
            with urlopen(url+route) as res:
                rows = json.load(res)["windows"]
            self.assertEqual(len(rows),2)
            self.assertEqual([r["sensor"] for r in rows if not r["canceled_at"]],["b"])

    def test_cancelamento_e_novo_agendamento_simultaneos(self):
        first, second = self.window(), self.window(2)
        maintenance.update(self.h,first,now=self.base)
        gate = Barrier(2)
        def cancel():
            gate.wait(5)
            return self.cancel(first)["created"]
        def create():
            gate.wait(5)
            try:
                return maintenance.update(self.h,second,now=self.at(1))["created"]
            except maintenance.Conflict:
                return False
        with ThreadPoolExecutor(2) as pool:
            left,right = pool.submit(cancel),pool.submit(create)
            self.assertTrue(left.result(timeout=8))
            created = right.result(timeout=8)
        result = maintenance.agenda(History(self.path),now=self.at(2))
        self.assertEqual([r["key"] for r in result["windows"] if not r["canceled_at"]],
                         [second["key"]] if created else [])
        self.assertEqual(maintenance.update(self.h,second,now=self.at(2))["created"],not created)

    def test_relatorio_durante_cancelamento_le_estado_inteiro(self):
        w = self.window()
        maintenance.update(self.h,w,now=self.base)
        connect = self.h.connect
        changed = []
        other = History(self.path)
        @contextmanager
        def reading():
            with connect() as db:
                def interleave(sql):
                    if "FROM comm_events e JOIN comm_windows" in sql and not changed:
                        maintenance.update(other,{"key":w["key"],"origin":self.origin},cancel=True,now=self.at(1))
                        changed.append(True)
                db.set_trace_callback(interleave)
                yield db
        def rooms():
            return timeline.period(self.h,"a",stamp(self.base),stamp(self.at(30)),now=self.at(2))["availability"]["rooms"]
        with patch.object(self.h,"connect",reading):
            before = rooms()
        self.assertEqual(changed,[True])
        self.assertEqual(before[0]["maintenance"][0]["reason"],w["reason"])
        self.assertNotIn("maintenance",rooms()[0])

    def test_reenvio_simultaneo_da_mesma_chave_grava_uma_janela(self):
        w = self.window()
        barrier = Barrier(4)
        def submit(_):
            barrier.wait(5)
            return maintenance.update(self.h,w,now=self.base)
        with ThreadPoolExecutor(4) as pool:
            receipts = list(pool.map(submit,range(4)))
        self.assertEqual(sum(r["created"] for r in receipts),1)
        self.assertEqual({r["at"] for r in receipts},{stamp(self.base)})
        self.assertEqual(len(maintenance.agenda(History(self.path),now=self.base)["windows"]),1)

    def test_cancelamento_na_fila_reavalia_a_hora_quando_consegue_gravar(self):
        from threading import Event
        w = self.window()
        maintenance.update(self.h,w,now=self.base)
        waiting = Event()
        reader = History(self.path)
        connect = reader.connect
        @contextmanager
        def probe():
            with connect() as db:
                db.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
                yield db
        with patch.object(reader,"connect",probe), patch("telemetria.maintenance.datetime") as clock:
            clock.now.return_value = self.at(9)
            with ThreadPoolExecutor(1) as pool:
                with self.h.connect() as lock:
                    lock.execute("BEGIN IMMEDIATE")
                    future = pool.submit(maintenance.update, reader, {"origin":self.origin,"key":w["key"]}, cancel=True)
                    self.assertTrue(waiting.wait(5))
                    clock.now.return_value = self.at(10)
                with self.assertRaises(maintenance.Conflict):
                    future.result(timeout=6)
        self.assertIsNone(maintenance.agenda(self.h,now=self.at(11))["windows"][0]["canceled_at"])

    def test_painel_principal_exibe_sala_em_manutencao_ate_o_fim_exclusivo(self):
        w = {**self.window(),"reason":"Bateria & cabos"}
        maintenance.update(self.h,w,now=self.base)
        with dashboard(self.h,0) as port, patch("telemetria.maintenance.datetime") as clock:
            for sec,active in ((9,False),(10,True),(19,True),(20,False)):
                clock.now.return_value=self.at(sec)
                with urlopen(f"http://127.0.0.1:{port}/") as res:
                    html=res.read().decode()
                self.assertIn('href="/manutencao"',html)
                self.assertEqual(html.count("Em manutenção até"),int(active))
                if active:
                    self.assertIn("Bateria &amp; cabos",html)
                self.assertNotIn("Em manutenção até",html.split('scope="row">b</th>',1)[1].split("</tr>",1)[0])

    def test_migracao_da_versao_seis_em_duas_conexoes(self):
        with self.h.connect() as db:
            db.execute("DROP TABLE maintenance_windows")
            db.execute("DROP TABLE maintenance_origin")
            db.execute("PRAGMA user_version=6")
        with ThreadPoolExecutor(2) as pool:
            origins = list(pool.map(lambda _:maintenance.agenda(History(self.path),now=self.base)["origin"],range(2)))
        self.assertEqual(len(set(origins)),1)
        self.assertEqual(maintenance.agenda(self.h,now=self.base)["rooms"],["a","b"])
        with self.h.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],9)
