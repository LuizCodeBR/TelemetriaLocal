"use strict";
const el = id => document.getElementById(id);

function utcInput(value) {
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d{1,6})?)?$/.test(value) || value.startsWith("0000")) {
    throw new Error("Informe uma data e hora válida em UTC.");
  }
  const [whole, fraction = ""] = value.split(".");
  return (whole.length === 16 ? whole + ":00" : whole) + "." + fraction.padEnd(6, "0") + "+00:00";
}
function wholeMillis(iso) {
  return Date.parse(iso.slice(0,23) + "Z");
}
function labelTime(iso) {
  const [date, hour] = iso.split("T");
  const time = hour.split("+")[0].replace(/0+$/, "").replace(/\.$/, "");
  return date.split("-").reverse().join("/") + " " + time;
}
function showRange(start, end) {
  el("start").value = start.slice(0,23);
  el("end").value = end.slice(0,23);
}

function addCells(body, values) {
  const row = body.insertRow();
  for (const value of values) {
    const cell = row.insertCell();
    if (value instanceof Node) cell.append(value);
    else cell.textContent = value;
  }
}
function emptyRow(body, count, message) {
  const cell = body.insertRow().insertCell();
  cell.colSpan = count;
  cell.textContent = message;
}

function safeSensor(name) {
  return name.startsWith("-") ? "'" + name : name;
}
function csvBlob(header, records) {
  const parts = ["\uFEFF"];
  let lines = [header];
  function flush() {
    parts.push(lines.map(line => line.map(cell => '"' + cell.replace(/"/g,'""') + '"').join(";")).join("\r\n") + "\r\n");
    lines = [];
  }
  for (const record of records) {
    lines.push(record);
    if (lines.length === 1024) flush();
  }
  if (lines.length) flush();
  return new Blob(parts, {type:"text/csv;charset=utf-8"});
}
function downloadCsv(header, records, prefix, period) {
  const url = URL.createObjectURL(csvBlob(header, records));
  const a = document.createElement("a");
  const fileTime = iso => iso.slice(0,26).replaceAll(":","-") + "Z";
  a.href = url; a.download = prefix + "_" + fileTime(period.start) + "_" + fileTime(period.end) + ".csv";
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function micros(iso) {
  return BigInt(wholeMillis(iso))*1000n + BigInt(iso.slice(23,26));
}
// Os intervalos de cada lista são ordenados e não se sobrepõem. O fim é exclusivo.
function maintenanceOverlap(outages, windows, start, end) {
  let total = 0n, i = 0, j = 0;
  while (i < outages.length && j < windows.length) {
    const [down, back] = outages[i], window = windows[j];
    const left = [down, window.start, start].sort().at(-1);
    const right = [back, window.end, end].sort()[0];
    if (left < right) total += micros(right) - micros(left);
    if (back <= window.end) i++;
    else j++;
  }
  return total;
}
function seconds(us) {
  const fraction = String(us % 1000000n).padStart(6,"0").replace(/0+$/, "");
  return String(us / 1000000n) + (fraction ? "," + fraction : "");
}
