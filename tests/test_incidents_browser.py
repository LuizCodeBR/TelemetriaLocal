"""Interação offline e atualização do painel com respostas em ordens diferentes."""
import asyncio
import json
import re
import tempfile
from pathlib import Path

import test_timeline_browser as browser
from telemetria import incidents
from telemetria.demo_communication import demonstrate_incidents


class IncidentBrowserTests(browser.ChromiumReportTests):
    def inspect(self, page, scenario):
        return self.run_browser(page, """
(async () => {
  const out=document.body.appendChild(document.createElement("output")); out.id="qa-result";
  const check={};
  const write = value => {el("note").value=value; el("note").dispatchEvent(new Event("input"));};
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  try {SCENARIO
    out.textContent=JSON.stringify(check);
  } catch (err) {out.textContent=JSON.stringify({error:String(err)});}
})();
""".replace("SCENARIO", scenario))

    def test_arquivo_mqtt_marca_anota_avanca_e_reinicia_sem_rede(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            path = asyncio.run(demonstrate_incidents(folder))
            page = path.read_text(encoding="utf-8")
        example = json.loads(re.search(r'id="bootstrap">(.*?)</script>',page,re.S)[1])
        self.assertEqual(example["incident"]["outcome"], "retomada")
        self.assertEqual(example["rooms"], ["sala-01","sala-02"])
        result = self.inspect(page, """
    window.fetch = () => {throw new Error("A demo tentou acessar a rede.");};
    check.start=el("facts").textContent;
    el("unseen").click(); el("seen").click(); await tick();
    check.openAfterSeen=el("facts").textContent;
    check.empty=el("events").textContent; check.pinned=el("list-status").textContent;
    write("Verificando cabo <A>."); el("note-form").requestSubmit(); await tick();
    el("advance").click(); await tick();
    check.end=el("facts").textContent;
    check.lines=Array.from(el("stream").children, li=>li.textContent);
    const url=new URL(el("deliveries").href); check.link=[url.protocol,Object.fromEntries(new URLSearchParams(url.hash.slice(1)))];
    el("restart").click(); await tick();
    check.reset=[el("stream").children.length,el("seen").disabled,el("note").value,el("deliveries").hidden];
    el("room").value="sala-02";el("room").dispatchEvent(new Event("change"));
    check.stable=el("detail-panel").hidden;
""")
        self.assertIn("Em aberto",result["start"])
        self.assertIn("Vista após 5 s",result["openAfterSeen"])
        self.assertIn("Em aberto",result["openAfterSeen"])
        self.assertIn("Nenhuma ocorrência",result["empty"])
        self.assertIn("fora do filtro",result["pinned"])
        self.assertIn("17 s de interrupção",result["end"])
        self.assertEqual([entry.split(" UTC",1)[1] for entry in result["lines"]],
                         ["Interrupção detectada","Marcado como visto","Nota · Verificando cabo <A>.","Comunicação retomada"])
        self.assertEqual(result["link"], ["blob:",{"sala":"sala-01","inicio":"2026-01-15T14:00:15.000000+00:00",
                                                    "fim":"2026-01-15T14:01:32.000000+00:00"}])
        self.assertEqual(result["reset"],[1,False,"",True])
        self.assertTrue(result["stable"])

    def test_resposta_antiga_e_rascunho_na_troca_de_sala(self):
        page = incidents.page().replace('<script type="application/json"',
            '<script>window.calls=[];window.fetch=(url,opt)=>new Promise(reply=>calls.push({url,opt,reply}));</script>'
            '<script type="application/json"',1)
        result = self.inspect(page, """
    const t = "2026-01-15T14:00:15.000000+00:00";
    const make=(id,sensor)=>({id,sensor,token:id.repeat(32),detected:t,ended:null,outcome:null,until:t,seen_at:null,actions:[]});
    const a=make("1","a"), b=make("2","b");
    const respond=(call,item,items=[a,b]) => call.reply({ok:true,json:async()=>({rooms:["a","b"],items,detail:item})});
    respond(calls[0],a);await tick();
    write("Rascunho da sala A");
    const older=refresh(), oldCall=calls.at(-1);
    el("events").rows[1].querySelector("button").click();
    respond(calls.at(-1),b);await tick();
    respond(oldCall,a);await older;
    check.b=[el("title").textContent,el("note").value];
    write("Nota B");const send=submitAction("nota");
    const first=calls.at(-1); first.reply({ok:false});await send;
    check.retryText=el("note").value;const retry=submitAction("nota");const second=calls.at(-1);
    check.requests=[first.url,second.url,first.opt.body===second.opt.body];
    write("Próxima nota B");
    second.reply({ok:true});await tick();respond(calls.at(-1),b);await retry;
    check.newDraft=el("note").value;
    el("events").rows[0].querySelector("button").click();
    respond(calls.at(-1),a);await tick();check.a=el("note").value;
""")
        self.assertEqual(result["b"],["b · ocorrência 2",""])
        self.assertEqual(result["retryText"],"Nota B")
        self.assertEqual(result["requests"],["/api/incidentes/2/nota","/api/incidentes/2/nota",True])
        self.assertEqual(result["newDraft"],"Próxima nota B")
        self.assertEqual(result["a"],"Rascunho da sala A")

    def test_empate_de_horarios_respeita_ordem_de_gravacao_da_retomada(self):
        from test_incidents import IncidentFixture
        from telemetria.storage import History
        fixture = IncidentFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.action("visto", {}, 20)
        fixture.action("nota", {"chave":"a"*32,"texto":"Antes"},20)
        fixture.monitor.advance(fixture.at(20),"sala-01","retomada")
        fixture.action("nota", {"chave":"b"*32,"texto":"Depois"},20)
        report = incidents.capture(History(fixture.path), now=fixture.at(21))
        result = self.inspect(incidents.page(), "draw(" + json.dumps(report) + """);
    check.order = Array.from(el("stream").children, li=>li.lastChild.textContent);
""")
        self.assertEqual(result["order"],["Interrupção detectada","Marcado como visto","Nota · Antes",
                                          "Comunicação retomada","Nota · Depois"])

    def test_confirmacao_perdida_recarrega_mesma_aba_e_muda_de_base(self):
        # As duas páginas têm contextos JS independentes e compartilham a sessão da aba.
        stub = """<script>
window.fetch = async (url, options={}) => {
  const store = sessionStorage;
  const token = store.getItem("ensaio-base") || "a".repeat(32);
  const t = "2026-01-15T14:00:15.000000+00:00";
  const key = "ensaio-acoes-" + token;
  const saved = JSON.parse(store.getItem(key) || "{}");
  if (options.method === "POST") {
    const post = JSON.parse(options.body);
    if (post.ocorrencia !== token) return {ok:false,status:409};
    saved[post.chave] = post.texto;
    store.setItem(key,JSON.stringify(saved));
    if (!store.getItem("ensaio-resposta-perdida")) {
      store.setItem("ensaio-resposta-perdida","1"); throw Error("Sem confirmação");
    }
    return {ok:true,status:200};
  }
  const item={id:"1",token,sensor:"sala",detected:t,until:t,ended:null,outcome:null,seen_at:null,
              actions:Object.values(saved).map(body=>({at:t,body}))};
  return {ok:true,json:async()=>({rooms:["sala"],items:[item],detail:item})};
};
</script>"""
        page = incidents.page().replace('<script type="application/json"',stub+'<script type="application/json"',1)
        source = json.dumps(page).replace("</",r"<\/")
        scenario = """
    await refresh();
    write("Nota com resposta perdida"); await submitAction("nota");
    check.before = el("feedback").textContent;
    const frame = document.createElement("iframe");
    const loaded = new Promise(ok=>frame.onload=ok);
    frame.srcdoc = APP;
    document.body.append(frame); await loaded;
    const w = frame.contentWindow, field = w.document.getElementById("note");
    await w.refresh(); check.restored = field.value;
    field.value += " "; field.dispatchEvent(new w.Event("input"));
    await w.submitAction("nota");
    const saved = () => JSON.parse(sessionStorage.getItem("ensaio-acoes-"+"a".repeat(32)) || "{}");
    check.afterRetry = [Object.keys(saved()).length,field.value];
    field.value="Nota com resposta perdida";field.dispatchEvent(new w.Event("input"));
    await w.submitAction("nota"); check.intentional = Object.keys(saved()).length;
    field.value="Só para a primeira base";field.dispatchEvent(new w.Event("input"));
    sessionStorage.setItem("ensaio-base","b".repeat(32));
    await w.submitAction("nota");check.replaced=[field.value,w.document.getElementById("feedback").textContent];
    check.other = JSON.parse(sessionStorage.getItem("ensaio-acoes-"+"b".repeat(32)) || "{}");
    frame.remove();
""".replace("APP",source)
        result = self.inspect(page,scenario)
        self.assertIn("Tente novamente",result["before"])
        self.assertEqual(result["restored"],"Nota com resposta perdida")
        self.assertEqual(result["afterRetry"],[1,""])
        self.assertEqual(result["intentional"],2)
        self.assertEqual(result["other"],{})
        self.assertEqual(result["replaced"][0],"")
        self.assertIn("Confira a ocorrência",result["replaced"][1])

    def test_horario_recuado_e_registro_legado_explicam_a_sequencia(self):
        result = self.inspect(incidents.page(), """
    const t = n => "2026-01-15T14:00:" + n + ".000000+00:00";
    const report = {id:"1",token:"1".repeat(32),sensor:"a",detected:t(15),ended:t(20),until:t(20),
      outcome:"retomada",seen_at:t(25),recovery_position:1,actions:[{at:t(25),body:null},{at:t(25),body:"Depois"}]};
    drawDetail(report); check.times=Array.from(el("stream").querySelectorAll("time"),v=>v.dateTime);
    check.hint=el("timing").textContent;
    drawDetail({...report,recovery_position:null});check.legacy=el("timing").textContent;
    check.legacyTimes=Array.from(el("stream").querySelectorAll("time"),v=>v.dateTime);
""")
        self.assertEqual([v[17:19] for v in result["times"]],["15","25","20","25"])
        self.assertIn("relógio recuou",result["hint"])
        self.assertIn("desconhecida",result["legacy"])
        self.assertEqual([v[17:19] for v in result["legacyTimes"]],["15","20","25","25"])
