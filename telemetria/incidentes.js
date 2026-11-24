"use strict";
const sample = JSON.parse(el("bootstrap").textContent);
const labels = {retomada:"Comunicação retomada", sem_observacao:"Observação interrompida"};
const drafts = new Map(), pendingNotes = new Map();

// Mantém apenas os envios sem confirmação na sessão desta aba.
function pendingFor(token) {
  if (!pendingNotes.has(token) && !sample) {
    try {
      const saved = JSON.parse(sessionStorage.getItem("telemetria.nota." + token));
      if (saved && typeof saved.text === "string" && /^[0-9a-f]{32}$/.test(saved.key)) pendingNotes.set(token, saved);
    } catch { /* O navegador pode permitir apenas memória. */ }
  }
  return pendingNotes.get(token);
}
function remember(token, note) {
  if (note) pendingNotes.set(token, note);
  else pendingNotes.delete(token);
  if (!sample) {
    try {
      if (note) sessionStorage.setItem("telemetria.nota." + token, JSON.stringify(note));
      else sessionStorage.removeItem("telemetria.nota." + token);
    } catch { /* A tentativa permanece disponível em memória. */ }
  }
}
let selected = "", detail = null, revision = 0, fetching = false, saving = false;
let demoEvent, recovered = false, demoLink = "";

