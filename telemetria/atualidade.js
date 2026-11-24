"use strict";
const boot = JSON.parse(el("bootstrap").textContent);
const sample = boot.example;
const outcomes = {atualizada:"Medição voltou a ser atual",sem_observacao:"Observação interrompida"};
let step = 0, report = null, selected = null, generation = 0, loading = false;
const time = value => value ? labelTime(value) : "—";
const limit = value => String(value).replace(".",",") + " s";

function renderDetail(event) {
  el("detail").hidden = !event;
  if (!event) return;
  el("event-title").textContent = event.sensor + " · ocorrência " + event.id;
  const trace = el("trace");
  trace.replaceChildren();
  function entry(text) {
    const li = document.createElement("li"); li.textContent = text; trace.append(li);
  }
  entry(time(event.detected) + " UTC · Desatualização detectada. " +
        (event.baseline ? "Última medição válida até então em " + time(event.baseline) + " UTC." : "Ainda sem medição válida.") +
        " Limite de " + limit(event.threshold) + ".");
  if (event.outcome === "atualizada") {
    entry(time(event.ended) + " UTC · Recuperação confirmada pela leitura " + event.recovery_id +
          ". Medida em " + time(event.recovery_measured) + " UTC e recebida em " + time(event.recovery_received) + " UTC.");
  } else {
    entry(time(event.until) + " UTC · " + (event.outcome ? "Último ponto observado antes da interrupção da central." :
          "Última observação confirmada. A ocorrência está aberta."));
  }
  const list = el("maintenance-list");
  list.replaceChildren();
  for (const w of event.maintenance) {
    const li = document.createElement("li");
    li.textContent = time(w.start) + " a " + time(w.end) + " UTC · " + w.reason;
    list.append(li);
  }
  const inside = maintenanceOverlap([[event.detected,event.until]],event.maintenance,event.detected,event.until);
  const outside = micros(event.until) - micros(event.detected) - inside;
  el("maintenance-total").textContent = seconds(inside) + " s em manutenção · " + seconds(outside) + " s fora da manutenção.";
}
function draw(data) {
  report = data;
  const room = el("room"), name = room.value;
  if (JSON.stringify(Array.from(room.options,o=>o.value).slice(1)) !== JSON.stringify(data.rooms)) {
    room.replaceChildren(new Option("Todas as salas",""),...data.rooms.map(s=>new Option(s,s)));
    if (data.rooms.includes(name)) room.value = name;
  }
  const rows = data.current.filter(r=>!room.value || r.sensor === room.value);
  const body = el("current"); body.replaceChildren();
  for (const r of rows) {
    const status = document.createElement("span");
    status.textContent = boot.states[r.freshness];
    status.className = r.freshness === "desatualizada" ? "stale" : r.freshness === "atualizada" ? "current" : "";
    addCells(body,[r.sensor,boot.communication[r.state],time(r.last_contact),status,time(r.measured),
                   limit(r.threshold),r.maintenance ? r.maintenance.reason + " · até " + time(r.maintenance.end) : "—"]);
  }
  if (!rows.length) emptyRow(body,7,"Aguardando sensores.");
  el("asof").textContent = data.seen ? "Última verificação de presença · " + time(data.seen) + " UTC" : "Aguardando observação da central.";
  const events = data.events.filter(e=>!room.value || e.sensor === room.value);
  el("scope").textContent = room.value ? "Histórico completo da sala selecionada." : "Ocorrências abertas e as 50 últimas encerradas.";
  const chosen = events.find(e=>e.id===selected) || events[0]; selected = chosen?.id;
  const table = el("events"); table.replaceChildren();
  for (const e of events) {
    const link = document.createElement("button"); link.className="reading"; link.textContent=e.sensor;
    link.setAttribute("aria-pressed",String(e.id===selected));
    link.onclick=()=>{selected=e.id;draw(report);el("event-title").focus();};
    addCells(table,[link,time(e.detected),outcomes[e.outcome] || "Em aberto",time(e.until),limit(e.threshold)]);
  }
  if (!events.length) emptyRow(table,5,"Nenhuma ocorrência neste histórico.");
  renderDetail(chosen);
}
async function fetchRoom(room) {
  const args = new URLSearchParams();
  if (room) args.set("sala",room);
  const response = await fetch("/api/atualidade?" + args,{cache:"no-store",signal:AbortSignal.timeout(30000)});
  if (!response.ok) throw Error();
  return response.json();
}
async function refresh() {
  const turn = ++generation;
  loading = true;
  try {
    if (sample) {
      el("step-description").textContent=sample[step].description;
      el("advance").disabled=step===sample.length-1;
      draw(sample[step].report);
    } else {
      const room = el("room").value;
      let data = await fetchRoom(room);
      if (turn !== generation) return;
      if (room && !data.rooms.includes(room)) {
        data = await fetchRoom("");
        if (turn !== generation) return;
        el("room").value = "";
      }
      draw(data);
    }
    if (turn === generation) el("message").textContent="";
  } catch {
    if (turn === generation) el("message").textContent="Falha ao consultar a central. Use Atualizar para tentar novamente.";
  } finally {
    if (turn === generation) loading = false;
  }
}
el("room").onchange=()=>{
  selected=null;
  if (!sample) {
    el("current").replaceChildren(); el("events").replaceChildren();
    emptyRow(el("current"),7,"Consultando a sala selecionada…");
    emptyRow(el("events"),5,"Consultando o histórico…");
    el("scope").textContent=el("asof").textContent="";
    renderDetail(null);
  }
  refresh();
};
el("refresh").onclick=refresh;
if (sample) {
  el("demo").hidden=el("offline").hidden=false;el("refresh").hidden=true;
  el("advance").onclick=()=>{if (step<sample.length-1) step++;refresh();};
  el("restart").onclick=()=>{step=0;selected=null;el("room").value="";refresh();};
} else {
  el("home").hidden=el("history").hidden=false;
  setInterval(()=>{if (!document.hidden && !loading) refresh();},5000);
}
refresh();
