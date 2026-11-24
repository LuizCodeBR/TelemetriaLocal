"""Verifica no navegador o recibo após recarga e o recorte exportado após editar a agenda."""
import csv
import io
import json
from pathlib import Path
import tempfile

import test_timeline_browser as browser
from telemetria import maintenance
from telemetria.demo_maintenance import demonstrate_maintenance


class MaintenanceReviewBrowserTests(browser.ChromiumReportTests):
    def test_reenvio_apos_recarga_respeita_cancelamento_por_outra_aba(self):
        stub = """<script>
window.fetch=async (url,opt={})=>{
 const token=sessionStorage.getItem("base")||"a".repeat(32), now="2026-01-15T13:55:00.000000+00:00";
 const rows=JSON.parse(sessionStorage.getItem("rows-"+token)||"[]");
 if(!opt.method)return {ok:true,json:async()=>({origin:token,rooms:["sala"],windows:rows,now})};
 const p=JSON.parse(opt.body);
 if(p.origin!==token)return {ok:false,status:409,json:async()=>({error:"Outra pasta",code:"origem_alterada"})};
 let row=rows.find(w=>w.key===p.key);
 if(!row){
   row={...p,created_at:now,canceled_at:null}; rows.push(row);
   sessionStorage.setItem("rows-"+token,JSON.stringify(rows));
   if(!sessionStorage.getItem("perdeu")){sessionStorage.setItem("perdeu","1");throw Error("Resposta perdida");}
 }
 return {ok:true,status:200,json:async()=>({created:false,at:row.created_at,canceled_at:row.canceled_at})};
};</script>"""
        app = maintenance.page().replace('<script type="application/json"',stub+'<script type="application/json"',1)
        source = json.dumps(app).replace("<",r"\u003c")
        scenario = """
(async()=>{
 const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
 const tick=()=>new Promise(ok=>setTimeout(ok,15));
 const named=(doc,id)=>doc.getElementById(id);
 function enter(doc){
   named(doc,"start").value="2026-01-15T14:00";named(doc,"end").value="2026-01-15T14:20";
   named(doc,"reason").value="Troca de bateria";
 }
 async function reloadApp(){
   const f=document.createElement("iframe"),loaded=new Promise(ok=>f.onload=ok);
   f.srcdoc=APP;document.body.append(f);await loaded;await tick();return f;
 }
 try{
   await tick();enter(document);el("schedule").requestSubmit();await tick();
   const db="rows-"+"a".repeat(32), rows=JSON.parse(sessionStorage.getItem(db));
   rows[0].canceled_at="2026-01-15T13:57:00.000000+00:00";sessionStorage.setItem(db,JSON.stringify(rows));
   sessionStorage.setItem("base","b".repeat(32));
   el("schedule").requestSubmit();await tick();
   const other=await reloadApp();const unrelated=named(other.contentDocument,"reason").value;other.remove();
   sessionStorage.setItem("base","a".repeat(32));
   const again=await reloadApp(),d=again.contentDocument,restored=named(d,"reason").value;
   enter(d);named(d,"schedule").requestSubmit();await tick();
   const receipt=named(d,"feedback").textContent,first=JSON.parse(sessionStorage.getItem(db));
   named(d,"schedule").requestSubmit();await tick();
   out.textContent=JSON.stringify({restored,unrelated,receipt,first:first.length,active:first.filter(w=>!w.canceled_at).length,
     afterIntentional:JSON.parse(sessionStorage.getItem(db)).length});
 }catch(e){out.textContent=JSON.stringify({error:String(e)});}
})();""".replace("APP",source)
        result=self.run_browser(app,scenario)
        self.assertEqual((result["first"],result["active"]),(1,0))
        self.assertEqual(result["restored"],"Troca de bateria")
        self.assertEqual(result["unrelated"],"")
        self.assertIn("cancelad",result["receipt"].lower())
        self.assertEqual(result["afterIntentional"],2)

    def test_virada_utc_e_janelas_contiguas_com_precisao_de_microssegundos(self):
        from datetime import UTC, datetime, timedelta
        from telemetria import incidents, timeline
        from telemetria.communication import Observer, stamp
        from telemetria.storage import History
        zero=datetime(2026,12,31,23,59,40,tzinfo=UTC)
        def at(us):
            return zero+timedelta(microseconds=us)
        cache=Path(__file__).resolve().parents[1]/".cache"/"testes"
        cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            history=History(Path(folder)/"central.sqlite3")
            monitor=Observer(history)
            monitor.start(zero)
            for name in ("a","b"):
                monitor.advance(zero,name,"presente")
            token=maintenance.agenda(history,now=zero)["origin"]
            for n,(left,right) in enumerate([(17_999_999,20_000_001),(20_000_001,21_000_003),
                                             (21_000_003,23_999_999)],1):
                key=f"{n:032x}"
                w={"origin":token,"sensor":"a","reason":f"Janela {n}","key":key,
                   "start":stamp(at(left)),"end":stamp(at(right))}
                maintenance.update(history,w,now=zero)
            maintenance.update(history,{"origin":token,"key":key},cancel=True,now=at(1))
            for n in (8,15,22):
                monitor.advance(at(n*1_000_000),"b",str(n))
            monitor.advance(at(24_000_000),"a","retomou")
            period=timeline.capture_period(history,["a","b"],stamp(at(14_000_000)),stamp(at(25_000_000)),now=at(25_000_000))
            incident=incidents.capture(history,room="a",now=at(25_000_000))["detail"]
        self.assertEqual([w["reason"] for w in incident["maintenance"]],["Janela 1","Janela 2"])
        room=period["a"]
        example={"data":period,"start":room["start"],"end":room["end"],"availability":room["availability"]}
        html=timeline.page(["a","b"],example=example)
        result=self.run_browser(html,"""
(async()=>{
 const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
 try{
   let saved;URL.createObjectURL=blob=>{saved=blob;return "blob:teste";};
   HTMLAnchorElement.prototype.click=()=>{};el("availability-csv").click();
   out.textContent=JSON.stringify({csv:await saved.text(),entries:el("events").innerText,
     row:Array.from(el("availability-rows").rows[0].cells,c=>c.textContent)});
 }catch(e){out.textContent=JSON.stringify({error:String(e)});}
})();""")
        a,b=list(csv.DictReader(io.StringIO(result["csv"]),delimiter=";"))
        self.assertEqual((a["em_interrupcao_s"],a["interrupcao_em_manutencao_s"],a["interrupcao_fora_de_manutencao_s"]),
                         ("9","3,000004","5,999996"))
        self.assertEqual(result["row"][-2:],["3,000004 s","5,999996 s"])
        self.assertEqual(b["em_interrupcao_s"],"0")
        self.assertIn("Janela 1",result["entries"])
        self.assertIn("Janela 2",result["entries"])
        self.assertNotIn("Janela 3",result["entries"])
        self.assertIn("01/01/2027",result["entries"])

    def test_agendar_no_futuro_preserva_filtro_linha_do_tempo_e_csv_abertos(self):
        cache=Path(__file__).resolve().parents[1]/".cache"/"testes"
        cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            page=demonstrate_maintenance(folder).read_text(encoding="utf-8")
        result=self.run_browser(page,"""
(async()=>{
 const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
 try{
   const f=el("report"),loaded=new Promise(ok=>f.onload=ok);el("advance").click();await loaded;
   const first=f.contentDocument,field=id=>f.contentDocument.getElementById(id);
   field("start").value="2026-01-15T14:19:59.500";field("end").value="2026-01-15T14:20:00.500";
   field("start").dispatchEvent(new Event("input"));field("filters").requestSubmit();
   el("start").value="2026-01-15T15:00";el("end").value="2026-01-15T15:30";
   el("reason").value="Próxima troca";el("schedule").requestSubmit();
   await new Promise(ok=>setTimeout(ok,100));
   let blob;f.contentWindow.URL.createObjectURL=b=>{blob=b;return "blob:teste";};
   f.contentWindow.HTMLAnchorElement.prototype.click=()=>{};field("availability-csv").click();
   out.textContent=JSON.stringify({same:first===f.contentDocument, csv:await blob.text(),
      entries:field("events").innerText,period:field("availability-period").textContent});
 }catch(e){out.textContent=JSON.stringify({error:String(e)});}
})();""")
        rows=list(csv.DictReader(io.StringIO(result["csv"]),delimiter=";"))
        self.assertEqual([row["interrupcao_em_manutencao_s"] for row in rows],["0,5","0"])
        self.assertEqual([row["interrupcao_fora_de_manutencao_s"] for row in rows],["0,5","1"])
        self.assertTrue(result["same"])
        self.assertIn("Troca de bateria",result["entries"])
        self.assertNotIn("Próxima troca",result["entries"])
        self.assertIn("14:19:59.5",result["period"])
