"use strict";
const example = JSON.parse(el("bootstrap").textContent);
const preset = el("preset");
const pageSize = 30;
let view = null, rooms = [], selected = "", pageNumber = 0;
let limit = 10000000n, generation = 0, pending = null;

function readLimit() {
  const text = el("limit").value.trim();
  if (!/^\d{1,15}(?:[.,]\d{1,6})?$/.test(text)) return null;
  const [whole, fraction = ""] = text.split(/[.,]/);
  return BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
}
function medianSeconds(sorted) {
  if (!sorted.length) return "";
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2) return seconds(sorted[mid]);
  // Duas leituras com diferença ímpar de microssegundos produzem meio microssegundo.
  const twiceMedian = sorted[mid - 1] + sorted[mid];
  const fraction = String(twiceMedian % 2000000n * 5n).padStart(7, "0").replace(/0+$/, "");
  return String(twiceMedian / 2000000n) + (fraction ? "," + fraction : "");
}
function prepare(room, start, end) {
  const readings = [], valid = [];
  for (const row of room.readings) {
    if (row.received_at < start || row.received_at >= end) continue;
    const delay = micros(row.received_at) - micros(row.measured_at);
    readings.push({...row, delay});
    if (delay >= 0n) valid.push(delay);
  }
  valid.sort((a,b) => a < b ? -1 : a > b ? 1 : 0);
  return {name:room.sensor, readings, valid, median:medianSeconds(valid),
    maximum:valid.length ? seconds(valid[valid.length - 1]) : ""};
}
function withinLimit(sorted) {
  let left = 0, right = sorted.length;
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    if (sorted[mid] <= limit) left = mid + 1; else right = mid;
  }
  return left;
}
function statusOf(delay) {
  if (delay < 0n) return "Conferir relógio";
  return delay > limit ? "Acima do limite" : "No prazo";
}
function counts(room) {
  const within = withinLimit(room.valid);
  return [room.readings.length, within, room.valid.length - within, room.readings.length - room.valid.length];
}
function currentRoom() { return rooms.find(r => r.name === selected); }
function showDetails() {
  const room = currentRoom();
  const rows = room && limit !== null ? room.readings : [];
  const body = el("details");
  body.replaceChildren();
  el("room-title").textContent = room ? "Entregas · " + room.name : "Entregas da sala";
  el("detail-csv").disabled = !rows.length;
  const pages = Math.ceil(rows.length / pageSize);
  el("prev").disabled = !pages || pageNumber === 0;
  el("next").disabled = pageNumber + 1 >= pages;
  el("pagination").textContent = pages ? "Página " + (pageNumber + 1) + " de " + pages : "";
  el("details-status").textContent = rows.length ? rows.length + " entregas no período de recebimento." : "";
  if (!rows.length) {
    const waiting = room ? "Sem medições recebidas." : "Aguardando o período aplicado.";
    emptyRow(body, 5, limit === null ? "Corrija o limite de atraso." : waiting);
  }
  for (const row of rows.slice(pageNumber * pageSize, (pageNumber + 1) * pageSize)) {
    addCells(body, [labelTime(row.received_at), labelTime(row.measured_at),
      row.delay < 0n ? "—" : seconds(row.delay) + " s", statusOf(row.delay), row.id]);
  }
}
function render() {
  limit = readLimit();
  el("warn").textContent = limit === null ? "Informe zero ou um valor positivo, com até 15 algarismos inteiros e seis casas decimais." : "";
  el("limit").setAttribute("aria-invalid", String(limit === null));
  const body = el("summary");
  body.replaceChildren();
  el("summary-csv").disabled = !rooms.length || limit === null;
  if (!rooms.length || limit === null) {
    const waiting = view ? "Nenhuma sala conhecida pela central." : "Aguardando o período aplicado.";
    emptyRow(body, 7, limit === null ? "Corrija o limite de atraso." : waiting);
  } else {
    for (const room of rooms) {
      const button = document.createElement("button");
      button.className = "reading"; button.textContent = room.name;
      button.setAttribute("aria-pressed", String(room.name === selected));
      button.onclick = () => {selected = room.name; pageNumber = 0; render(); el("room-title").focus();};
      const missing = room.readings.length ? "—" : "Sem medições recebidas";
      addCells(body, [button, ...counts(room), room.median ? room.median + " s" : missing,
                      room.maximum ? room.maximum + " s" : "—"]);
    }
  }
  showDetails();
}
function show(data, start, end) {
  view = data ? {start, end} : null;
  el("period").textContent = view ? labelTime(view.start) + " a " + labelTime(view.end) + " UTC" : "";
  rooms = data ? data.rooms.map(room => prepare(room, start, end)) : [];
  if (data && !currentRoom()) selected = rooms.length ? rooms[0].name : "";
  pageNumber = 0;
  render();
}
function editPeriod() {
  generation++;
  if (pending) pending.abort();
  preset.value = "custom";
  show(null);
  el("message").textContent = "Aplique o período selecionado.";
}
async function loadPeriod() {
  const order = ++generation;
  if (pending) pending.abort();
  el("message").textContent = "";
  show(null);
  try {
    const manual = preset.value === "custom";
    const params = new URLSearchParams();
    let start, end;
    if (manual) {
      start = utcInput(el("start").value); end = utcInput(el("end").value);
      if (start >= end) throw new Error("O fim deve ser posterior ao início.");
      params.set("inicio", start); params.set("fim", end);
    } else params.set("periodo", preset.value);
    let data;
    if (example) {
      data = example;
      if (!manual) {start = data.start; end = data.end;}
      if (start < data.start || end > data.end) throw new Error("Selecione um período dentro da amostra offline.");
    } else {
      pending = new AbortController();
      el("period").textContent = "Consultando as entregas…";
      const response = await fetch("/api/entregas?" + params, {signal:pending.signal, cache:"no-store"});
      if (!response.ok) throw new Error("Não foi possível consultar as entregas. Confira o período e a conexão.");
      data = await response.json();
      start = data.start; end = data.end;
    }
    if (order !== generation) return;
    if (!manual) showRange(start, end);
    show(data, start, end);
  } catch (err) {
    if (order === generation) {show(null); el("message").textContent = err.message;}
  }
}
function* summaryRecords() {
  for (const room of rooms) yield [safeSensor(room.name), view.start, view.end, seconds(limit),
    ...counts(room).map(String), room.median, room.maximum];
}
function* detailRecords(room) {
  for (const row of room.readings) yield [safeSensor(room.name), view.start, view.end, seconds(limit), row.id,
    row.measured_at, row.received_at, row.delay < 0n ? "" : seconds(row.delay), statusOf(row.delay)];
}
function exportPrefix(name) {
  return name + "_limite-" + seconds(limit).replace(",", ".") + "s";
}
const filterColumns = ["sensor","inicio_recebimento_utc","fim_exclusivo_recebimento_utc","limite_s"];
el("summary-csv").onclick = () => {
  if (view && rooms.length && limit !== null) downloadCsv([...filterColumns,"recebidas","no_prazo","acima_do_limite",
    "conferir_relogio","atraso_mediano_s","maior_atraso_s"], summaryRecords(), exportPrefix("atrasos-salas"), view);
};
el("detail-csv").onclick = () => {
  const room = currentRoom();
  if (!view || !room || !room.readings.length || limit === null) return;
  // O identificador admite maiúsculas. O Windows normalmente ignora caixa nos nomes de arquivos.
  const fileRoom = room.name.replace(/[A-Z]/g, "~$&");
  downloadCsv([...filterColumns,"id","medido_em_utc","recebido_em_utc","atraso_s","situacao"],
              detailRecords(room), exportPrefix("entregas-" + fileRoom), view);
};
el("limit").oninput = render;
el("filters").onsubmit = event => {event.preventDefault(); loadPeriod();};
for (const id of ["start","end"]) el(id).oninput = editPeriod;
preset.onchange = () => {if (preset.value === "custom") editPeriod(); else loadPeriod();};
el("prev").onclick = () => {pageNumber--; showDetails();};
el("next").onclick = () => {pageNumber++; showDetails();};
if (example) {
  preset.replaceChildren(new Option("Amostra completa", "sample"), new Option("Personalizado", "custom"));
  el("offline").hidden = false;
  el("sample-range").hidden = false;
  el("sample-range").textContent = "Amostra de " + labelTime(example.start) + " a " + labelTime(example.end) + " UTC.";
} else el("home").hidden = false;
const incidentLink = new URLSearchParams(location.hash.slice(1));
if (incidentLink.has("sala") && incidentLink.has("inicio") && incidentLink.has("fim")) {
  selected = incidentLink.get("sala");
  preset.value = "custom";
  showRange(incidentLink.get("inicio"), incidentLink.get("fim"));
}
loadPeriod();
