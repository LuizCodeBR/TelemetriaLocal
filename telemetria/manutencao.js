"use strict";
const sample = JSON.parse(el("bootstrap").textContent);
let agenda, busy = false, pending = null, generation = 0;

// Uma chave representa um envio até a central confirmar o resultado.
let unresolved = new Map();
function signature(data) {
  return JSON.stringify([data.origin,data.sensor,data.start,data.end,data.reason]);
}
function storeAttempts(origin) {
  if (sample) return;
  try {
    const name = "telemetria.manutencao." + origin;
    if (unresolved.size) sessionStorage.setItem(name,JSON.stringify([...unresolved.values()]));
    else sessionStorage.removeItem(name);
  } catch { /* Sem armazenamento de sessão, os reenvios funcionam em memória. */ }
}
function loadAttempts(origin) {
  unresolved = new Map();
  try {
    const entries = JSON.parse(sessionStorage.getItem("telemetria.manutencao." + origin) || "[]");
    for (const entry of entries) {
      if (entry?.origin === origin && /^[0-9a-f]{32}$/.test(entry.key) &&
          [entry.sensor,entry.start,entry.end,entry.reason].every(s => typeof s === "string"))
        unresolved.set(signature(entry),entry);
    }
  } catch { /* A primeira consulta também funciona sem dados salvos. */ }
}
function renderAgenda() {
  const selected = el("room").value;
  if (JSON.stringify(Array.from(el("room").options,o => o.value)) !== JSON.stringify(agenda.rooms)) {
    el("room").replaceChildren(...agenda.rooms.map(name => new Option(name,name)));
    if (agenda.rooms.includes(selected)) el("room").value = selected;
  }
  el("clock").textContent = "Relógio da central · " + labelTime(agenda.now) + " UTC";
  const table = el("agenda");
  table.replaceChildren();
  for (const w of [...agenda.windows].sort((a,b) => a.start.localeCompare(b.start) || a.sensor.localeCompare(b.sensor))) {
    const state = w.canceled_at ? "Cancelada" : agenda.now < w.start ? "Agendada" : agenda.now < w.end ? "Em manutenção" : "Encerrada";
    let action = "—";
    if (w.canceled_at) action = labelTime(w.canceled_at) + " UTC";
    if (state === "Agendada") {
      action = document.createElement("button");
      action.textContent = "Cancelar"; action.disabled = busy;
      action.setAttribute("aria-label","Cancelar manutenção de " + w.sensor + " às " + labelTime(w.start));
      action.onclick = () => submit({key:w.key, origin:agenda.origin}, true);
    }
    addCells(table,[w.sensor,labelTime(w.start),labelTime(w.end),w.reason,state,action]);
  }
  if (!agenda.windows.length) emptyRow(table,6,"Nenhuma manutenção agendada.");
  el("save").disabled = busy || !agenda.rooms.length;
  if (!agenda.rooms.length) el("clock").textContent += " · Aguardando o primeiro sensor.";
}
async function refresh() {
  const turn = ++generation;
  try {
    if (!sample) {
      const response = await fetch("/api/manutencao",{cache:"no-store"});
      if (!response.ok) throw Error("Falha ao consultar a agenda.");
      const data = await response.json();
      if (turn !== generation) return;
      const first = !agenda;
      if (first || agenda.origin !== data.origin) loadAttempts(data.origin);
      if (!first && agenda.origin !== data.origin) {
        el("schedule").reset(); pending = null;
        el("feedback").textContent = "A pasta de dados mudou. Confira a sala e preencha uma nova janela.";
      }
      agenda = data;
      renderAgenda();
      if (first && unresolved.size) {
        const saved = [...unresolved.values()].at(-1);
        if (agenda.rooms.includes(saved.sensor)) {
          el("room").value = saved.sensor; el("start").value = saved.start.slice(0,23);
          el("end").value = saved.end.slice(0,23); el("reason").value = saved.reason;
          el("feedback").textContent = "Envio sem confirmação. Confira a janela e repita o envio para consultar o resultado.";
        }
      }
    } else renderAgenda();
    el("message").textContent = "";
  } catch (err) {if (turn === generation) el("message").textContent = err.message;}
}
function demoUpdate(data, cancel) {
  if (cancel) {
    const w = agenda.windows.find(row => row.key === data.key);
    if (w.start <= agenda.now) throw Error("A janela já começou.");
    w.canceled_at = agenda.now;
  } else {
    if (data.start <= agenda.now) throw Error("Escolha um início futuro no relógio da central.");
    if (agenda.windows.some(w=>!w.canceled_at && w.sensor===data.sensor && w.start<data.end && w.end>data.start))
      throw Error("A sala já tem uma janela nesse horário.");
    agenda.windows.push({...data, created_at:agenda.now, canceled_at:null});
  }
}
async function submit(data, cancel) {
  if (busy) return;
  busy = true; ++generation; renderAgenda(); el("feedback").textContent = "";
  let canceledAt;
  try {
    if (sample) demoUpdate(data, cancel);
    else {
      if (!cancel) {
        unresolved.set(signature(data),data);
        storeAttempts(data.origin);
      }
      const response = await fetch("/api/manutencao" + (cancel ? "/cancelar" : ""), {
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
      if (!response.ok) {
        const conflict = response.status === 409 ? await response.json() : null;
        if (!cancel && (response.status === 400 || conflict?.code === "conflito_agenda")) {
          unresolved.delete(signature(data)); storeAttempts(data.origin);
        }
        if (conflict) throw Error(conflict.error);
        if (response.status === 400) throw Error("Confira a sala, o motivo e os horários informados.");
        throw Error("Falha ao confirmar a operação. Você pode repetir o envio.");
      }
      const receipt = await response.json();
      canceledAt = receipt.canceled_at;
      if (!cancel) {unresolved.delete(signature(data)); storeAttempts(data.origin);}
    }
    if (!cancel) pending = null;
    el("feedback").textContent = canceledAt ? "Este agendamento já foi cancelado em " + labelTime(canceledAt) + " UTC." :
      cancel ? "Cancelamento registrado." : "Agendamento confirmado.";
  } catch (err) {el("feedback").textContent = err.message;}
  finally {busy = false; await refresh();}
}
el("schedule").onsubmit = event => {
  event.preventDefault();
  if (busy || !agenda) return;
  try {
    const values = {origin:agenda.origin, sensor:el("room").value,start:utcInput(el("start").value),
                    end:utcInput(el("end").value),reason:el("reason").value.trim()};
    if (!values.reason || /[\u0000-\u001f\u007f-\u009f]/u.test(values.reason)) throw Error("Informe um motivo em uma linha.");
    if (values.start >= values.end) throw Error("O fim deve ser posterior ao início.");
    const id = signature(values);
    if (pending?.signature !== id) pending = {signature:id,key:unresolved.get(id)?.key || crypto.randomUUID().replaceAll("-","")};
    submit({...values,key:pending.key}, false);
  } catch (err) {el("feedback").textContent = err.message;}
};
function showReport() {
  const example = structuredClone(sample.example);
  for (const room of example.availability.rooms) {
    room.maintenance = agenda.windows.filter(w => !w.canceled_at && w.sensor === room.sensor)
                                     .sort((a,b) => a.start.localeCompare(b.start));
  }
  const payload = JSON.stringify({sensors:agenda.rooms,example}).replaceAll("<","\\u003c");
  el("report").srcdoc = sample.reportPrefix + payload + sample.reportSuffix;
  el("report-panel").hidden = false;
}
function resetDemo() {
  generation++; pending = null; agenda = structuredClone(sample.agenda);
  el("report-panel").hidden = true; el("report").removeAttribute("srcdoc"); el("advance").disabled = false;
  el("schedule").reset(); el("start").value = sample.start.slice(0,19); el("end").value = sample.end.slice(0,19);
  el("reason").value = "Troca de bateria"; el("feedback").textContent = ""; refresh();
}
el("refresh").onclick = () => {if (!busy) refresh();};
if (sample) {
  el("demo").hidden = el("offline").hidden = false;
  el("advance").onclick = () => {agenda.now = sample.example.end; el("advance").disabled = true; renderAgenda(); showReport();};
  el("restart").onclick = resetDemo; resetDemo();
} else {
  el("save").disabled = true;
  el("home").hidden = el("history").hidden = false; refresh();
  setInterval(() => {if (!busy && !document.hidden) refresh();},15000);
}
