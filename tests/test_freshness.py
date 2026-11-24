"""Atualidade por sala, evidência no recebimento e histórico transacional."""
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
from urllib.request import urlopen

from telemetria import freshness_report, maintenance
from telemetria.communication import Observer, stamp
from telemetria.dashboard import dashboard
from telemetria.storage import History, Reading, ReadingConflict


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1]/".cache"/"testes"
        cache.mkdir(parents=True,exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)/"central.sqlite3"
        self.base = datetime(2026,1,15,14,20,tzinfo=UTC)
        self.moment = self.base
        self.h = History(self.path,received_clock=lambda:self.moment)
        self.monitor = Observer(self.h)
        self.monitor.start(self.base)

    def at(self,sec):
        return self.base + timedelta(seconds=sec)

    def contact(self,sec,room="a"):
        self.moment = self.at(sec)
        self.monitor.advance(self.moment,room,str(sec) if room else None)

    def reading(self,sec,n,room="a"):
        return Reading(room,f"{n:032x}",stamp(self.at(sec)),24,52)

    def read(self,sec,room=None):
        return freshness_report.capture(self.h,room,now=self.at(sec))

    def stale(self):
        self.contact(0)
        self.h.record(self.reading(0,1))
        for s in (5,10,15):
            self.contact(s)

    def test_fronteira_estrita_e_sala_presente_sem_primeira_leitura(self):
        self.contact(0)
        self.contact(0,"b")
        for t in (5,10):
            self.contact(t)
            self.contact(t,"b")
            self.h.record(self.reading(t,100+t,"b"))
        self.assertFalse(self.read(10)["events"])
        self.contact(10.000001)
        data=self.read(11,"a")
        self.assertEqual(data["current"][0]["state"],"online")
        self.assertEqual(data["current"][0]["freshness"],"desatualizada")
        self.assertEqual(data["events"][0]["detected"],stamp(self.at(10.000001)))
        self.assertIsNone(data["events"][0]["baseline"])
        self.assertFalse(self.read(11,"b")["events"])
        self.assertEqual(self.read(11,"b")["current"][0]["freshness"],"atualizada")

    def test_atrasada_duplicada_futura_e_igual_temperatura(self):
        self.stale()
        self.moment=self.at(20)
        self.assertEqual(self.h.record(self.reading(1,2)),"gravada")
        self.assertEqual(self.h.record(self.reading(1,2)),"repetida")
        self.assertEqual(self.h.record(self.reading(200,3)),"gravada")
        with self.assertRaises(ReadingConflict):
            self.h.record(self.reading(19,2))
        self.assertEqual(self.read(20)["current"][0]["measured"],stamp(self.at(1)))
        self.assertIsNone(self.read(20)["events"][0]["outcome"])
        self.contact(24)
        # Exatos dez segundos ainda são atuais; o valor continua em 24 °C.
        valid=self.reading(14,4)
        self.assertEqual(self.h.record(valid),"gravada")
        self.assertEqual(self.h.record(valid),"repetida")
        event,=self.read(24)["events"]
        self.assertEqual((event["detected"],event["ended"],event["recovery_id"]),
                         (stamp(self.at(15)),stamp(self.at(24)),valid.id))
        self.assertEqual(event["recovery_measured"],stamp(self.at(14)))
        self.assertEqual(event["recovery_received"],stamp(self.at(24)))
        self.assertEqual(self.h.snapshot()["total"],4)

    def test_manter_limite_da_ocorrencia_e_regra_individual(self):
        self.stale()
        self.h.configure("a",gap_seconds=30)
        self.contact(20)
        self.h.record(self.reading(3,2))
        event,=self.read(20)["events"]
        self.assertIsNone(event["ended"])
        self.assertEqual(self.read(20)["current"][0]["threshold"],10)
        self.h.record(self.reading(15,3))
        self.assertEqual(self.read(20)["current"][0]["threshold"],30)
        self.assertEqual(self.read(20)["events"][0]["threshold"],10)
        for sec in (25,35,45):
            self.contact(sec)
        self.assertEqual(len(self.read(45)["events"]),1)
        self.contact(46)
        self.assertEqual([e["threshold"] for e in self.read(46)["events"]],[30,10])

    def test_parada_reinicio_e_pacote_durante_lacuna(self):
        self.stale()
        self.moment=self.at(18)
        self.h.record(self.reading(-50,2))
        # A central deixa de comprovar presença; a leitura de 33 s fica apenas no coletor.
        self.moment=self.at(33)
        self.h.record(self.reading(33,3))
        projected,=self.read(33)["events"]
        self.assertEqual((projected["outcome"],projected["ended"]),("sem_observacao",stamp(self.at(18))))
        self.assertIsNone(projected["recovery_id"])
        self.h=History(self.path,received_clock=lambda:self.moment)
        self.monitor=Observer(self.h)
        self.monitor.start(self.at(35))
        self.contact(35)
        self.assertEqual(self.read(35)["events"][0],projected)
        self.assertEqual(self.read(35)["current"][0]["measured"],stamp(self.at(33)))
        self.contact(43);self.contact(46)
        self.monitor.stop()
        final=self.read(55)
        self.assertEqual([e["outcome"] for e in final["events"]],["sem_observacao","sem_observacao"])
        self.assertEqual(final["current"][0]["freshness"],"sem_observacao")

    def test_rollback_do_relogio_nao_antecipa_recuperacao(self):
        self.stale()
        self.moment=self.at(22)
        self.h.record(self.reading(1,2))
        self.moment=self.at(16)
        early=self.reading(12,3)
        self.h.record(early)
        saved,=self.read(22)["events"]
        self.assertEqual(saved["recovery_id"],early.id)
        self.assertEqual(saved["ended"],stamp(self.at(22)))
        self.assertEqual(saved["recovery_received"],stamp(self.at(16)))

    def test_transacao_falha_na_recuperacao_preserva_leitura_e_ocorrencia_consistentes(self):
        self.stale()
        self.moment=self.at(18)
        r=self.reading(17,2)
        with self.h.connect() as db:
            db.execute("""CREATE TRIGGER falhar BEFORE UPDATE OF outcome ON freshness_events
                          BEGIN SELECT RAISE(ABORT,'ensaio de rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.record(r)
        with self.h.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM readings WHERE id=?",(r.id,)).fetchone())
            db.execute("DROP TRIGGER falhar")
        event,=self.read(18)["events"]
        self.assertIsNone(event["ended"])
        self.assertEqual(self.h.record(r),"gravada")
        self.assertEqual(self.read(18)["events"][0]["recovery_id"],r.id)

    def test_coletores_disputam_recuperacao_com_reenvio(self):
        self.stale()
        self.moment=self.at(20)
        gate=Barrier(4)
        def deliver(reading):
            h=History(self.path,received_clock=lambda:self.moment)
            gate.wait(6)
            return h.record(reading)
        first,second=self.reading(19,2),self.reading(20,3)
        with ThreadPoolExecutor(4) as pool:
            outcomes=list(pool.map(deliver,[first,second,first,second]))
        self.assertCountEqual(outcomes,["gravada","gravada","repetida","repetida"])
        data=self.read(20)
        event,=data["events"]
        self.assertIn(event["recovery_id"],[first.id,second.id])
        self.assertEqual(data["current"][0]["measured"],second.measured_at)
        self.assertEqual(event["recovery_received"],stamp(self.at(20)))

    def test_consulta_durante_recuperacao_e_janela_de_manutencao(self):
        self.contact(0)
        token=maintenance.agenda(self.h,now=self.base)["origin"]
        for n,(start,end) in enumerate([(5,17),(17,22),(4,9)]):
            w={"origin":token,"key":f"{n:032x}","sensor":"b" if n==2 else "a",
               "start":stamp(self.at(start)),"end":stamp(self.at(end)),"reason":"Ajuste"}
            if n==2:self.contact(0,"b")
            maintenance.update(self.h,w,now=self.base)
            if n==1:
                maintenance.update(self.h,{"origin":token,"key":w["key"]},cancel=True,now=self.at(1))
        self.stale()
        ready,done=Event(),Event()
        other=History(self.path,received_clock=lambda:self.at(20))
        def write():
            if not ready.wait(5):raise TimeoutError("O leitor não abriu a consulta")
            try:other.record(self.reading(19,2))
            finally:done.set()
        connect=self.h.connect
        @contextmanager
        def reader():
            with connect() as db:
                def sync(sql):
                    if "FROM freshness_events e JOIN" in sql and not ready.is_set():
                        ready.set()
                        done.wait(5)
                db.set_trace_callback(sync)
                yield db
        with ThreadPoolExecutor(1) as pool:
            writer=pool.submit(write)
            with patch.object(self.h,"connect",reader):
                before=self.read(20,"a")
            writer.result(timeout=6)
        after=self.read(20,"a")
        self.assertEqual([d["current"][0]["freshness"] for d in [before,after]],["desatualizada","atualizada"])
        self.assertEqual([d["events"][0]["outcome"] for d in [before,after]],[None,"atualizada"])
        self.assertEqual([(w["start"],w["end"]) for w in after["events"][0]["maintenance"]],
                         [(stamp(self.at(5)),stamp(self.at(17)))])
        self.assertEqual(after["events"][0]["until"],stamp(self.at(20)))

    def test_abertura_de_base_081_preserva_medicoes_sem_inventar_ocorrencias(self):
        self.stale()
        self.monitor.stop()
        with self.h.connect() as db:
            db.execute("DROP TABLE freshness_watch")
            db.execute("DROP TABLE freshness_events")
            db.execute("DROP INDEX freshness_valid_reading")
            db.execute("PRAGMA user_version=7")
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(History,[self.path,self.path]))
        self.assertEqual(self.read(30)["events"],[])
        self.assertEqual(self.h.snapshot()["total"],1)
        self.monitor.start(self.at(30))
        self.contact(30)
        self.assertEqual(self.read(30)["current"][0]["measured"],stamp(self.base))
        self.contact(35);self.contact(41)
        self.assertEqual(self.read(41)["events"][0]["detected"],stamp(self.at(41)))

    def test_grafico_por_periodo_reune_atualidade_e_manutencao_sem_alterar_disponibilidade(self):
        from telemetria import timeline
        self.contact(0)
        token=maintenance.agenda(self.h,now=self.base)["origin"]
        maintenance.update(self.h,{"origin":token,"key":"f"*32,"sensor":"a","reason":"Bateria",
                           "start":stamp(self.at(5)),"end":stamp(self.at(18))},now=self.base)
        self.stale()
        self.moment=self.at(20)
        self.h.record(self.reading(19,2))
        self.contact(22)
        def cut(start,end):
            return timeline.period(self.h,"a",stamp(self.at(start)),stamp(self.at(end)),now=self.at(22))
        self.assertEqual(cut(5,15)["freshness"],[])
        data=cut(15,21)
        self.assertEqual(data["freshness"],[{"start":stamp(self.at(15)),"until":stamp(self.at(20)),"outcome":"atualizada"}])
        self.assertEqual(data["availability"]["rooms"][0]["maintenance"][0]["reason"],"Bateria")
        self.assertEqual(data["availability"]["rooms"][0]["outages"],[])
        self.assertEqual(cut(20,22)["freshness"],data["freshness"])
        self.assertEqual(cut(20.000001,22)["freshness"],[])

    def test_api_painel_e_filtro(self):
        self.stale()
        self.contact(15,"b")
        with dashboard(self.h,0) as port:
            base=f"http://127.0.0.1:{port}"
            with urlopen(base+"/") as reply:
                home=reply.read().decode()
            self.assertIn('href="/atualidade"',home)
            self.assertIn("Última medição válida",home)
            with urlopen(base+"/atualidade") as reply:
                self.assertIn("Histórico de desatualização",reply.read().decode())
            with urlopen(base+"/api/atualidade?sala=a") as reply:
                data=json.load(reply)
            self.assertEqual(data["rooms"],["a","b"])
            self.assertEqual({r["sensor"] for r in data["events"]+data["current"]},{"a"})
            for q in ("sala=a&sala=b","sala=","outra=a"):
                with self.assertRaises(HTTPError) as failure:
                    urlopen(base+"/api/atualidade?"+q)
                self.assertEqual(failure.exception.code,400)
