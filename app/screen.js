"use strict";

const names = {heating: "승온", holding: "유지", cooling: "냉각"};
const time = new Intl.DateTimeFormat("ko-KR", {timeZone: "Asia/Seoul", month: "numeric",
  day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
const button = document.getElementById("refresh");
const total = document.getElementById("total");
const kilnChoice = document.getElementById("kiln-choice");
const firingChoice = document.getElementById("firing-choice");
const plot = document.getElementById("plot");
const caption = document.getElementById("plot-caption");
const clockTime = new Intl.DateTimeFormat("ko-KR", {timeZone: "Asia/Seoul", hourCycle: "h23",
  hour: "2-digit", minute: "2-digit", second: "2-digit"});
let shown = null;
let chartRequest = 0;
let loading = false;

function draw() {
  const width = Math.max(260, plot.clientWidth);
  const height = plot.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  plot.width = Math.round(width * ratio);
  plot.height = Math.round(height * ratio);
  const ctx = plot.getContext("2d");
  ctx.scale(ratio, ratio);
  if (!shown || !shown.points.length) return;
  const points = shown.points;
  const stamps = points.map(p => Date.parse(p.observed_at));
  const temps = points.map(p => p.temperature_c);
  const top = 17, bottom = height - 28, left = 46, right = width - 18;
  const yMin = Math.floor((Math.min(...temps) - 1) / 25) * 25;
  const yMax = Math.ceil((Math.max(...temps) + 1) / 25) * 25;
  const tMin = stamps[0], tMax = stamps[stamps.length - 1];
  const x = t => tMax === tMin ? (left + right) / 2 : left + (t - tMin) * (right - left) / (tMax - tMin);
  const y = v => bottom - (v - yMin) * (bottom - top) / (yMax - yMin);
  ctx.font = '11px "Malgun Gothic", sans-serif';
  ctx.fillStyle = "#615750";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * i / 4;
    ctx.fillText(Math.round(v) + "°", left - 8, y(v) + 4);
    ctx.strokeStyle = "#e6ddd1";
    ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
  }
  const ticks = tMax === tMin ? 1 : width < 450 ? 3 : 5;
  for (let i = 0; i < ticks; i++) {
    const t = ticks === 1 ? tMin : tMin + (tMax - tMin) * i / (ticks - 1);
    ctx.textAlign = ticks === 1 ? "center" : i === 0 ? "left" : i === ticks - 1 ? "right" : "center";
    ctx.fillText(clockTime.format(new Date(t)), x(t), height - 5);
  }
  ctx.strokeStyle = "#a14d2d"; ctx.lineWidth = 2.5; ctx.lineJoin = "round";
  ctx.beginPath();
  points.forEach((p, i) => {
    // A missing sequence must leave a visible break until the sensor replays it.
    if (i && p.sequence === points[i - 1].sequence + 1) ctx.lineTo(x(stamps[i]), y(temps[i]));
    else ctx.moveTo(x(stamps[i]), y(temps[i]));
  });
  ctx.stroke();
  ctx.fillStyle = "#a14d2d";
  points.forEach((p, i) => {
    if (!i || i === points.length - 1 || p.sequence !== points[i - 1].sequence + 1 ||
        (i + 1 < points.length && points[i + 1].sequence !== p.sequence + 1)) {
      ctx.beginPath(); ctx.arc(x(stamps[i]), y(temps[i]), 3, 0, 2 * Math.PI); ctx.fill();
    }
  });
}

function choices(select, values, preferred) {
  const chosen = values.includes(preferred) ? preferred : values[0];
  if (Array.from(select.options, option => option.value).join("|") !== values.join("|")) {
    select.replaceChildren(...values.map(value => new Option(value, value)));
  }
  if (chosen) select.value = chosen;
}

async function jsonAt(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error("unavailable");
  return response.json();
}

async function loadChart() {
  const requestId = ++chartRequest;
  const kiln = kilnChoice.value;
  if (!kiln) return;
  const base = "/api/series/" + encodeURIComponent(kiln);
  try {
    const menu = await jsonAt(base);
    if (requestId !== chartRequest) return;
    const preferred = firingChoice.dataset.kiln === kiln ? firingChoice.value : "demo-" + kiln;
    choices(firingChoice, menu.firings, preferred);
    firingChoice.dataset.kiln = kiln;
    const data = await jsonAt(base + "/" + encodeURIComponent(firingChoice.value));
    if (requestId !== chartRequest) return;
    shown = data;
    const seq = data.points.map(p => p.sequence);
    const gaps = Math.max(...seq) - Math.min(...seq) + 1 - new Set(seq).size;
    caption.textContent = "관측 시각 기준 · 최근 " + seq.length + "건 · 표시 구간 누락 " + gaps + "건";
    plot.setAttribute("aria-label", kiln + " " + data.firing_id + " 온도 곡선. " + caption.textContent);
    draw();
  } catch {
    if (requestId !== chartRequest) return;
    caption.textContent = "온도 곡선을 갱신하지 못했습니다.";
  }
}
function cell(tag, value) {
  const el = document.createElement(tag);
  el.textContent = value;
  return el;
}
async function load() {
  if (loading) return;
  loading = true;
  button.disabled = true;
  try {
    const data = await jsonAt("/api/summary");
    const fragment = document.createDocumentFragment();
    for (const k of data.kilns) {
      const row = document.createElement("tr");
      const title = cell("th", k.kiln_id);
      title.scope = "row";
      const phase = cell("span", names[k.phase]);
      phase.className = "phase " + k.phase;
      const category = document.createElement("td");
      category.append(phase);
      row.append(title, category, cell("td", k.temperature_c.toFixed(1) + " °C"),
                 cell("td", k.peak_c.toFixed(1) + " °C"), cell("td", time.format(new Date(k.observed_at))),
                 cell("td", k.reading_count.toLocaleString("ko-KR")));
      fragment.append(row);
    }
    document.getElementById("kilns").replaceChildren(fragment);
    total.textContent = "가마 " + data.kilns.length + "기 · 누적 " + data.reading_count.toLocaleString("ko-KR") + "건";
    document.getElementById("stamp").textContent = "기록 갱신 " + time.format(new Date(data.updated_at));
    choices(kilnChoice, data.kilns.map(k => k.kiln_id), kilnChoice.value || "K03");
    await loadChart();
  } catch {
    total.textContent = "최신 기록을 가져오지 못했습니다. 다시 조회해 주세요.";
  } finally {
    button.disabled = false;
    loading = false;
  }
}
button.addEventListener("click", load);
kilnChoice.addEventListener("change", loadChart);
firingChoice.addEventListener("change", loadChart);
new ResizeObserver(draw).observe(plot);
window.setInterval(() => { if (!document.hidden) load(); }, 3000);
load();