function resetDemo() {
  recovered = false;
  demoEvent = {...sample.incident, ended:null, outcome:null, seen_at:null, actions:[],
               until:demoTime(5)};
  drafts.clear(); pendingNotes.clear();
  el("note").value = ""; el("feedback").textContent = "";
  el("advance").disabled = false;
}
function demoTime(secondsAfter) {
  return utcInput(new Date(wholeMillis(sample.incident.detected) + secondsAfter * 1000).toISOString().slice(0,-1));
}
function demoReport() {
  const inRoom = !el("room").value || el("room").value === demoEvent.sensor;
  const visible = inRoom && !(el("open").checked && demoEvent.outcome) && !(el("unseen").checked && demoEvent.seen_at);
  return {rooms:sample.rooms, items:visible ? [demoEvent] : [],
          detail:inRoom && (visible || selected === demoEvent.id) ? demoEvent : null};
}
function receiptLink(event) {
  const start = new Date(wholeMillis(event.detected)).toISOString().slice(0,-1);
  const end = new Date(wholeMillis(event.ended) + 60000).toISOString().slice(0,-1);
  const fragment = new URLSearchParams({sala:event.sensor, inicio:utcInput(start), fim:utcInput(end)});
  return (sample ? demoLink : "/entregas") + "#" + fragment;
}
function controls() {
  el("seen").disabled = saving || !detail || !!detail.seen_at;
  el("seen").textContent = detail && detail.seen_at ? "Marcado como visto" : "Marcar como visto";
  el("save").disabled = saving || !detail || !el("note").value.trim();
}
function drawDetail(event) {
  const changed = !detail || !event || detail.token !== event.token;
  detail = event;
  selected = event ? event.id : "";
  el("detail-panel").hidden = !event;
  if (changed) {
    if (event && !drafts.has(event.token)) {
      const pending = pendingFor(event.token);
      if (pending) drafts.set(event.token, {text:pending.text});
    }
    el("note").value = event ? drafts.get(event.token)?.text || "" : "";
  }
  controls();
  if (!event) return;
  el("title").textContent = event.sensor + " · ocorrência " + event.id;
  const duration = seconds(micros(event.until) - micros(event.detected));
  const response = event.seen_at ? " · Vista após " + seconds(micros(event.seen_at) - micros(event.detected)) + " s" : " · Ainda não vista";
  el("facts").textContent = (labels[event.outcome] || "Em aberto") + " · " + duration + " s de interrupção observada" + response;
  const link = el("deliveries");
  link.hidden = event.outcome !== "retomada";
  if (!link.hidden) {
    link.href = receiptLink(event); link.target = "_blank"; link.rel = "noopener";
  } else link.removeAttribute("href");
  const entries = [{at:event.detected, text:"Interrupção detectada"}];
  for (const action of event.actions) entries.push({at:action.at, text:action.body === null ? "Marcado como visto" : "Nota · " + action.body});
  let timing = "";
  if (event.ended) {
    const closure = {at:event.ended, text:labels[event.outcome]};
    if (event.outcome === "retomada" && Number.isInteger(event.recovery_position)) {
      entries.splice(1 + event.recovery_position, 0, closure);
      if (entries.some((entry,i) => i && entry.at < entries[i-1].at)) {
        timing = "O relógio recuou. A sequência segue a ordem de gravação e conserva os horários registrados.";
      }
    } else {
      entries.splice(1, 0, closure);
      entries.sort((a,b) => a.at < b.at ? -1 : a.at > b.at ? 1 : 0);
      if (event.outcome === "retomada" && event.actions.length) {
        timing = "Histórico anterior à versão 0.7.1. A sequência reflete os horários disponíveis. A posição original da retomada é desconhecida.";
      }
    }
  }
  el("timing").textContent = timing;
  el("timing").hidden = !timing;
  const windows = event.maintenance || [];
  el("planned-context").hidden = !windows.length;
  const list = el("planned-windows");
  list.replaceChildren();
  for (const w of windows) {
    const li = document.createElement("li");
    li.textContent = labelTime(w.start) + " a " + labelTime(w.end) + " UTC · " + w.reason;
    list.append(li);
  }
  const planned = maintenanceOverlap([[event.detected,event.until]], windows, event.detected,event.until);
  el("planned-summary").textContent = seconds(planned) + " s de interrupção durante a manutenção · " +
    seconds(micros(event.until) - micros(event.detected) - planned) + " s fora da manutenção.";
  el("stream").replaceChildren();
  for (const entry of entries) {
    const item = document.createElement("li"), time = document.createElement("time");
    time.dateTime = entry.at; time.textContent = labelTime(entry.at) + " UTC";
    item.append(time, document.createTextNode(entry.text));
    el("stream").append(item);
  }
}
function draw(report) {
  const selector = el("room"), room = selector.value;
  if (JSON.stringify(Array.from(selector.options, o => o.value).slice(1)) !== JSON.stringify(report.rooms)) {
    selector.replaceChildren(new Option("Todas as salas",""));
    for (const name of report.rooms) selector.add(new Option(name,name));
    selector.value = report.rooms.includes(room) ? room : "";
  }
  const body = el("events");
  body.replaceChildren();
  if (!report.items.length) emptyRow(body,4,"Nenhuma ocorrência para estes filtros.");
  for (const event of report.items) {
    const button = document.createElement("button");
    button.className = "reading"; button.textContent = event.sensor;
    button.setAttribute("aria-pressed", String(event.id === report.detail?.id));
    button.onclick = async () => {
      drawDetail(null); selected = event.id;
      await refresh(); if (detail?.id === event.id) el("title").focus();
    };
    addCells(body,[button,labelTime(event.detected),labels[event.outcome] || "Em aberto",event.seen_at ? "Vista" : "Ainda não vista"]);
  }
  drawDetail(report.detail);
  const pinned = detail && !report.items.some(e => e.id === detail.id);
  el("list-status").textContent = report.items.length + " ocorrência(s) na lista." +
    (pinned ? " O detalhe selecionado continua aberto fora do filtro." : "");
}
async function refresh() {
  const turn = ++revision;
  fetching = true;
  const args = new URLSearchParams();
  if (el("room").value) args.set("sala",el("room").value);
  if (el("open").checked) args.set("abertos","1");
  if (el("unseen").checked) args.set("pendentes","1");
  if (selected) args.set("id",selected);
  try {
    let data;
    if (sample) data = demoReport();
    else {
      const reply = await fetch("/api/incidentes?" + args,{cache:"no-store"});
      if (!reply.ok) throw new Error();
      data = await reply.json();
    }
    if (turn === revision) {el("message").textContent = ""; draw(data);}
  } catch {
    if (turn === revision) el("message").textContent = "Falha ao atualizar a consulta. Tente Atualizar.";
  } finally {if (turn === revision) fetching = false;}
}
function demoRecord(action, text) {
  const at = recovered ? demoTime(18) : demoTime(5);
  if (action === "visto") {
    if (demoEvent.seen_at) return;
    demoEvent.seen_at = at;
  }
  demoEvent.actions.push({at, body:action === "visto" ? null : text});
}
async function submitAction(action) {
  if (!detail || saving) return;
  const id = detail.id, token = detail.token, draft = drafts.get(token);
  if (action === "nota" && !draft?.text.trim()) return;
  saving = true; controls(); el("feedback").textContent = "";
  try {
    if (sample) demoRecord(action, draft?.text.trim());
    else {
      let payload = {};
      if (action === "nota") {
        const text = draft.text.trim(), pending = pendingFor(token);
        const note = pending?.text === text ? pending : {text, key:crypto.randomUUID().replaceAll("-","")};
        remember(token, note);
        payload = {texto:text, chave:note.key};
      }
      const res = await fetch("/api/incidentes/" + id + "/" + action, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({...payload, ocorrencia:token})});
      if (res.status === 409 || res.status === 404) {
        el("feedback").textContent = "O envio não corresponde aos dados atuais. Confira a ocorrência antes de enviar novamente.";
        await refresh();
        return;
      }
      if (!res.ok) throw new Error();
      if (action === "nota") remember(token, null);
    }
    if (action === "nota" && drafts.get(token) === draft) {
      drafts.delete(token); if (detail?.token === token) el("note").value = "";
    }
    el("feedback").textContent = "Ocorrência " + id + (action === "nota" ? " · Nota registrada." : " · Marcada como vista.");
    await refresh();
  } catch {
    el("feedback").textContent = "Não foi possível confirmar a ação. Tente novamente.";
  } finally {saving = false; controls();}
}
el("seen").onclick = () => submitAction("visto");
el("note-form").onsubmit = e => {e.preventDefault(); submitAction("nota");};
el("note").oninput = () => {
  if (detail) drafts.set(detail.token,{text:el("note").value});
  controls();
};
for (const id of ["room","open","unseen"]) el(id).onchange = () => {drawDetail(null); refresh();};
el("refresh").onclick = refresh;
if (sample) {
  el("demo-box").hidden = false; el("offline").hidden = false; el("refresh").hidden = true;
  demoLink = URL.createObjectURL(new Blob([sample.deliveries_html], {type:"text/html;charset=utf-8"}));
  resetDemo();
  el("advance").onclick = () => {
    recovered = true; demoEvent.recovery_position = demoEvent.actions.length; demoEvent.outcome = "retomada"; demoEvent.ended = sample.incident.ended;
    demoEvent.until = demoEvent.ended; el("advance").disabled = true; refresh();
  };
  el("restart").onclick = () => {
    resetDemo(); el("room").value = ""; el("open").checked = false; el("unseen").checked = false;
    drawDetail(null); refresh();
  };
} else {
  el("home").hidden = false;
  setInterval(() => {if (!document.hidden && !saving && !fetching) refresh();}, 5000);
  window.addEventListener("focus", () => {if (!saving && !fetching) refresh();});
}
refresh();
