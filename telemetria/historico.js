"use strict";
const boot = JSON.parse(document.getElementById("bootstrap").textContent);
const form = el("filters");
const preset = el("preset");
const sensor = el("sensor");
const chart = el("chart");
const pageSize = 30;
let view = null, pageIndex = 0, generation = 0, pending = null;
let plotted = null;

function filterExample(name, start, end) {
  const source = boot.example.data[name];
  const from = start < boot.example.start ? boot.example.start : start;
  const to = end > boot.example.end ? boot.example.end : end;
  const result = {sensor:name, start, end, coverage:{start:from,end:to}, availability:boot.example.availability, rows:[], events:[], gaps:[]};
  if (from >= to) return result;
  result.rows = source.rows.filter(r => from <= r.measured_at && r.measured_at < to);
  result.events = source.events.filter(e => e.start < to &&
    (e.until > from || e.start >= from || (e.until === from && e.outcome === "retomada")));
  result.freshness = (source.freshness || []).filter(e => e.start < to &&
    (e.until > from || e.start >= from || (e.until === from && e.outcome === "atualizada")));
  result.gaps = source.gaps.filter(g => g.start < to && g.until > from);
  return result;
}
function numeric(n) { return String(n).replace(".", ","); }
function detail(row, group = null, index = 0) {
  const next = el("next-point");
  next.hidden = !group || group.length < 2;
  next.onclick = next.hidden ? null : () => {
    const i = (index + 1) % group.length;
    detail(group[i], group, i);
  };
  const position = next.hidden ? "" : " · Medição " + (index+1) + " de " + group.length + " neste ponto";
  el("detail").hidden = false;
  el("detail").textContent = [view.sensor + position, "Medição " + labelTime(row.measured_at) + " UTC",
    numeric(row.temperature) + " °C", numeric(row.humidity) + "% de umidade",
    "Recebimento " + labelTime(row.received_at) + " UTC", "ID " + row.id].join(" · ");
}
function table() {
  const body = el("readings");
  body.replaceChildren();
  const rows = view ? view.rows : [];
  const lastPage = Math.max(0, Math.ceil(rows.length / pageSize) - 1);
  pageIndex = Math.min(pageIndex, lastPage);
  const first = pageIndex * pageSize;
  for (const row of rows.slice(first, first + pageSize)) {
    const button = document.createElement("button");
    button.className = "reading";
    button.textContent = labelTime(row.measured_at);
    button.onclick = () => detail(row);
    addCells(body, [button, numeric(row.temperature), numeric(row.humidity), labelTime(row.received_at)]);
  }
  if (!rows.length) emptyRow(body, 4, "Nenhuma medição neste recorte.");
  el("prev").disabled = pageIndex === 0;
  el("next").disabled = pageIndex >= lastPage;
  el("pagination").textContent = rows.length ? (first + 1) + "–" + Math.min(first + pageSize, rows.length) + " de " + rows.length : "0 medições";
}
function selectedWindows() {
  if (!view) return [];
  const bounds = view.coverage || view;
  const windows = view.availability?.rooms.find(room => room.sensor === view.sensor)?.maintenance || [];
  return windows.filter(w => bounds.start < bounds.end && w.start < bounds.end && w.end > bounds.start);
}
function eventTable() {
  const body = el("events");
  body.replaceChildren();
  const labels = {retomada:"Interrupção com retomada", sem_observacao:"Interrupção até a perda de observação"};
  const entries = view ? view.events.map(e => ({
    ...e, label: e.until >= (view.coverage || view).end ? "Interrupção no período" : labels[e.outcome] || "Interrupção em aberto"
  })).concat(view.gaps.map(g => ({...g, label: g.ongoing ? "Central sem observação (em andamento)" : "Central sem observação"}))) : [];
  for (const e of view?.freshness || []) {
    let label = "Medição desatualizada no período";
    if (e.until < (view.coverage || view).end) {
      if (e.outcome === "atualizada") label = "Medição desatualizada com recuperação";
      if (e.outcome === "sem_observacao") label = "Medição desatualizada até o fim da observação";
    }
    entries.push({...e,label});
  }
  for (const w of selectedWindows()) entries.push({label:"Manutenção · " + w.reason, start:w.start, until:w.end});
  entries.sort((a,b) => a.start.localeCompare(b.start));
  for (const e of entries) addCells(body, [e.label, labelTime(e.start), labelTime(e.until)]);
  if (!entries.length) emptyRow(body, 3, "Nenhum evento registrado neste recorte.");
}
function draw() {
  const width = chart.clientWidth, height = chart.clientHeight;
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  chart.width = Math.round(width * ratio);
  chart.height = Math.round(height * ratio);
  const ctx = chart.getContext("2d");
  ctx.scale(ratio,ratio);
  plotted = null;
  if (!view) return;
  const originMs = wholeMillis(view.start);
  const originUs = Number(view.start.slice(23,26));
  const elapsed = iso => (wholeMillis(iso) - originMs) + (Number(iso.slice(23,26)) - originUs) / 1000;
  const span = elapsed(view.end);
  const left = 48, right = width - 12, top = 12, bottom = height - 33;
  const position = fraction => left + fraction * (right - left);
  const x = iso => position(elapsed(iso) / span);
  const bounds = view.coverage || view;
  const bandLeft = x(bounds.start), bandRight = x(bounds.end);
  let low = Infinity, high = -Infinity;
  for (const row of view.rows) {low = Math.min(low, row.temperature); high = Math.max(high, row.temperature);}
  if (!view.rows.length) {low = 0; high = 1;}
  const pad = Math.max(.25, (high - low) * .15);
  low -= pad; high += pad;
  const y = temp => bottom - (temp - low) / (high - low) * (bottom - top);
  ctx.save(); ctx.beginPath(); ctx.rect(left, top, right-left, bottom-top); ctx.clip();
  for (const gap of view.gaps) {
    const a = Math.max(bandLeft,x(gap.start)), b = Math.min(bandRight,x(gap.until));
    ctx.fillStyle = "#e1e5e7"; ctx.fillRect(a,top,b-a,bottom-top);
    ctx.save();ctx.beginPath();ctx.rect(a,top,b-a,bottom-top);ctx.clip();
    ctx.strokeStyle = "#b5c1c5";ctx.beginPath();
    for (let p = a - height; p < b; p += 10) {ctx.moveTo(p,bottom);ctx.lineTo(p+height,top);}
    ctx.stroke();ctx.restore();
  }
  for (const event of view.events) {
    const a = x(event.start), b = x(event.until);
    ctx.fillStyle = "#f5ddca";
    const bandStart = Math.max(bandLeft,a);
    ctx.fillRect(bandStart,top,Math.max(0,Math.min(bandRight,b)-bandStart),bottom-top);
    if (event.start >= bounds.start) {
      ctx.strokeStyle = "#a94a10";ctx.beginPath();ctx.moveTo(a,top);ctx.lineTo(a,bottom);ctx.stroke();
    }
    if (event.outcome === "retomada" && event.until >= bounds.start && event.until < bounds.end) {
      ctx.setLineDash([4,3]);ctx.strokeStyle = "#13644b";ctx.lineWidth = 2;
      ctx.beginPath();ctx.moveTo(Math.max(left+1,b),top);ctx.lineTo(Math.max(left+1,b),bottom);ctx.stroke();
      ctx.setLineDash([]);ctx.lineWidth = 1;
    }
  }
  for (const w of selectedWindows()) {
    const a = Math.max(bandLeft, x(w.start)), b = Math.min(bandRight, x(w.end));
    ctx.fillStyle = "#68529a";
    ctx.fillRect(a,top,Math.max(0,b-a),5);
  }
  for (const event of view.freshness || []) {
    const a = Math.max(bandLeft,x(event.start)), b = Math.min(bandRight,x(event.until));
    ctx.fillStyle = "#9e4422";
    ctx.fillRect(a,bottom-5,Math.max(1,b-a),5);
  }
  ctx.restore();ctx.font = "12px system-ui";
  for (let i=0;i<4;i++) {
    const v = low + (high-low)*i/3, pos = y(v);
    ctx.strokeStyle = "#abbbb375";ctx.beginPath();ctx.moveTo(left,pos);ctx.lineTo(right,pos);ctx.stroke();
    if (view.rows.length) {
      ctx.fillStyle = "#49635a";ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1).replace(".",","),left-7,pos+4);
    }
  }
  ctx.fillStyle = "#00674f";
  for (const row of view.rows) {
    ctx.beginPath();ctx.arc(x(row.measured_at),y(row.temperature),view.rows.length > 500 ? 1.5 : 3,0,2*Math.PI);ctx.fill();
  }
  if (!view.rows.length) {ctx.fillStyle="#49635a";ctx.textAlign="left";ctx.fillText("Sem medições",left+8,top+20);}
  const divisions = width >= 680 ? 4 : 2;
  function tick(fraction) {
    const deltaUs = originUs + Math.round(span * 1000 * fraction);
    const iso = new Date(originMs + Math.floor(deltaUs/1000)).toISOString();
    if (span >= 86400000) return iso.slice(8,10)+"/"+iso.slice(5,7)+" "+iso.slice(11,16);
    if (span / divisions >= 1000) return iso.slice(11,19);
    const subMs = String(deltaUs % 1000).padStart(3,"0");
    return iso.slice(11,23) + (span / divisions < 1 ? subMs : "");
  }
  ctx.fillStyle = "#49635a";
  for (let i=0; i<=divisions; i++) {
    const fraction = i / divisions;
    ctx.textAlign = i === 0 ? "left" : i === divisions ? "right" : "center";
    ctx.fillText(tick(fraction), position(fraction), height-9);
  }
  plotted = {x, y};
}
chart.onclick = event => {
  if (!view || !plotted) return;
  const rect = chart.getBoundingClientRect();
  let best = null, distance = 14;
  for (const row of view.rows) {
    const d = Math.hypot(event.clientX - rect.left - plotted.x(row.measured_at),
                         event.clientY - rect.top - plotted.y(row.temperature));
    if (d < distance) {distance = d; best = row;}
  }
  if (best) {
    const px = Math.round(plotted.x(best.measured_at)), py = Math.round(plotted.y(best.temperature));
    const group = view.rows.filter(r => Math.round(plotted.x(r.measured_at)) === px && Math.round(plotted.y(r.temperature)) === py);
    detail(group[0], group);
  }
};
function* readingRecords(data) {
  for (const r of data.rows) yield [safeSensor(data.sensor), r.id, r.measured_at, r.received_at, numeric(r.temperature), numeric(r.humidity)];
}
el("csv").onclick = () => {
  if (view) downloadCsv(["sensor","id","medido_em_utc","recebido_em_utc","temperatura_c","umidade_pct"],
                        readingRecords(view), "telemetria-" + view.sensor, view);
};

