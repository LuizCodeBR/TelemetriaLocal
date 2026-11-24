"""Persistência das ações humanas e concorrência com o monitor de comunicação."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from telemetria import incidents
from telemetria.communication import Observer, stamp
from telemetria.dashboard import dashboard
from telemetria.storage import History


class IncidentFixture(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "central.sqlite3"
        self.history = History(self.path)
        self.monitor = Observer(self.history)
        self.origin = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=16)
        self.monitor.start(self.at(0))
        for name in ("sala-01", "sala-02"):
            self.monitor.advance(self.at(0), name, "primeiro")
        for tick in (8, 15, 16):
            self.monitor.advance(self.at(tick), "sala-02", str(tick))
        self.event = int(self.report()["detail"]["id"])

    def at(self, seconds):
        return self.origin + timedelta(seconds=seconds)

    def report(self, **args):
        return incidents.capture(self.history, now=self.at(16), **args)

    def action(self, kind, body, second):
        return incidents.record(self.history, self.event, kind, body, now=self.at(second))


class IncidentTests(IncidentFixture):
    def test_visto_e_notas_antes_e_depois_da_retomada_com_reabertura(self):
        self.assertTrue(self.action("visto", {}, 20)["created"])
        self.assertFalse(self.action("visto", {}, 21)["created"])
        note = {"chave":"1"*32, "texto":"  Verificando a alimentação.  "}
        self.action("nota", note, 21)
        self.assertFalse(self.action("nota", note, 24)["created"])
        self.assertIsNone(self.report()["detail"]["outcome"])
        self.monitor.advance(self.at(24), "sala-02", "24")
        self.monitor.advance(self.at(32), "sala-01", "retorno")
        self.action("nota", {"chave":"2"*32, "texto":"Sinal confirmado."}, 35)
        saved = incidents.capture(History(self.path), now=self.at(40))["detail"]
        self.assertEqual(saved["outcome"], "retomada")
        self.assertEqual((saved["detected"],saved["seen_at"],saved["ended"]),
                         tuple(stamp(self.at(s)) for s in (15,20,32)))
        self.assertEqual([a["body"] for a in saved["actions"]], [None,"Verificando a alimentação.","Sinal confirmado."])

    def test_reenvio_com_texto_divergente_nao_substitui_nota(self):
        note = {"chave":"3"*32, "texto":"Conferido"}
        self.action("nota", note, 20)
        with self.assertRaises(incidents.NoteConflict):
            self.action("nota", {**note, "texto":"Outra nota"}, 21)
        self.action("nota", {**note, "chave":"4"*32}, 18)
        actions = self.report()["detail"]["actions"]
        self.assertEqual(actions, [{"at":stamp(self.at(20)),"body":"Conferido"}]*2)
        self.assertIsNone(self.report()["detail"]["seen_at"])

    def test_filtros_incidente_fora_da_lista_e_isolamento_entre_salas(self):
        self.assertEqual(self.report(room="sala-02")["items"], [])
        self.assertEqual(len(self.report(open_only=True, unseen_only=True)["items"]), 1)
        self.action("visto", {}, 17)
        seen = self.report(unseen_only=True, selected=self.event)
        self.assertEqual(seen["items"], [])
        self.assertEqual(seen["detail"]["id"], str(self.event))
        self.assertIsNone(self.report(room="sala-02", selected=self.event)["detail"])
        self.monitor.advance(self.at(24), "sala-01", "volta")
        self.monitor.advance(self.at(31))
        self.assertEqual([e["sensor"] for e in self.report(open_only=True)["items"]], ["sala-02"])
        self.assertIsNone(self.report(room="sala-02")["detail"]["seen_at"])

    def test_duas_quedas_na_mesma_sala_mantem_acoes_independentes(self):
        note = {"chave":"7"*32,"texto":"Verificando a sala."}
        self.action("visto", {}, 17)
        self.action("nota", note, 18)
        self.monitor.advance(self.at(24), "sala-01", "retorno")
        for second in (30, 36, 39):
            self.monitor.advance(self.at(second), "sala-02", str(second))
        latest = self.report(room="sala-01", open_only=True)["detail"]
        self.assertNotEqual(latest["id"], str(self.event))
        self.assertIsNone(latest["seen_at"])
        self.assertEqual(latest["actions"], [])
        # Um mesmo envio, repetido em paralelo, recebe apenas uma confirmação de criação.
        gate = Barrier(2)
        def retry(_):
            gate.wait(timeout=5)
            return incidents.record(self.history, int(latest["id"]), "nota", note, now=self.at(40))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(retry, (0, 1)))
        self.assertEqual(sorted(r["created"] for r in results), [False, True])
        self.assertEqual([r["at"] for r in results], [stamp(self.at(40))]*2)
        first = self.report(selected=self.event)["detail"]
        self.assertEqual([a["body"] for a in first["actions"]], [None, note["texto"]])
        self.assertEqual(len(self.report(selected=int(latest["id"]))["detail"]["actions"]), 1)

    def test_encerramento_e_silencio_da_central_sao_observacao_interrompida(self):
        self.action("nota", {"chave":"9"*32, "texto":"Verificar central"}, 16)
        before = incidents.capture(self.history, now=self.at(31))["detail"]
        self.assertEqual((before["outcome"],before["ended"]), ("sem_observacao",stamp(self.at(16))))
        self.monitor.stop()
        self.monitor.start(self.at(40))
        final = incidents.capture(self.history, now=self.at(40))["detail"]
        self.assertEqual(final, before)
        self.assertEqual(self.report(open_only=True)["items"], [])

    def test_abrir_base_da_versao_anterior_em_paralelo_preserva_ocorrencias(self):
        before = self.report()["items"]
        # Reproduz a estrutura v4 da versão 0.6.1.
        with self.history.connect() as db:
            db.execute("DROP TABLE comm_actions")
            db.execute("PRAGMA user_version=4")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(History, [self.path,self.path]))
        self.assertEqual(self.report()["items"], before)
        self.assertTrue(self.action("visto", {}, 16)["created"])
        with self.history.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_lista_e_detalhe_compartilham_leitura_enquanto_escritor_confirma(self):
        other = History(self.path)
        entered, committed = Event(), Event()
        connect = self.history.connect
        waited = []

        def writer():
            if not entered.wait(5):
                raise TimeoutError("A consulta não chegou ao detalhe.")
            try:
                incidents.record(other, self.event, "visto", {}, now=self.at(19))
                incidents.record(other, self.event, "nota", {"chave":"a"*32,"texto":"Recebido"}, now=self.at(20))
            finally:
                committed.set()

        @contextmanager
        def reader():
            with connect() as db:
                def interleave(sql):
                    if "SELECT at,body FROM comm_actions" in sql:
                        entered.set()
                        waited.append(committed.wait(5))
                db.set_trace_callback(interleave)
                yield db
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            with patch.object(self.history, "connect", reader):
                before = self.report()
            future.result(timeout=6)
        self.assertEqual(waited,[True])
        self.assertIsNone(before["items"][0]["seen_at"])
        self.assertEqual(before["detail"]["actions"],[])
        after = self.report()
        self.assertEqual(after["items"][0]["seen_at"],stamp(self.at(19)))
        self.assertEqual([a["body"] for a in after["detail"]["actions"]],[None,"Recebido"])

    @staticmethod
    def post(base, path, data, headers=None):
        req = Request(base + path, data=json.dumps(data).encode(), method="POST",
                      headers={"Content-Type":"application/json", **(headers or {})})
        with urlopen(req, timeout=7) as response:
            return response.status, json.load(response)

    def test_duas_abas_http_e_retomada_simultanea(self):
        start, after_seen = Barrier(3), Barrier(3)
        with dashboard(self.history, 0) as port:
            base = f"http://127.0.0.1:{port}"
            path = f"/api/incidentes/{self.event}/"
            token = self.report()["detail"]["token"]
            def tab(n):
                start.wait(timeout=6)
                status, _ = self.post(base, path+"visto", {"ocorrencia":token})
                after_seen.wait(timeout=6)
                note = {"chave":str(n)*32,"texto":f"Nota da aba {n}","ocorrencia":token}
                created, _ = self.post(base, path+"nota", note)
                repeated, _ = self.post(base, path+"nota", note)
                return status, created, repeated

            def resume():
                start.wait(timeout=6)
                self.monitor.advance(self.at(24), "sala-01", "retorno")
                after_seen.wait(timeout=6)
            with ThreadPoolExecutor(max_workers=3) as pool:
                a, b, c = pool.submit(tab,1), pool.submit(tab,2), pool.submit(resume)
                statuses = [a.result(timeout=12),b.result(timeout=12)]
                c.result(timeout=6)
            self.assertEqual(sorted(row[0] for row in statuses),[200,201])
            self.assertEqual([r[1:] for r in statuses],[(201,200)]*2)
            with urlopen(base+"/api/incidentes?sala=sala-01",timeout=3) as res:
                detail = json.load(res)["detail"]
            self.assertEqual(detail["outcome"],"retomada")
            self.assertEqual(detail["ended"],stamp(self.at(24)))
            self.assertCountEqual([a["body"] for a in detail["actions"]],[None,"Nota da aba 1","Nota da aba 2"])

    def test_http_valida_notas_e_vinculo_com_ocorrencia(self):
        with dashboard(self.history, 0) as port:
            base = f"http://127.0.0.1:{port}"
            path = f"/api/incidentes/{self.event}/"
            with urlopen(base+"/",timeout=3) as res:
                self.assertIn('href="/incidentes"',res.read().decode())
            with urlopen(base+"/incidentes",timeout=3) as res:
                self.assertIn("Marcar como visto",res.read().decode())
            for field, value in [("texto","  "),("texto","a"*1001),("texto","\x00"),("chave","?")]:
                with self.subTest(field=field,length=len(value)), self.assertRaises(HTTPError) as err:
                    self.post(base,path+"nota",{"chave":"1"*32,"texto":"Mensagem",field:value})
                self.assertEqual(err.exception.code,400)
            for endpoint, data, headers, expected in [
                ("/api/incidentes/99999/visto", {}, {},404), (path+"visto",{},{"Content-Type":"text/plain"},415),
                (path+"visto",{},{"Origin":"null"},403)]:
                with self.assertRaises(HTTPError) as err:
                    self.post(base,endpoint,data,headers)
                self.assertEqual(err.exception.code,expected)
            self.assertEqual(self.report()["detail"]["actions"],[])
