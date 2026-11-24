"""Exercita o HTML e o CSV no navegador local quando Chromium, Edge ou Chrome está disponível."""
import csv
from datetime import UTC, datetime, timedelta
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from telemetria import timeline
from telemetria.communication import stamp

ROOT = Path(__file__).resolve().parents[1]


def at(second):
    return datetime(2026, 1, 15, 14, tzinfo=UTC) + timedelta(seconds=second)


def chromium():
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge", "chrome"):
        browser = shutil.which(name)
        if browser:
            return browser
    for folder in ("ProgramFiles(x86)", "ProgramFiles"):
        for suffix in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            candidate = Path(os.environ.get(folder, "")) / suffix
            if candidate.is_file():
                return str(candidate)
    return None


@unittest.skipUnless(chromium(), "Teste visual disponível com Edge, Chromium ou Chrome")
class ChromiumReportTests(unittest.TestCase):
    def run_browser(self, page, script):
        cache = ROOT / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache, ignore_cleanup_errors=True) as temp:
            folder = Path(temp)
            source = folder / "ensaio.html"
            source.write_text(page.replace("</body>", "<script>" + script + "</script></body>"), encoding="utf-8")
            args = [chromium(), "--headless", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
                    "--disable-crash-reporter", "--no-first-run", "--no-default-browser-check", "--disable-sync",
                    "--user-data-dir=" + str(folder / "perfil"), "--disk-cache-dir=" + str(folder / "cache"),
                    "--virtual-time-budget=6000", "--window-size=390,850", "--dump-dom", source.as_uri()]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                args.insert(1, "--no-sandbox")
            result = subprocess.run(args, capture_output=True, timeout=35, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        dom = result.stdout.decode("utf-8")
        record = re.search(r'<output id="qa-result">([^<]+)</output>', dom)
        self.assertIsNotNone(record, dom[-1800:])
        outcome = json.loads(html.unescape(record[1]))
        self.assertNotIn("error", outcome, outcome)
        return outcome


class BrowserTimelineTests(ChromiumReportTests):
    def test_filtro_detalhe_paginacao_e_csv_do_mesmo_recorte_em_arquivo_local(self):
        names = ["sala-01", "-sala-02"]
        rows = [dict(id=f"{i:032x}", measured_at=stamp(at(i)), received_at=stamp(at(39)),
                     temperature=-2.5, humidity=52.5) for i in range(34)]
        start, end = stamp(at(0)), stamp(at(40))
        data = {n: {"sensor":n, "start":start, "end":end, "rows":rows, "events":[], "gaps":[]} for n in names}
        example = {"start":start, "end":end, "data":data}
        page = timeline.page(names, example=example)
        script = """
(async () => {
  const result = document.createElement('output');
  result.id = 'qa-result';
  document.body.append(result);
  try {
    const check = {};
    check.initial = el('pagination').textContent;
    el('next').click();
    check.secondPage = el('pagination').textContent;
    sensor.value = '-sala-02';
    sensor.dispatchEvent(new Event('change'));
    check.changed = el('result').textContent;
    preset.value = '1';
    preset.dispatchEvent(new Event('change'));
    check.anchor = el('end').value;
    el('start').value = '2026-01-15T14:00:02';
    el('start').dispatchEvent(new Event('input'));
    el('end').value = '2026-01-15T14:00:04';
    form.requestSubmit();
    check.rows = el('readings').rows.length;
    el('readings').querySelector('button').click();
    check.detail = el('detail').textContent;
    let saved;
    URL.createObjectURL = blob => {saved = blob;return 'blob:capturado';};
    HTMLAnchorElement.prototype.click = function () {check.download = this.download;};
    el('csv').click();
    check.csv = await saved.text();
    check.bom = [...new Uint8Array(await saved.arrayBuffer()).slice(0,3)];
    el('start').value = '2026-01-15T14:00:04';
    el('end').value = '2026-01-15T14:00:02';
    form.requestSubmit();
    check.invalid = el('message').textContent;
    check.disabled = el('csv').disabled;
    result.textContent = JSON.stringify(check);
  } catch (e) {result.textContent = JSON.stringify({error: String(e)});}
})();
"""
        page = page.replace("<script type=", "<script>window.fetch=()=>{throw Error('Rede usada no arquivo offline');};</script><script type=", 1)
        outcome = self.run_browser(page, script)
        self.assertEqual(outcome["initial"], "1–30 de 34")
        self.assertEqual(outcome["secondPage"], "31–34 de 34")
        self.assertIn("-sala-02", outcome["changed"])
        self.assertTrue(outcome["anchor"].startswith("2026-01-15T14:00:40"))
        self.assertEqual(outcome["rows"], 2)
        self.assertIn("14:00:39", outcome["detail"])
        self.assertIn("-sala-02", outcome["download"])
        self.assertEqual(outcome["bom"], [239,187,191])
        exported = list(csv.DictReader(io.StringIO(outcome["csv"]), delimiter=";"))
        self.assertEqual([r["id"] for r in exported], [rows[i]["id"] for i in (2,3)])
        for r in exported:
            self.assertEqual((r["sensor"], r["temperatura_c"], r["umidade_pct"]), ("'-sala-02", "-2,5", "52,5"))
        self.assertTrue(outcome["disabled"])
        self.assertIn("posterior", outcome["invalid"])

    def test_resposta_atrasada_erro_e_edicao_preservam_o_recorte_exportavel(self):
        page = timeline.page(["sala-a", "sala-b"])
        setup = """
<script>
window.requests = [];
window.fetch = (url, options) => new Promise((resolve, reject) => {
  requests.push({url, signal: options.signal, resolve, reject});
});
Date.now = () => Date.UTC(2099, 0, 1);
</script>"""
        page = page.replace('<script type="application/json"', setup + '<script type="application/json"', 1)
        scenario = """
(async () => {
  const data = (name, count) => ({
    sensor:name, start:"2026-01-15T13:00:00.000000+00:00", end:"2026-01-15T14:00:00.000000+00:00",
    events:[], gaps:[], rows:Array.from({length:count}, (_,i) => ({
      id:i.toString(16).padStart(32,"0"), measured_at:"2026-01-15T13:00:01.000000+00:00",
      received_at:"2026-01-15T13:00:05.000000+00:00", temperature:24, humidity:52
    }))
  });
  const output = document.body.appendChild(document.createElement("output"));
  output.id = "qa-result";
  try {
    const check = {first:requests[0].url};
    let late;
    requests[0].resolve({ok:true,json:() => new Promise(resolve => {late = resolve;})});
    await Promise.resolve();
    sensor.value = "sala-b";
    const newest = loadPeriod();
    requests[1].resolve({ok:true,json:async () => data("sala-b",37)});
    await newest;
    let file, exports = 0;
    URL.createObjectURL = blob => {file = blob; return "blob:teste";};
    HTMLAnchorElement.prototype.click = function () {exports++;};
    el("csv").click();
    late(data("sala-a",1));
    await Promise.resolve();await Promise.resolve();
    check.current = el("result").textContent;
    check.dates = [el("start").value,el("end").value];
    check.page = el("pagination").textContent;
    const oldError = loadPeriod();
    sensor.value = "sala-a";
    const newLoad = loadPeriod();
    el("csv").click();
    requests[3].resolve({ok:true,json:async () => data("sala-a",2)});
    await newLoad;
    requests[2].reject(Error("Falha antiga"));
    await oldError;
    check.newest = el("result").textContent;
    check.message = el("message").textContent;
    const edited = loadPeriod();
    el("start").value = "2026-01-15T13:20";
    el("start").dispatchEvent(new Event("input"));
    requests[4].resolve({ok:true,json:async () => data("sala-a",5)});
    await edited;
    el("csv").click();
    check.unapplied = el("csv").disabled && !view;
    check.exports = exports;
    check.csv = await file.text();
    output.textContent = JSON.stringify(check);
  } catch (err) {output.textContent = JSON.stringify({errorThrown:String(err)});}
})();
"""
        result = self.run_browser(page, scenario)
        self.assertNotIn("errorThrown", result)
        self.assertIn("periodo=1h", result["first"])
        self.assertNotIn("2099", result["first"])
        self.assertTrue(result["dates"][0].startswith("2026-01-15T13:00"))
        self.assertTrue(result["dates"][1].startswith("2026-01-15T14:00"))
        self.assertIn("sala-b", result["current"])
        self.assertEqual(result["page"], "1–30 de 37")
        self.assertIn("sala-a", result["newest"])
        self.assertEqual(result["message"], "")
        self.assertTrue(result["unapplied"])
        self.assertEqual(result["exports"], 1)
        rows = list(csv.DictReader(io.StringIO(result["csv"]), delimiter=";"))
        self.assertEqual(len(rows), 37)
        self.assertEqual([row["id"] for row in rows], [f"{i:032x}" for i in range(37)])
        self.assertEqual({row["sensor"] for row in rows}, {"sala-b"})

    def test_grafico_subsegundo_pontos_sobrepostos_e_nomes_de_csv(self):
        start, end = stamp(at(0)), stamp(at(1))
        rows = [dict(id=f"{i:032x}", measured_at=stamp(at(.115)), received_at=stamp(at(.5)),
                     temperature=20, humidity=50+i) for i in range(2)]
        source = {"sensor":"sala", "start":start, "end":end, "rows":rows, "events":[], "gaps":[]}
        page = timeline.page(["sala"], example={"start":start, "end":end, "data":{"sala":source}})
        scenario = """
(async () => {
  const output = document.body.appendChild(document.createElement("output")); output.id = "qa-result";
  try {
    const check = {files:[],labels:[]};
    const dots = [];
    const context = chart.getContext("2d"), arc = context.arc.bind(context), fill = context.fillText.bind(context);
    context.arc = (x,y,...rest) => {dots.push([x,y]); return arc(x,y,...rest);};
    context.fillText = (s,...rest) => {if(s.includes(":"))check.labels.push(s);return fill(s,...rest);};
    el("start").value = "2026-01-15T14:00:00.100";
    el("start").dispatchEvent(new Event("input"));
    el("end").value = "2026-01-15T14:00:00.130";
    await loadPeriod();
    check.dots = dots.slice();
    const [x,y] = dots[dots.length-1], rect = chart.getBoundingClientRect();
    chart.dispatchEvent(new MouseEvent("click", {clientX:rect.left+x,clientY:rect.top+y}));
    check.first = el("detail").textContent;
    const nextPoint = document.getElementById("next-point");
    if (nextPoint) nextPoint.click();
    check.second = el("detail").textContent;
    let blob;
    URL.createObjectURL = value => {blob=value;return "blob:teste";};
    HTMLAnchorElement.prototype.click = function () {check.files.push(this.download);};
    el("csv").click();
    check.firstCSV = await blob.text();
    el("end").value = "2026-01-15T14:00:00.115";
    await loadPeriod();el("csv").click();
    check.emptyCSV = await blob.text();
    output.textContent = JSON.stringify(check);
  } catch (err) {output.textContent = JSON.stringify({errorThrown:String(err)});}
})();
"""
        result = self.run_browser(page, scenario)
        self.assertNotIn("errorThrown", result)
        with self.subTest("eixo tem marcas distinguíveis"):
            self.assertGreaterEqual(len(set(result["labels"])), 2)
        with self.subTest("todas as leituras no mesmo ponto"):
            self.assertEqual(result["dots"][0], result["dots"][1])
            self.assertIn(rows[0]["id"], result["first"])
            self.assertIn(rows[1]["id"], result["second"])
        with self.subTest("arquivos identificam os dois limites"):
            self.assertEqual(len(set(result["files"])), 2)
            self.assertIn("130", result["files"][0])
            self.assertIn("115", result["files"][1])
        decoded = list(csv.DictReader(io.StringIO(result["firstCSV"]), delimiter=";"))
        self.assertEqual([r["id"] for r in decoded], [r["id"] for r in rows])
        self.assertEqual(list(csv.DictReader(io.StringIO(result["emptyCSV"]), delimiter=";")), [])

    def test_amostra_offline_recorta_faixas_e_marcadores_na_cobertura_gravada(self):
        start, end = stamp(at(10)), stamp(at(20))
        source = {"sensor":"sala", "start":start, "end":end, "rows":[], "gaps":[],
                  "events":[{"start":stamp(at(5)), "until":stamp(at(25)), "outcome":"retomada"}]}
        page = timeline.page(["sala"], example={"start":start, "end":end, "data":{"sala":source}})
        scenario = """
(async () => {
  const output = document.body.appendChild(document.createElement("output"));output.id = "qa-result";
  const fill = [], markers = [];
  const ctx = chart.getContext("2d");
  const origRect = ctx.fillRect.bind(ctx), origDash = ctx.setLineDash.bind(ctx);
  ctx.fillRect = (...args) => {if (ctx.fillStyle === "#f5ddca") fill.push(args);origRect(...args);};
  ctx.setLineDash = dash => {if (dash.length) markers.push(dash);origDash(dash);};
  async function range(start,end) {
    el("start").value = "2026-01-15T14:00:"+start;
    el("start").dispatchEvent(new Event("input"));
    el("end").value = "2026-01-15T14:00:"+end;
    await loadPeriod();
  }
  await range("00","30");
  const check = {width:fill[0][2], graphWidth:chart.clientWidth-60, markers:markers.length,
                 notice:el("result").textContent, label:el("events").textContent};
  await range("21","26");
  check.unavailable = view.events.length;
  output.textContent = JSON.stringify(check);
})();
"""
        r = self.run_browser(page, scenario)
        self.assertAlmostEqual(r["width"], r["graphWidth"]/3, places=5)
        self.assertEqual(r["markers"], 0)
        self.assertIn("O exemplo contém dados", r["notice"])
        self.assertIn("Interrupção no período", r["label"])
        self.assertEqual(r["unavailable"], 0)

    def test_csv_com_mais_de_dois_lotes_preserva_ordem_precisao_e_quebras(self):
        start, end = stamp(at(0)), stamp(at(1))
        rows = [dict(id=f"{i:032x}", measured_at=stamp(at(i/1000000)), received_at=end,
                     temperature=-1.25, humidity=60) for i in range(2050)]
        source = {"sensor":"sala", "start":start, "end":end, "rows":rows, "events":[], "gaps":[]}
        page = timeline.page(["sala"], example={"start":start, "end":end, "data":{"sala":source}})
        scenario = """
(async () => {
  const output = document.body.appendChild(document.createElement("output"));output.id = "qa-result";
  let file;
  URL.createObjectURL = blob => {file = blob;return "blob:teste";};
  HTMLAnchorElement.prototype.click = () => {};
  el("csv").click();
  const lines = (await file.text()).split("\\r\\n");
  const records = lines.slice(1,-1).map(line => line.split(";").map(v => v.slice(1,-1)));
  const ok = records.every((cells,i) => cells.length === 6 && cells[1] === i.toString(16).padStart(32,"0") &&
    cells[2] === "2026-01-15T14:00:00."+String(i).padStart(6,"0")+"+00:00" && cells[4] === "-1,25");
  output.textContent = JSON.stringify({total:records.length, ok, final:lines.at(-1), table:el("pagination").textContent});
})();
"""
        r = self.run_browser(page, scenario)
        self.assertEqual(r, {"total":2050, "ok":True, "final":"", "table":"1–30 de 2050"})

    def test_disponibilidade_mqtt_csv_e_troca_de_sala_pro_mesmo_periodo(self):
        import asyncio
        from telemetria.demo_communication import demonstrate_timeline
        cache = ROOT / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            path = asyncio.run(demonstrate_timeline(folder, availability_demo=True))
            page = path.read_text(encoding="utf8")
        scenario = """
(async () => {
  const out = document.body.appendChild(document.createElement("output")); out.id = "qa-result";
  try {
    const snap = () => Array.from(el("availability-rows").rows, row => Array.from(row.cells, c=>c.textContent));
    const check = {initial:snap(), period:el("availability-period").textContent};
    let blob;
    URL.createObjectURL = value => {blob=value;return "blob:teste";};
    HTMLAnchorElement.prototype.click = function () {check.file=this.download;};
    el("availability-csv").click();
    check.csv=await blob.text(); check.bom=[...new Uint8Array(await blob.arrayBuffer()).slice(0,3)];
    el("availability-rows").rows[1].querySelector("button").click();
    check.chart = el("result").textContent;
    check.preset = preset.value;
    el("start").value="2026-01-15T14:00:16";el("start").dispatchEvent(new Event("input"));
    check.disabled=el("availability-csv").disabled;
    el("end").value="2026-01-15T14:00:31";form.requestSubmit();
    check.onlyOutage=snap();
    el("start").value="2026-01-15T14:00:32";el("start").dispatchEvent(new Event("input"));
    el("end").value="2026-01-15T14:00:42";form.requestSubmit();
    check.onlyGap=snap();el("availability-csv").click();check.gapCSV=await blob.text();
    out.textContent=JSON.stringify(check);
  } catch(e) {out.textContent=JSON.stringify({error:String(e)});}
})();
"""
        result = self.run_browser(page,scenario)
        self.assertEqual(result["initial"], [
            ["sala-01","46,9%","32 s","15 s","17 s","17 s","10 s","0 s","0 s","17 s"],
            ["sala-02","100%","32 s","32 s","0 s","0 s","10 s","0 s","0 s","0 s"]])
        self.assertTrue(result["file"].startswith("disponibilidade_"))
        self.assertEqual(result["bom"],[239,187,191])
        rows = list(csv.DictReader(io.StringIO(result["csv"]), delimiter=";"))
        self.assertEqual([r["sensor"] for r in rows],["sala-01","sala-02"])
        self.assertEqual([r["disponibilidade_pct"] for r in rows], ["46,9","100"])
        self.assertEqual(rows[0]["central_sem_observacao_s"],"10")
        self.assertEqual(rows[0]["fim_exclusivo_utc"],stamp(at(42)))
        self.assertTrue(result["disabled"])
        self.assertEqual(result["preset"],"custom")
        self.assertIn("sala-02",result["chart"])
        self.assertIn("14:00:42",result["chart"])
        self.assertEqual(result["onlyOutage"][0][1:6],["0%","15 s","0 s","15 s","15 s"])
        self.assertEqual(result["onlyGap"][0][1:],["Sem dados suficientes","0 s","0 s","0 s","0 s","10 s","0 s","0 s","0 s"])
        rows = list(csv.DictReader(io.StringIO(result["gapCSV"]), delimiter=";"))
        self.assertEqual(rows[0]["disponibilidade_pct"], "")

    def test_resumo_tempos_fracionarios_maior_interrupcao_e_amostra_limitada(self):
        start,end=stamp(at(0)),stamp(at(1))
        def interval(a,b):
            return [stamp(at(a)),stamp(at(b))]
        report={"rooms":[{"sensor":"-sala","evaluated":[interval(.000001,.999999)],
                          "outages":[interval(.000002,.000006),interval(.99,.990002)]},
                         {"sensor":"sem-sinal","evaluated":[],"outages":[]}], "blind":[]}
        example={"start":start,"end":end,"availability":report,"data":{
            n:{"rows":[],"gaps":[],"events":[]} for n in ("-sala","sem-sinal")}}
        page = timeline.page(list(example["data"]),example=example)
        script = """
(async () => {
  const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
  let blob;
  URL.createObjectURL=b=>{blob=b;return "blob:teste";};HTMLAnchorElement.prototype.click=()=>{};
  el("end").value="2026-01-15T14:00:02";form.requestSubmit();
  el("availability-csv").click();
  out.textContent=JSON.stringify({csv:await blob.text()});
})();
"""
        result=self.run_browser(page,script)
        a,b=list(csv.DictReader(io.StringIO(result["csv"]),delimiter=";"))
        self.assertEqual(a["sensor"],"'-sala")
        self.assertEqual(a["avaliado_s"],"0,999998")
        self.assertEqual(a["em_comunicacao_s"],"0,999992")
        self.assertEqual(a["em_interrupcao_s"],"0,000006")
        self.assertEqual(a["maior_interrupcao_s"],"0,000004")
        self.assertEqual(a["evidencia_insuficiente_s"],"1,000002")
        self.assertEqual(b["disponibilidade_pct"],"")
        self.assertEqual(b["evidencia_insuficiente_s"],"2")

    def test_resumo_ao_vivo_ignora_resposta_superada_e_detalha_intervalo_absoluto(self):
        page=timeline.page(["a","b"])
        page=page.replace('<script type="application/json"',
            '<script>window.calls=[];window.fetch=url=>new Promise(resolve=>calls.push({url,resolve}));</script><script type="application/json"',1)
        script="""
(async () => {
  const out=document.body.appendChild(document.createElement("output"));out.id="qa-result";
  try {
    const t=s=>"2026-01-15T14:00:"+String(s).padStart(2,"0")+".000000+00:00";
    const data=(name,n)=>({sensor:name,start:t(0),end:t(42),rows:[],events:[],gaps:[],availability:{
      rooms:["a","b"].map(sensor=>({sensor,evaluated:[[t(0),t(32)]],outages:sensor==="a"?[[t(n),t(32)]]:[]})),
      blind:[[t(32),t(42)]]}});
    let old;
    calls[0].resolve({ok:true,json:()=>new Promise(resolve=>{old=resolve;})});await Promise.resolve();
    const latest=loadPeriod();calls[1].resolve({ok:true,json:async()=>data("a",15)});await latest;
    old(data("a",5));await Promise.resolve();await Promise.resolve();
    const check={percent:el("availability-rows").rows[0].cells[1].textContent};
    let blob;
    URL.createObjectURL = value => {blob=value;return "blob:teste";};
    HTMLAnchorElement.prototype.click = () => {};
    el("availability-csv").click(); check.csv=await blob.text();
    el("availability-rows").rows[1].querySelector("button").click();
    const drill=new URL(calls[2].url,"http://localhost");
    check.params=Object.fromEntries(drill.searchParams);
    check.pending=el("availability-csv").disabled;
    calls[2].resolve({ok:true,json:async()=>data("b",15)});
    await new Promise(resolve=>setTimeout(resolve,0));
    check.chart=el("result").textContent;
    el("start").dispatchEvent(new Event("input"));check.edited=el("availability-csv").disabled;
    out.textContent=JSON.stringify(check);
  } catch(e) {out.textContent=JSON.stringify({error:String(e)});}
})();
"""
        result=self.run_browser(page,script)
        self.assertEqual(result["percent"], "46,9%")
        rows = list(csv.DictReader(io.StringIO(result["csv"]), delimiter=";"))
        self.assertEqual([(r["sensor"], r["disponibilidade_pct"], r["em_interrupcao_s"]) for r in rows],
                         [("a", "46,9", "17"), ("b", "100", "0")])
        self.assertEqual(result["params"],{"sensor":"b","inicio":stamp(at(0)),"fim":stamp(at(42))})
        self.assertTrue(result["pending"])
        self.assertTrue(result["edited"])
        self.assertIn("b ·",result["chart"])

    def test_primeira_sala_chega_com_pagina_aberta_e_consultas_fora_de_ordem(self):
        page = timeline.page([])
        stub = """<script>
window.calls = [];
window.fetch = url => new Promise(resolve => calls.push({url, reply: value =>
  resolve({ok:true, json:async () => value})}));
</script>"""
        page = page.replace('<script type="application/json"', stub + '<script type="application/json"', 1)
        script = """
(async () => {
  const out = document.body.appendChild(document.createElement("output")); out.id = "qa-result";
  const settle = () => new Promise(resolve => setTimeout(resolve, 0));
  try {
    calls[0].reply([]); await settle();
    const result = {waiting:el("result").textContent, disabled:el("availability-csv").disabled};
    el("apply").click();
    el("apply").click();
    result.catalog = calls.map(c => c.url);
    calls[2].reply(["b"]); await settle();
    result.query = calls[3].url;
    const t = n => "2026-01-15T14:00:0" + n + ".000000+00:00";
    calls[3].reply({sensor:"b",start:t(0),end:t(2),rows:[],events:[],gaps:[],availability:{
      rooms:["a","b"].map(sensor => ({sensor,evaluated:[[t(0),t(2)]],outages:[]})),blind:[]}});
    await settle();
    calls[1].reply(["antiga"]); await settle();
    result.options = Array.from(sensor.options, o => o.value);
    result.selected = sensor.value;
    result.rooms = Array.from(el("availability-rows").rows, r => r.cells[0].textContent);
    result.csvReady = !el("availability-csv").disabled;
    out.textContent = JSON.stringify(result);
  } catch (e) {out.textContent = JSON.stringify({error:String(e)});}
})();
"""
        result = self.run_browser(page, script)
        self.assertIn("Aplicar", result["waiting"])
        self.assertTrue(result["disabled"])
        self.assertEqual(result["catalog"], ["/api/sensores"] * 3)
        self.assertIn("sensor=b", result["query"])
        self.assertEqual(result["options"], ["a", "b"])
        self.assertEqual(result["selected"], "b")
        self.assertEqual(result["rooms"], ["a", "b"])
        self.assertTrue(result["csvReady"])

    def test_csv_e_tabela_distinguem_percentuais_proximos_dos_extremos(self):
        from decimal import Decimal

        start, end = stamp(at(0)), stamp(at(40))
        tick = stamp(at(.000001))
        cuts = {"ativa": [], "quase-ativa": [[start, tick]], "quase-ausente": [[tick, end]],
                "ausente": [[start, end]], "desconhecida": []}
        rooms = [{"sensor": name, "evaluated": [] if name == "desconhecida" else [[start, end]],
                  "outages": outages} for name, outages in cuts.items()]
        example = {"start": start, "end": end, "availability": {"rooms": rooms, "blind": []},
                   "data": {name: {"rows": [], "events": [], "gaps": []} for name in cuts}}
        page = timeline.page(list(cuts), example=example)
        script = """
(async () => {
  const out = document.body.appendChild(document.createElement("output")); out.id = "qa-result";
  let blob;
  URL.createObjectURL = value => {blob = value; return "blob:teste";};
  HTMLAnchorElement.prototype.click = () => {};
  el("availability-csv").click();
  out.textContent = JSON.stringify({csv:await blob.text(), labels:Array.from(
    el("availability-rows").rows, r => r.cells[1].textContent)});
})();
"""
        result = self.run_browser(page, script)
        rows = list(csv.DictReader(io.StringIO(result["csv"]), delimiter=";"))
        values = [r["disponibilidade_pct"] for r in rows]
        self.assertEqual([r["sensor"] for r in rows], list(cuts))
        self.assertEqual(result["labels"], [s + "%" if s else "Sem dados suficientes" for s in values])
        self.assertEqual([values[0], values[3], values[4]], ["100", "0", ""])
        self.assertTrue(Decimal("99.99") < Decimal(values[1].replace(",", ".")) < 100)
        self.assertTrue(0 < Decimal(values[2].replace(",", ".")) < Decimal("0.01"))
        self.assertEqual(rows[1]["em_interrupcao_s"], "0,000001")
        self.assertEqual(rows[2]["em_comunicacao_s"], "0,000001")

    def test_csv_e_tabela_ao_vivo_e_offline_conferidos_segundo_a_segundo(self):
        from decimal import Decimal, ROUND_HALF_UP
        from itertools import groupby
        from telemetria.communication import Observer
        from telemetria.storage import History, Reading

        names = ["a", "b", "sem-presenca"]
        periods = [(-2, 58), (-2, 0), (0, 3), (3, 15), (14, 16), (15, 22),
                   (21, 25), (24, 30), (29, 33), (32, 45), (45, 49), (48, 55), (54, 58)]
        cache = ROOT / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            history = History(Path(folder) / "central.sqlite3")
            history.record(Reading.sample(names[2], at(0)))
            monitor = Observer(history)
            monitor.start(at(0))
            for second, name in [(0, "a"), (3, "b"), (8, "b"), (14, "b"), (17, "b"), (22, "a")]:
                monitor.advance(at(second), name, str(second))
                if second == 3:
                    from telemetria import maintenance
                    origin = maintenance.agenda(history, now=at(3))["origin"]
                    for key, (room, left, right) in enumerate([("a",16,20),("a",20,26),("b",47,48)]):
                        maintenance.update(history, {"origin":origin,"key":f"{key:032x}","sensor":room,
                            "start":stamp(at(left)),"end":stamp(at(right)),"reason":"Ensaio"}, now=at(3))
            monitor.advance(at(24))
            monitor.stop()
            monitor.start(at(30))
            for second in (32, 40, 45, 47, 49, 54):
                name = "b" if second == 49 else "a"
                monitor.advance(at(second), name, str(second))
            start, end = (stamp(at(n)) for n in periods[0])
            sample = timeline.capture_period(history, names, start, end, now=at(54))
            evidence = sample["a"]["availability"]
            for room in sample.values():
                del room["availability"]
            example = {"start": start, "end": end, "data": sample, "availability": evidence}
            live = [timeline.period(history, "a", stamp(at(i)), stamp(at(j)), now=at(54)) for i, j in periods]

        # Contagem independente por segundo a partir do roteiro de contatos.
        def state(room, sec):
            if 24 <= sec < 30:
                return "central"
            if room == "a":
                if 15 <= sec < 22:
                    return "falha"
                if 0 <= sec < 24 or 32 <= sec < 54:
                    return "contato"
            if room == "b":
                if 45 <= sec < 49:
                    return "falha"
                if 3 <= sec < 24 or 49 <= sec < 54:
                    return "contato"
            return "incerto"

        stub = """<script>
window.snapshots = SNAPSHOTS;
window.fetch = async url => {
  const params = new URL(url, "http://localhost").searchParams;
  const data = params.has("inicio") ? snapshots.find(d =>
    d.start === params.get("inicio") && d.end === params.get("fim")) : snapshots[0];
  return {ok:!!data, json:async () => data};
};
</script>""".replace("SNAPSHOTS", json.dumps(live))
        live_page = timeline.page(names).replace('<script type="application/json"',
                                                  stub + '<script type="application/json"', 1)
        script = """
(async () => {
  const out = document.body.appendChild(document.createElement("output")); out.id = "qa-result";
  try {
    let blob;
    URL.createObjectURL = value => {blob = value; return "blob:teste";};
    HTMLAnchorElement.prototype.click = () => {};
    const tables = [], files = [];
    for (const [a,b] of PERIODS) {
      preset.value = "custom"; el("start").value = a.slice(0,23); el("end").value = b.slice(0,23);
      await loadPeriod();
      el("availability-csv").click();
      tables.push(Array.from(el("availability-rows").rows, r => Array.from(r.cells, c => c.textContent)));
      files.push(blob);
    }
    const csv = await Promise.all(files.map(file => file.text()));
    out.textContent = JSON.stringify({captures:tables.map((table, i) => ({table, csv:csv[i]}))});
  } catch (e) {out.textContent = JSON.stringify({error:String(e)});}
})();
""".replace("PERIODS", json.dumps([[d["start"], d["end"]] for d in live]))
        offline = self.run_browser(timeline.page(names, example=example), script)["captures"]
        self.assertEqual(self.run_browser(live_page, script)["captures"], offline)
        for (start, end), capture in zip(periods, offline, strict=True):
            rows = list(csv.reader(io.StringIO(capture["csv"]), delimiter=";"))[1:]
            self.assertEqual([r[0] for r in rows], names)
            for name, row, cells in zip(names, rows, capture["table"], strict=True):
                with self.subTest(start=start, end=end, sensor=name):
                    states = [state(name, t) for t in range(start, end)]
                    up, down = states.count("contato"), states.count("falha")
                    evaluated = up + down
                    longest = max((len(list(run)) for key, run in groupby(states) if key == "falha"), default=0)
                    durations = [evaluated, up, down, longest, states.count("central"), states.count("incerto")]
                    inside = sum(state(name,t) == "falha" and
                                 ((name == "a" and 16 <= t < 26) or (name == "b" and 47 <= t < 48))
                                 for t in range(start,end))
                    durations.extend([inside,down-inside])
                    self.assertEqual(row[1:3], [stamp(at(start)), stamp(at(end))])
                    self.assertEqual(row[4:], [str(n) for n in durations])
                    if evaluated:
                        percent = (Decimal(100 * up) / evaluated).quantize(Decimal(".1"), rounding=ROUND_HALF_UP)
                        self.assertEqual(Decimal(row[3].replace(",", ".")), percent)
                    else:
                        self.assertEqual(row[3], "")
                    self.assertEqual(cells, [name, row[3] + "%" if evaluated else "Sem dados suficientes",
                                             *[f"{n} s" for n in durations]])
