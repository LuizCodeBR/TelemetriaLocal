"""Exercita o exemplo isolado e a troca de sala sob consultas concorrentes no navegador."""
import json
from pathlib import Path
import re
import tempfile

import test_timeline_browser as browser
from telemetria import freshness_report
from telemetria.demo_freshness import demonstrate_freshness


class FreshnessBrowserTests(browser.ChromiumReportTests):
    @classmethod
    def setUpClass(cls):
        cache=Path(__file__).resolve().parents[1]/".cache"/"testes"
        cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            page=demonstrate_freshness(folder)
            cls.html=page.read_text(encoding="utf-8")
        cls.sample=json.loads(re.search(r'id="bootstrap">(.*?)</script>',cls.html,re.S)[1])["example"]

    def test_html_offline_quatro_etapas_filtro_e_reinicio(self):
        page=self.html.replace("<head>","<head><script>window.requests=0;window.fetch=()=>{requests++;throw Error()};</script>",1)
        result=self.run_browser(page,"""
const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
try {
 const capture=()=>({rows:Array.from(el("current").rows,r=>Array.from(r.cells,c=>c.textContent)),
   trace:el("detail").hidden?"":el("trace").textContent,events:el("events").innerText,
   windows:el("maintenance-list").textContent,split:el("maintenance-total").textContent});
 const stages=[capture()];for(let i=0;i<3;i++){el("advance").click();stages.push(capture());}
 const finished=el("advance").disabled;
 el("room").value="sala-02";el("room").dispatchEvent(new Event("change"));const other=capture();
 el("restart").click();out.textContent=JSON.stringify({stages,other,finished,requests,reset:capture(),
   mobile:document.documentElement.scrollWidth<=window.innerWidth+1});
}catch(e){out.textContent=JSON.stringify({error:String(e)});}
""")
        initial,stale,late,recovered=result["stages"]
        self.assertIn("Nenhuma ocorrência",initial["events"])
        for data in (stale,late):
            self.assertEqual(data["rows"][0][1],"Em comunicação")
            self.assertEqual(data["rows"][0][3],"Medição desatualizada")
            self.assertIn("14:20:00",data["rows"][0][4])
            self.assertEqual(data["rows"][1][3],"Medição atualizada")
        self.assertEqual(recovered["rows"][0][3],"Medição atualizada")
        self.assertIn("14:20:24",recovered["rows"][0][4])
        self.assertIn("00000000000000000000000000000003",recovered["trace"])
        self.assertIn("14:20:25",recovered["trace"])
        self.assertIn("Troca de bateria",recovered["windows"])
        self.assertEqual(recovered["split"],"5 s em manutenção · 5 s fora da manutenção.")
        self.assertIn("Nenhuma ocorrência",result["other"]["events"])
        self.assertEqual(result["requests"],0)
        self.assertTrue(result["finished"] and result["mobile"])
        self.assertEqual(result["reset"]["rows"],initial["rows"])

    def test_grafico_offline_exibe_faixa_da_ocorrencia_e_manutencao(self):
        from telemetria import timeline
        from telemetria.storage import History
        cache=Path(__file__).resolve().parents[1]/".cache"/"testes"
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            db=History(demonstrate_freshness(folder).parent/"central.sqlite3")
            first,last=self.sample[0]["report"]["seen"],self.sample[-1]["report"]["seen"]
            rooms=["sala-01","sala-02"]
            data=timeline.capture_period(db,rooms,first,last)
        example={"data":data,"start":first,"end":last,"availability":data[rooms[0]]["availability"]}
        page=timeline.page(rooms,example=example)
        result=self.run_browser(page,"""
const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
try{
 const rects=[];const original=CanvasRenderingContext2D.prototype.fillRect;
 CanvasRenderingContext2D.prototype.fillRect=function(...args){
   if(this.fillStyle==="#9e4422" || this.fillStyle==="#68529a") rects.push([this.fillStyle,args[2]]);
   return original.apply(this,args);
 };
 draw();const events=el("events").textContent;
 el("sensor").value="sala-02";el("sensor").dispatchEvent(new Event("change"));
 out.textContent=JSON.stringify({events,rects,other:el("events").textContent});
}catch(e){out.textContent=JSON.stringify({error:String(e)});}
""")
        self.assertIn("Medição desatualizada",result["events"])
        self.assertIn("Troca de bateria",result["events"])
        self.assertNotIn("Medição desatualizada",result["other"])
        self.assertEqual({r[0] for r in result["rects"] if r[1]>0},{"#9e4422","#68529a"})


    def test_consulta_lenta_e_sala_removida_retornam_o_historico_correto(self):
        stub = """<script>
window.calls = [];
window.fetch = url => new Promise(resolve => calls.push({url,resolve}));
window.setInterval = fn => { window.poll = fn; return 1; };
</script>"""
        page = freshness_report.page().replace('<script type="application/json"',stub+'<script type="application/json"',1)
        script = """
(async () => {
 const out = document.body.appendChild(document.createElement("output")); out.id="qa-result";
 try {
   const snap = DATA;
   const flush = () => new Promise(resolve => setTimeout(resolve,0));
   const reply = (i,data) => calls[i].resolve({ok:true,json:async()=>data});
   poll(); poll();
   const pending = calls.length;
   reply(0,snap); await flush();
   const rendered = el("current").innerText;
   el("room").value="sala-01"; el("room").dispatchEvent(new Event("change"));
   const rest = { ...snap, rooms:["sala-02"], current:snap.current.filter(r=>r.sensor==="sala-02"), events:[] };
   reply(calls.length-1,{...rest,current:[]}); await flush();
   const lastRequest = calls[calls.length-1].url;
   reply(calls.length-1,rest); await flush();
   out.textContent = JSON.stringify({pending,rendered,lastRequest,filter:el("room").value,rows:el("current").innerText});
 } catch(e) { out.textContent = JSON.stringify({error:String(e)}); }
})();""".replace("DATA",json.dumps(self.sample[-1]["report"]))
        result = self.run_browser(page,script)
        self.assertEqual(result["pending"],1)
        self.assertIn("sala-01",result["rendered"])
        self.assertEqual(result["lastRequest"],"/api/atualidade?")
        self.assertEqual(result["filter"],"")
        self.assertIn("sala-02",result["rows"])

    def test_resposta_antiga_nao_substitui_consulta_mais_recente(self):
        stub='<script>window.calls=[];window.fetch=url=>new Promise(resolve=>calls.push({url,resolve}));</script>'
        page=freshness_report.page().replace('<script type="application/json"',stub+'<script type="application/json"',1)
        data=json.dumps(self.sample[-1]["report"]).replace("<",r"\u003c")
        script="""
(async()=>{
 const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
 try{
  const snap=DATA,reply=(i,value)=>calls[i].resolve({ok:true,json:async()=>value});
  reply(0,snap);await new Promise(r=>setTimeout(r,0));
  el("room").value="sala-01";el("room").dispatchEvent(new Event("change"));
  el("room").value="sala-02";el("room").dispatchEvent(new Event("change"));
  const one=name=>({...snap,current:snap.current.filter(r=>r.sensor===name),events:snap.events.filter(e=>e.sensor===name)});
  reply(2,one("sala-02"));await new Promise(r=>setTimeout(r,0));
  reply(1,one("sala-01"));await new Promise(r=>setTimeout(r,0));
  out.textContent=JSON.stringify({filter:el("room").value,rows:el("current").innerText,hidden:el("detail").hidden,
   urls:calls.slice(1,3).map(c=>c.url)});
 }catch(e){out.textContent=JSON.stringify({error:String(e)});}
})();""".replace("DATA",data)
        result=self.run_browser(page,script)
        self.assertEqual(result["filter"],"sala-02")
        self.assertIn("sala-02",result["rows"])
        self.assertNotIn("sala-01",result["rows"])
        self.assertTrue(result["hidden"])
        self.assertEqual(result["urls"],["/api/atualidade?sala=sala-01","/api/atualidade?sala=sala-02"])
