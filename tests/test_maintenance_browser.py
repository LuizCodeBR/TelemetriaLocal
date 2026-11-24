"""Agenda, linha do tempo e CSV exercitados em um navegador com o HTML offline."""
import csv
import io
import json
from pathlib import Path
import tempfile

import test_timeline_browser as browser
from telemetria import incidents, maintenance
from telemetria.demo_maintenance import demonstrate_maintenance
from telemetria.storage import History


class MaintenanceBrowserTests(browser.ChromiumReportTests):
    @classmethod
    def setUpClass(cls):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True,exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=cache)
        cls.addClassCleanup(folder.cleanup)
        path = demonstrate_maintenance(folder.name)
        cls.demo = path.read_text(encoding="utf-8")
        history = History(path.parent / "central.sqlite3")
        cls.incident = incidents.capture(history,room="sala-01")

    def test_html_isolado_agenda_relatorio_linha_do_tempo_e_csv(self):
        html = self.demo.replace("<head>","<head><script>window.fetch=()=>{throw Error('Rede inesperada');};</script>",1)
        result = self.run_browser(html, """
(async () => {
  const out = document.body.appendChild(document.createElement("output")); out.id="qa-result";
  const check = {}, frame=el("report");
  try {
    async function advance() {
      const loaded = new Promise(resolve => frame.addEventListener("load",resolve,{once:true}));
      el("advance").click(); await loaded;
      return frame.contentDocument;
    }
    function rows(doc) {
      return Array.from(doc.querySelectorAll("#availability-rows tr"),r=>Array.from(r.cells,c=>c.textContent));
    }
    const doc = await advance();
    check.base = rows(doc).map(r=>r.slice(-2));
    check.timeline = doc.querySelector("#events").innerText;
    const win = frame.contentWindow;
    let blob;
    win.URL.createObjectURL = value => {blob=value;return "blob:teste";};
    win.HTMLAnchorElement.prototype.click = () => {};
    doc.querySelector("#availability-csv").click(); check.csv=await blob.text();
    doc.querySelector("#start").value="2026-01-15T14:19:59.500";
    doc.querySelector("#start").dispatchEvent(new Event("input"));
    doc.querySelector("#end").value="2026-01-15T14:20:00.500";
    doc.querySelector("#filters").requestSubmit(); check.cut=rows(doc).map(r=>r.slice(-2));
    check.mobile=document.documentElement.scrollWidth<=window.innerWidth+1;
    el("restart").click(); el("agenda").querySelector("button").click();
    check.canceled=el("agenda").innerText;
    el("room").value="sala-02"; el("reason").value="Substituir sensor";
    el("schedule").requestSubmit(); check.saved=el("feedback").textContent;
    el("schedule").requestSubmit(); check.conflict=el("feedback").textContent;
    const after=await advance(); check.changed=rows(after).map(r=>r.slice(-2));
    el("restart").click(); check.reset=el("agenda").rows.length;
    out.textContent=JSON.stringify(check);
  } catch (e) {out.textContent=JSON.stringify({error:String(e)});}
})();
""")
        self.assertEqual(result["base"],[["900 s","420 s"],["0 s","1320 s"]])
        self.assertIn("Troca de bateria",result["timeline"])
        self.assertIn("Interrupção com retomada",result["timeline"])
        self.assertEqual(result["cut"],[["0,5 s","0,5 s"],["0 s","1 s"]])
        rows = list(csv.DictReader(io.StringIO(result["csv"]),delimiter=";"))
        self.assertEqual([(r["em_interrupcao_s"],r["interrupcao_em_manutencao_s"],r["interrupcao_fora_de_manutencao_s"])
                          for r in rows],[("1320","900","420"),("1320","0","1320")])
        self.assertIn("Cancelada",result["canceled"])
        self.assertIn("confirmado",result["saved"])
        self.assertIn("já tem",result["conflict"])
        self.assertEqual(result["changed"],[["0 s","1320 s"],["900 s","420 s"]])
        self.assertTrue(result["mobile"])
        self.assertEqual(result["reset"],1)

    def test_detalhe_do_incidente_preserva_ordem_e_exibe_faixa(self):
        stub = '<script>window.fetch=async()=>({ok:true,json:async()=>('+json.dumps(self.incident)+')});</script>'
        html = incidents.page().replace('<script type="application/json"',stub+'<script type="application/json"',1)
        out = self.run_browser(html,"""
setTimeout(()=>{
  const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
  out.textContent=JSON.stringify({times:el("stream").innerText, context:el("planned-summary").textContent,
                                  windows:el("planned-windows").textContent});
},50);
""")
        self.assertIn("Comunicação retomada",out["times"])
        self.assertIn("Troca de bateria",out["windows"])
        self.assertIn("900 s de interrupção durante a manutenção · 420 s fora",out["context"])

    def test_pagina_ao_vivo_envia_para_o_servidor_e_exibe_conflito(self):
        state={"now":"2026-01-15T13:55:00.000000+00:00","origin":"1"*32,"windows":[],"rooms":["sala-01"]}
        stub="""<script>window.ops=[];window.fetch=async(url,options={})=>{
          if (!options.method) return {ok:true,json:async()=>STATE};
          ops.push(JSON.parse(options.body));
          return ops.length===1 ? {ok:false,status:409,json:async()=>({error:"Horário ocupado."})} : {ok:true,json:async()=>({created:true})};
        };</script>""".replace("()=>STATE","()=>("+json.dumps(state)+")")
        html=maintenance.page().replace('<script type="application/json"',stub+'<script type="application/json"',1)
        out=self.run_browser(html,"""
(async()=>{
  const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
  const tick=()=>new Promise(resolve=>setTimeout(resolve,10));
  try {
    await tick();el("start").value="2026-01-15T14:00";el("end").value="2026-01-15T14:20";
    el("reason").value="Teste";el("schedule").requestSubmit();await tick();const conflict=el("feedback").textContent;
    el("schedule").requestSubmit();await tick();
    out.textContent=JSON.stringify({conflict,saved:el("feedback").textContent,same:ops[0].key===ops[1].key,
                                    sensor:ops[0].sensor, start:ops[0].start});
  }catch(e){out.textContent=JSON.stringify({error:String(e)});}
})();
""")
        self.assertEqual(out,{"conflict":"Horário ocupado.","saved":"Agendamento confirmado.","same":True,
                              "sensor":"sala-01","start":"2026-01-15T14:00:00.000000+00:00"})
