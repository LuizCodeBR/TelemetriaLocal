"""O relatório de entregas pelo fluxo normal no navegador e nos arquivos CSV."""
import asyncio
import csv
import io
import json
from pathlib import Path
import tempfile

import test_timeline_browser as browser
from test_deliveries import at
from telemetria import deliveries
from telemetria.communication import stamp
from telemetria.demo_communication import demonstrate_deliveries


class DeliveryBrowserTests(browser.ChromiumReportTests):
    def inspect(self, page, scenario):
        script = """
(async () => {
  const out = document.body.appendChild(document.createElement("output")); out.id = "qa-result";
  const check = {};
  const table = id => Array.from(el(id).rows, r => Array.from(r.cells, c => c.textContent));
  const setLimit = value => {el("limit").value = value; el("limit").dispatchEvent(new Event("input"));};
  let blob;
  URL.createObjectURL = b => {blob = b; return "blob:teste";};
  HTMLAnchorElement.prototype.click = function () {check.filename = this.download;};
  const save = async id => {el(id).click(); return blob.text();};
  try {
    SCENARIO
    out.textContent = JSON.stringify(check);
  } catch (e) {out.textContent = JSON.stringify({error:String(e)});}
})();
""".replace("SCENARIO", scenario)
        return self.run_browser(page, script)

    def test_demo_mqtt_duas_salas_troca_de_limite_e_csv(self):
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            page = asyncio.run(demonstrate_deliveries(temp)).read_text(encoding="utf-8")
        result = self.inspect(page, """
    check.ten = table("summary");
    check.summary = await save("summary-csv");
    check.bom = Array.from(new Uint8Array(await blob.arrayBuffer()).slice(0,3));
    setLimit("20"); check.twenty = table("summary");
    check.details = await save("detail-csv");
    el("summary").rows[1].querySelector("button").click();
    check.second = table("details");
    el("start").value = "2026-01-15T14:00:32"; el("start").dispatchEvent(new Event("input"));
    check.blocked = el("summary-csv").disabled && el("detail-csv").disabled;
    el("end").value = "2026-01-15T14:00:33"; el("filters").requestSubmit();
    check.batch = table("summary");
    el("preset").value = "sample"; el("preset").dispatchEvent(new Event("change"));
    check.restored = table("summary");
""")
        self.assertEqual(result["ten"], [["sala-01","7","3","4","0","12 s","27 s"],
                                         ["sala-02","6","6","0","0","0 s","0 s"]])
        self.assertEqual(result["twenty"][0][2:4], ["5","2"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["batch"][0], ["sala-01","6","4","2","0","14,5 s","27 s"])
        self.assertIn("Sem medições recebidas", result["batch"][1])
        self.assertEqual(result["restored"], result["twenty"])
        self.assertEqual(result["bom"], [239,187,191])
        summary = list(csv.DictReader(io.StringIO(result["summary"]), delimiter=";"))
        self.assertEqual([(r["sensor"], r["limite_s"], r["acima_do_limite"]) for r in summary],
                         [("sala-01", "10", "4"), ("sala-02", "10", "0")])
        details = list(csv.DictReader(io.StringIO(result["details"]), delimiter=";"))
        self.assertEqual(len({r["id"] for r in details}), 7)
        self.assertEqual(sorted(int(r["atraso_s"]) for r in details), [0,2,7,12,17,22,27])
        self.assertEqual(sum(r["situacao"] == "Acima do limite" for r in details), 2)
        self.assertTrue(all(r["limite_s"] == "20" for r in details))
        self.assertTrue(all(r["fim_exclusivo_recebimento_utc"] == stamp(at(42)) for r in details))
        self.assertEqual(len(result["second"]), 6)

    def test_fracoes_relogio_invalido_sala_sem_registro_e_campos_em_branco(self):
        received = stamp(at(10))
        def reading(n, delta):
            return {"id": f"{n:032x}", "received_at": received, "measured_at": stamp(at(10, -delta))}
        sample = {"start": stamp(at()), "end": stamp(at(20)), "rooms": [
            {"sensor":"-sala", "readings":[reading(1, 1), reading(2, 2), reading(3, -1)]},
            {"sensor":"relogio", "readings":[reading(4, -1)]}, {"sensor":"vazia", "readings":[]}]}
        result = self.inspect(deliveries.page(sample), """
    setLimit("0,000001"); check.table = table("summary"); check.summary = await save("summary-csv");
    check.details = await save("detail-csv");
    setLimit("0"); check.zero = table("summary")[0];
    check.invalid = [];
    for (const v of ["", "-1", "0.0000001", "NaN", "1e4"]) {
      setLimit(v); check.invalid.push(el("summary-csv").disabled && el("detail-csv").disabled);
    }
    setLimit("0.000002"); check.restored = table("summary")[0];
    el("start").value = "2026-01-15T13:59:00"; el("start").dispatchEvent(new Event("input"));
    el("filters").requestSubmit();
    check.outside = [el("message").textContent, el("summary-csv").disabled];
""")
        self.assertEqual(result["table"][0], ["-sala", "3", "1", "1", "1", "0,0000015 s", "0,000002 s"])
        self.assertEqual(result["zero"][2:4], ["0","2"])
        self.assertEqual(result["restored"][2:4], ["2","0"])
        self.assertTrue(all(result["invalid"]))
        self.assertTrue(result["outside"][1])
        self.assertIn("amostra", result["outside"][0])
        a, skew, empty = csv.DictReader(io.StringIO(result["summary"]), delimiter=";")
        self.assertEqual(a["sensor"], "'-sala")
        self.assertEqual((a["atraso_mediano_s"], a["maior_atraso_s"]), ("0,0000015","0,000002"))
        self.assertEqual(skew["atraso_mediano_s"], "")
        self.assertEqual((empty["recebidas"], empty["maior_atraso_s"]), ("0",""))
        rows = list(csv.DictReader(io.StringIO(result["details"]), delimiter=";"))
        self.assertEqual([(r["atraso_s"],r["situacao"]) for r in rows],
                         [("0,000001","No prazo"),("0,000002","Acima do limite"),("","Conferir relógio")])

    def test_recarregamentos_fora_de_ordem_e_exportacao_de_todas_as_paginas(self):
        rows = [{"id": f"{n:032x}", "measured_at": stamp(at(-5)), "received_at": stamp(at(1, n))}
                for n in range(65)]
        sample = {"start":stamp(at()), "end":stamp(at(2)), "rooms":[{"sensor":"b", "readings":rows}]}
        page = deliveries.page().replace('<script type="application/json"',
            '<script>window.calls=[];window.fetch=url=>new Promise(reply=>calls.push({url,reply}));</script>'
            '<script type="application/json"', 1)
        scenario = """
    const snap = SAMPLE;
    const fresh = loadPeriod(); setLimit("1");
    check.pending = el("summary-csv").disabled;
    check.pendingText = el("period").textContent;
    calls[1].reply({ok:true, json:async () => snap}); await fresh;
    calls[0].reply({ok:true, json:async () => ({...snap,rooms:[]})});
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    check.latest = table("summary"); check.firstPage = table("details").length;
    el("next").click(); el("next").click(); check.lastPage = table("details").length;
    check.csv = await save("detail-csv"); check.queries = calls.map(c => c.url);
""".replace("SAMPLE", json.dumps(sample))
        result = self.inspect(page, scenario)
        self.assertTrue(result["pending"])
        self.assertIn("Consultando", result["pendingText"])
        self.assertEqual(result["latest"][0][:5], ["b","65","0","65","0"])
        self.assertEqual((result["firstPage"],result["lastPage"]), (30,5))
        self.assertEqual(result["queries"], ["/api/entregas?periodo=1h"] * 2)
        exported = list(csv.DictReader(io.StringIO(result["csv"]), delimiter=";"))
        self.assertEqual([r["id"] for r in exported],[r["id"] for r in rows])

    def test_limite_colado_com_excesso_de_precisao_invalida_os_dois_csvs(self):
        sample = {"start":stamp(at()), "end":stamp(at(20)), "rooms":[{"sensor":"a", "readings":[
            {"id":"0"*32, "measured_at":stamp(at()), "received_at":stamp(at(10, 1))}]}]}
        result = self.inspect(deliveries.page(sample), """
    const field = el("limit");
    field.focus(); field.select();
    document.execCommand("insertText", false, "000000000000010,0000009");
    check.text = field.value;
    check.blocked = el("summary-csv").disabled && el("detail-csv").disabled;
    setLimit("10,000001"); check.valid = table("summary")[0];
""")
        self.assertEqual(result["text"], "000000000000010,0000009")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["valid"][2:4], ["1", "0"])

    def test_nomes_dos_csvs_identificam_o_limite_e_conservam_o_periodo(self):
        sample = {"start":stamp(at()), "end":stamp(at(20)), "rooms":[{"sensor":"a", "readings":[
            {"id":"0"*32, "measured_at":stamp(at()), "received_at":stamp(at(10))}]}]}
        result = self.inspect(deliveries.page(sample), """
    check.files = [];
    for (const limitText of ["10", "10,0", "10,000001"]) {
      setLimit(limitText);
      el("summary-csv").click(); const summary = check.filename;
      el("detail-csv").click(); check.files.push([summary, check.filename]);
    }
""")
        first, same, different = result["files"]
        self.assertEqual(first, same)
        for before, after in zip(first, different, strict=True):
            self.assertNotEqual(before, after)
            self.assertIn("limite-10s", before)
            self.assertIn("limite-10.000001s", after)
            self.assertIn("14-00-20.000000Z", after)

    def test_contagem_independente_por_sala_periodo_limite_e_csv(self):
        from decimal import Decimal
        from statistics import median
        from telemetria.storage import History, Reading

        # O último número é o atraso em microssegundos, independente da consulta testada.
        events = {"a": [(3, 0, 0), (2, 1, 1), (1, 1, 2), (4, 2, -1), (5, 3, 1000000)],
                  "b": [(1, 0, 999999), (2, 1, 1000000), (3, 1, 1000001), (4, 2, 10000001)],
                  "c-relogio": [(1, 1, -5000000)], "d-reserva": []}
        periods = [(0, 4), (0, 1), (1, 2), (2, 4), (3, 4)]
        limits = ["0", "0,000001", "1", "1.000001", "10"]
        cache = Path(__file__).resolve().parents[1] / ".cache" / "testes"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            moment = at()
            history = History(Path(folder) / "central.sqlite3", received_clock=lambda: moment)
            for room, samples in events.items():
                history.configure(room, high=31)
                for ident, sec, delay in samples:
                    moment = at(sec)
                    record = Reading(room, f"{ident:032x}", stamp(at(sec, -delay)), 23, 50)
                    history.record(record)
                    moment = at(8)
                    self.assertEqual(history.record(record), "repetida")
            snapshots = [deliveries.capture(history, stamp(at(start)), stamp(at(end))) for start, end in periods]

        live = deliveries.page().replace('<script type="application/json"', """
<script>
const snapshots = DATA;
window.fetch = async url => {
  const params = new URL(url, "http://localhost").searchParams;
  const data = params.has("inicio") ? snapshots.find(s => s.start === params.get("inicio") &&
    s.end === params.get("fim")) : snapshots[0];
  return {ok:!!data, json:async () => data};
};
</script><script type="application/json" """.rstrip().replace("DATA", json.dumps(snapshots)), 1)
        scenario = """
    const files = [], cases = [];
    function exportFile(id) {
      if (el(id).disabled) return null;
      el(id).click(); files.push(blob); return files.length - 1;
    }
    for (const [start,end] of PERIODS) {
      preset.value = "custom"; el("start").value=start.slice(0,23); el("end").value=end.slice(0,23);
      await loadPeriod();
      for (const text of LIMITS) {
        setLimit(text);
        const rooms = [], summary = table("summary");
        const summaryFile = exportFile("summary-csv");
        for (let i=0; i<summary.length; i++) {
          el("summary").rows[i].querySelector("button").click();
          rooms.push({rows:table("details"), file:exportFile("detail-csv")});
        }
        cases.push({summary,summaryFile,rooms});
      }
    }
    const separator = String.fromCharCode(30);
    const combined = new Blob(files.flatMap((file,i) => i ? [separator,file] : [file]));
    check.files = (await combined.text()).split(separator);
    check.cases = cases;
""".replace("PERIODS", json.dumps([[s["start"],s["end"]] for s in snapshots])).replace("LIMITS", json.dumps(limits))
        result = self.inspect(deliveries.page(snapshots[0]), scenario)
        self.assertEqual(self.inspect(live, scenario), result)
        def csv_rows(idx):
            return list(csv.DictReader(io.StringIO(result["files"][idx].lstrip("\ufeff")), delimiter=";"))

        def number(text):
            return Decimal(text.replace(",", ".")) if text else None

        for i, (start, end) in enumerate(periods):
            for j, threshold in enumerate(limits):
                value = number(threshold)
                case = result["cases"][i * len(limits) + j]
                summary = csv_rows(case["summaryFile"])
                self.assertEqual([r["sensor"] for r in summary], list(events))
                for (room, samples), row, cells, detail in zip(events.items(), summary, case["summary"],
                                                               case["rooms"], strict=True):
                    with self.subTest(start=start, end=end, limit=threshold, room=room):
                        active = sorted((r for r in samples if start <= r[1] < end), key=lambda r:(r[1],r[0]))
                        valid = [Decimal(d) / 1000000 for _, _, d in active if d >= 0]
                        within = sum(d <= value for d in valid)
                        counts = [len(active), within, len(valid) - within, len(active) - len(valid)]
                        self.assertEqual([int(row[k]) for k in ("recebidas","no_prazo","acima_do_limite","conferir_relogio")], counts)
                        self.assertEqual(cells[:5], [room, *map(str, counts)])
                        self.assertEqual(number(row["limite_s"]), value)
                        self.assertEqual((row["inicio_recebimento_utc"], row["fim_exclusivo_recebimento_utc"]),
                                         (stamp(at(start)), stamp(at(end))))
                        self.assertEqual(number(row["atraso_mediano_s"]), median(valid) if valid else None)
                        self.assertEqual(number(row["maior_atraso_s"]), max(valid, default=None))
                        if valid:
                            self.assertEqual(cells[5:], [row["atraso_mediano_s"]+" s",row["maior_atraso_s"]+" s"])
                        if not active:
                            self.assertIsNone(detail["file"])
                            self.assertEqual(detail["rows"], [["Sem medições recebidas."]])
                            continue
                        for (ident, sec, delay), entry, shown in zip(active, csv_rows(detail["file"]), detail["rows"], strict=True):
                            expected = Decimal(delay) / 1000000
                            status = "Conferir relógio" if delay < 0 else ("No prazo" if expected <= value else "Acima do limite")
                            self.assertEqual((entry["sensor"],entry["id"],entry["medido_em_utc"],entry["recebido_em_utc"]),
                                             (room,f"{ident:032x}",stamp(at(sec,-delay)),stamp(at(sec))))
                            self.assertEqual(number(entry["limite_s"]), value)
                            self.assertEqual(number(entry["atraso_s"]), None if delay < 0 else expected)
                            self.assertEqual((entry["situacao"],shown[3],shown[4]), (status,status,entry["id"]))

    def test_csv_em_blocos_conserva_limite_e_sala_apos_a_troca(self):
        import hashlib

        beginning, ending = stamp(at()), stamp(at(3))
        rooms = [{"sensor":"Sala", "readings":[{"id":f"{n:032x}", "measured_at":stamp(at(0, n)),
                  "received_at":stamp(at(1, n))} for n in range(1031)]},
                 {"sensor":"sala", "readings":[{"id":f"{0:032x}", "measured_at":beginning, "received_at":beginning}]}]
        result = self.inspect(deliveries.page({"start":beginning,"end":ending,"rooms":rooms}), """
    setLimit("1"); el("detail-csv").click();
    const original = blob; check.first = check.filename;
    el("summary").rows[1].querySelector("button").click(); el("detail-csv").click();
    check.second = check.filename;
    setLimit("0"); el("summary").rows[0].querySelector("button").click(); el("detail-csv").click();
    const latest = blob;
    async function digest(file) {
      const hash = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      return Array.from(new Uint8Array(hash), x => x.toString(16).padStart(2,"0")).join("");
    }
    [check.original,check.latest] = await Promise.all([digest(original), digest(latest)]);
""")
        header = ["sensor","inicio_recebimento_utc","fim_exclusivo_recebimento_utc","limite_s","id",
                  "medido_em_utc","recebido_em_utc","atraso_s","situacao"]
        for limit, status, digest in [("1","No prazo",result["original"]),("0","Acima do limite",result["latest"])]:
            buf = io.StringIO(newline="")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
            writer.writerow(header)
            for row in rooms[0]["readings"]:
                writer.writerow(["Sala",beginning,ending,limit,row["id"],row["measured_at"],row["received_at"],"1",status])
            self.assertEqual(digest, hashlib.sha256(buf.getvalue().encode("utf-8-sig")).hexdigest())
        self.assertNotEqual(result["first"].casefold(), result["second"].casefold())