function availabilityPercent(online, evaluated) {
  if (evaluated === 0n) return "";
  if (online === 0n) return "0";
  if (online === evaluated) return "100";
  // Acrescenta precisão quando o arredondamento ocultaria uma ocorrência real.
  let scale = 10n, digits = 1;
  let rounded;
  while (true) {
    rounded = (online * 100n * scale + evaluated / 2n) / evaluated;
    if (rounded !== 0n && rounded !== 100n * scale) break;
    scale *= 10n;
    digits++;
  }
  const fraction = String(rounded % scale).padStart(digits, "0").replace(/0+$/, "");
  return String(rounded / scale) + (fraction ? "," + fraction : "");
}
function availabilityRows(data) {
  if (!data || !data.availability) return [];
  const duration = micros(data.end) - micros(data.start);
  const bounds = data.coverage || data;
  function overlap([start,end]) {
    const left = start > bounds.start ? start : bounds.start;
    const right = end < bounds.end ? end : bounds.end;
    return left < right ? micros(right) - micros(left) : 0n;
  }
  const sum = intervals => intervals.reduce((total, interval) => total + overlap(interval), 0n);
  const blind = sum(data.availability.blind);
  return data.availability.rooms.map(room => {
    const evaluated = sum(room.evaluated);
    let offline = 0n, longest = 0n;
    for (const interval of room.outages) {
      const value = overlap(interval);
      offline += value;
      if (value > longest) longest = value;
    }
    const planned = maintenanceOverlap(room.outages, room.maintenance || [], bounds.start, bounds.end);
    const online = evaluated - offline;
    const percent = availabilityPercent(online, evaluated);
    return {sensor:room.sensor, percent, evaluated, online, offline, planned, outside:offline-planned, longest, blind, unknown:duration-evaluated-blind};
  });
}
async function openRoom(name, data) {
  if (name !== data.sensor) {
    if (!Array.from(sensor.options).some(option => option.value === name)) sensor.add(new Option(name,name));
    sensor.value = name;
    preset.value = "custom";
    showRange(data.start, data.end);
    await loadPeriod();
  }
  if (view && view.sensor === name && view.start === data.start && view.end === data.end) el("chart-title").focus();
}
let comparison = [];
function comparisonTable(data) {
  comparison = availabilityRows(data);
  const body = el("availability-rows");
  body.replaceChildren();
  el("availability-csv").disabled = !comparison.length;
  el("availability-period").textContent = data ? labelTime(data.start) + " a " + labelTime(data.end) + " UTC" : "";
  if (!comparison.length) emptyRow(body, 10, data ? "Sem salas com histórico disponível." : "Aguardando o período aplicado.");
  for (const r of comparison) {
    const button = document.createElement("button");
    button.className = "reading";
    button.textContent = r.sensor;
    button.onclick = () => openRoom(r.sensor, data);
    addCells(body, [button, r.percent ? r.percent + "%" : "Sem dados suficientes",
      ...[r.evaluated,r.online,r.offline,r.longest,r.blind,r.unknown,r.planned,r.outside].map(us => seconds(us)+" s")]);
  }
}
el("availability-csv").onclick = () => {
  if (!view || !comparison.length) return;
  const rows = comparison.map(r => [safeSensor(r.sensor),view.start,view.end,r.percent,
    ...[r.evaluated,r.online,r.offline,r.longest,r.blind,r.unknown,r.planned,r.outside].map(seconds)]);
  downloadCsv(["sensor","inicio_utc","fim_exclusivo_utc","disponibilidade_pct","avaliado_s","em_comunicacao_s",
    "em_interrupcao_s","maior_interrupcao_s","central_sem_observacao_s","evidencia_insuficiente_s",
    "interrupcao_em_manutencao_s","interrupcao_fora_de_manutencao_s"],rows,"disponibilidade",view);
};
function setRooms(names, selected) {
  const current = Array.from(sensor.options, option => option.value);
  if (names.length !== current.length || names.some((name, i) => name !== current[i])) {
    sensor.replaceChildren();
    for (const name of names) sensor.add(new Option(name, name));
  }
  if (names.includes(selected)) sensor.value = selected;
}
function display(data) {
  if (!boot.example && data && data.availability) {
    setRooms(data.availability.rooms.map(room => room.sensor), data.sensor);
  }
  view = data; pageIndex = 0; el("detail").hidden = true;
  el("next-point").hidden = true; el("next-point").onclick = null;
  el("csv").disabled = !data;
  el("result").textContent = data ? data.sensor + " · " + labelTime(data.start) + " a " + labelTime(data.end) + " UTC · " + data.rows.length + " medições" : "";
  if (data && boot.example && (data.start < boot.example.start || data.end > boot.example.end)) {
    el("result").textContent += " · O exemplo contém dados de " + labelTime(boot.example.start) + " a " + labelTime(boot.example.end) + " UTC.";
  }
  comparisonTable(data); table(); eventTable(); draw();
}
async function loadPeriod() {
  const order = ++generation;
  if (pending) pending.abort();
  display(null);el("message").textContent = "";
  try {
    if (!boot.example) pending = new AbortController();
    if (!sensor.value && !boot.example) {
      const response = await fetch("/api/sensores", {signal:pending.signal, cache:"no-store"});
      if (!response.ok) throw new Error("Falha ao consultar as salas. Tente aplicar o filtro novamente.");
      const names = await response.json();
      if (order !== generation) return;
      setRooms(names, sensor.value);
    }
    if (!sensor.value) {
      el("result").textContent = "Aguardando sensores. Use Aplicar após o primeiro sinal.";
      return;
    }
    const relative = preset.value !== "custom";
    const params = new URLSearchParams({sensor:sensor.value});
    let start, end;
    if (!relative) {
      start = utcInput(el("start").value); end = utcInput(el("end").value);
      if (start >= end) throw new Error("O fim deve ser posterior ao início.");
      params.set("inicio", start); params.set("fim", end);
    } else if (boot.example) {
      const anchor = wholeMillis(boot.example.end);
      start = utcInput(new Date(anchor - Number(preset.value)*3600000).toISOString().slice(0,-1));
      end = utcInput(new Date(anchor).toISOString().slice(0,-1));
    } else params.set("periodo", preset.value + "h");
    let data;
    if (boot.example) data = filterExample(sensor.value,start,end);
    else {
      el("result").textContent = "Consultando o histórico…";
      const response = await fetch("/api/historico?" + params, {signal:pending.signal,cache:"no-store"});
      if (!response.ok) throw new Error("A consulta falhou. Confira o período e a conexão com a central.");
      data = await response.json();
    }
    if (order === generation) {
      if (relative) showRange(data.start, data.end);
      display(data);
    }
  } catch (err) {
    if (order === generation) {display(null); el("message").textContent = err.message;}
  }
}
setRooms(boot.sensors, null);
if (boot.example) {
  el("offline").hidden = false; preset.value = "custom";
  showRange(boot.example.start, boot.example.initialEnd || boot.example.end);
} else el("home").hidden = false;
sensor.onchange = loadPeriod;
function editPeriod() {
  preset.value = "custom"; generation++; if (pending) pending.abort(); display(null);
  el("message").textContent = "";
  el("result").textContent = "Aplique o período selecionado.";
}
preset.onchange = () => {if (preset.value === "custom") editPeriod(); else loadPeriod();};
for (const id of ["start","end"]) el(id).oninput = editPeriod;
form.onsubmit = event => {event.preventDefault();loadPeriod();};
el("prev").onclick = () => {pageIndex--;table();};
el("next").onclick = () => {pageIndex++;table();};
window.addEventListener("resize",draw);
loadPeriod();
