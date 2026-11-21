// Stock Agent dashboard (milestone 3).
// Plain JavaScript, no build step. Every piece of text that comes from the API is put on
// the page with textContent (never innerHTML), so a strange company name can never run as code.
"use strict";

const MOVERS_N = 15;        // bars in the chart and rows in the movers table
const COMPANIES_N = 20;     // rows in the top-companies table
const REQUEST_TIMEOUT_MS = 10000;
const VERIFY_TIP = "Moved 20% or more in a single day. Check the news or a second source.";

// What the user has picked, and what we already fetched.
const state = {
  period: "day",            // day | week | month
  direction: "gainers",     // gainers | losers
  outliers: new Set(),      // tickers flagged by /api/outliers
  groups: null,             // last /api/groups response
};
const moversCache = new Map();  // "day:gainers" -> Promise of the API response
let moversChart = null;

// ---------- small helpers ----------

/** Fetch JSON with a timeout. Throws an Error with a readable message on any failure. */
async function fetchJson(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      let detail = "";
      try { detail = (await response.json()).detail; } catch (e) { /* not JSON */ }
      const error = new Error(typeof detail === "string" && detail ? detail : `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/** Create an element with optional CSS classes and text (text goes in safely via textContent). */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

/** "▲ +9.95%" / "▼ −2.77%" / "0.00%". Arrow and sign carry the meaning, colour is extra. */
function pctParts(value) {
  if (!isNumber(value)) return { text: "—", cls: "flat", label: "no data" };
  if (value > 0) return { text: `▲ +${value.toFixed(2)}%`, cls: "up", label: `up ${value.toFixed(2)} percent` };
  if (value < 0) return { text: `▼ −${Math.abs(value).toFixed(2)}%`, cls: "down", label: `down ${Math.abs(value).toFixed(2)} percent` };
  return { text: "0.00%", cls: "flat", label: "unchanged" };
}

function pctCell(value) {
  const parts = pctParts(value);
  const td = el("td", `px-3 py-2 text-right tabular-nums whitespace-nowrap font-medium ${parts.cls}`);
  const visible = el("span", "", parts.text);
  visible.setAttribute("aria-hidden", "true");
  td.append(visible, el("span", "sr-only", parts.label));
  return td;
}

function money(value) {
  return isNumber(value) ? `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—";
}

/** Market cap arrives in billions: 24.3 -> "$24.3B", 4978.1 -> "$4.98T". */
function marketCap(billions) {
  if (!isNumber(billions)) return "—";
  if (billions >= 1000) return `$${(billions / 1000).toFixed(2)}T`;
  return `$${billions.toFixed(1)}B`;
}

/** "2026-11-20" -> "Fri, Nov 20, 2026". Built from parts so the time zone cannot shift the day. */
function prettyDate(isoDate) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate));
  if (!match) return String(isoDate);
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" });
}

/** A small "verify" badge with a tooltip that also appears on keyboard focus. */
function verifyBadge() {
  const wrap = el("span", "group relative ml-2 inline-block align-middle");
  const badge = el("span", "cursor-help rounded bg-amber-100 px-1.5 py-0.5 text-xs font-semibold text-amber-900 focus-visible:outline-2 focus-visible:outline-indigo-500 dark:bg-amber-900 dark:text-amber-100", "verify");
  badge.tabIndex = 0;
  badge.title = VERIFY_TIP;
  badge.setAttribute("aria-label", `verify: ${VERIFY_TIP}`);
  const tip = el("span", "pointer-events-none absolute left-0 top-full z-10 mt-1 hidden w-56 rounded-md bg-slate-900 p-2 text-xs font-normal text-white shadow-lg group-hover:block group-focus-within:block dark:bg-slate-700", VERIFY_TIP);
  tip.setAttribute("aria-hidden", "true");
  wrap.append(badge, tip);
  return wrap;
}

/** A row that spans the table and shows a message (loading, empty, or error). */
function messageRow(tbody, columns, text, isError) {
  const tr = el("tr");
  const td = el("td", `px-3 py-6 text-center ${isError ? "text-red-700 dark:text-red-400" : "text-slate-600 dark:text-slate-400"}`, text);
  td.colSpan = columns;
  if (isError) td.setAttribute("role", "alert");
  tr.append(td);
  tbody.replaceChildren(tr);
}

/** Grey shimmer rows while a table loads. */
function skeletonRows(tbody, columns, rows) {
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < rows; i++) {
    const tr = el("tr", "border-b border-slate-100 dark:border-slate-800");
    for (let c = 0; c < columns; c++) {
      const td = el("td", "px-3 py-2.5");
      td.append(el("span", "block h-4 w-full animate-pulse rounded bg-slate-200 dark:bg-slate-700"));
      tr.append(td);
    }
    fragment.append(tr);
  }
  tbody.replaceChildren(fragment);
}

/** Friendly message for a failed request. */
function errorText(error) {
  if (error.status === 503) return `No data yet: ${error.message} (run python main.py)`;
  if (error.status) return `Could not load this section: ${error.message}`;
  return "Could not reach the API. Is uvicorn running?";
}

function showGlobalError() {
  const box = document.getElementById("global-error");
  box.textContent = "Cannot reach the Stock Agent API. Start it with: python -m uvicorn api:app --reload, then reload this page.";
  box.hidden = false;
}

// ---------- summary + header ----------

async function loadSummary() {
  const ids = ["card-stocks", "card-outliers", "card-date"];
  try {
    const data = await fetchJson("/api/summary");
    document.getElementById("as-of").textContent = `as of ${prettyDate(data.as_of)}`;
    document.getElementById("card-stocks").textContent = isNumber(data.stocks) ? data.stocks.toLocaleString("en-US") : "—";
    document.getElementById("card-outliers").textContent = isNumber(data.outliers) ? String(data.outliers) : "—";
    document.getElementById("card-date").textContent = prettyDate(data.as_of);
  } catch (error) {
    document.getElementById("as-of").textContent = "Data date unavailable";
    for (const id of ids) document.getElementById(id).textContent = "—";
    throw error;
  }
}

async function loadOutliers() {
  const data = await fetchJson("/api/outliers");
  state.outliers = new Set((data.stocks || []).map((row) => row.ticker));
}

// ---------- movers (chart + table) ----------

function getMovers(period, direction) {
  const key = `${period}:${direction}`;
  if (!moversCache.has(key)) {
    const request = fetchJson(`/api/movers?period=${period}&direction=${direction}&n=${MOVERS_N}`);
    request.catch(() => moversCache.delete(key));  // a failed request can be retried later
    moversCache.set(key, request);
  }
  return moversCache.get(key);
}

async function showMovers() {
  const { period, direction } = state;
  const tbody = document.getElementById("movers-body");
  skeletonRows(tbody, 6, 8);
  document.getElementById("movers-caption").textContent = `Top ${MOVERS_N} ${period} ${direction}`;
  try {
    const data = await getMovers(period, direction);
    // The user may have clicked another tab while we waited: only draw the latest choice.
    if (period !== state.period || direction !== state.direction) return;
    const rows = data.stocks || [];
    renderMoversTable(tbody, rows, period);
    renderMoversChart(rows, period, direction);
  } catch (error) {
    if (period !== state.period || direction !== state.direction) return;
    messageRow(tbody, 6, errorText(error), true);
    renderMoversChart([], period, direction);
  }
}

function renderMoversTable(tbody, rows, period) {
  if (rows.length === 0) {
    messageRow(tbody, 6, "No stocks to show.", false);
    return;
  }
  const fragment = document.createDocumentFragment();
  rows.forEach((row, index) => {
    const tr = el("tr", "border-b border-slate-100 last:border-0 dark:border-slate-800");
    tr.append(el("td", "px-3 py-2 text-right tabular-nums text-slate-500 dark:text-slate-400", index + 1));
    const tickerCell = el("td", "px-3 py-2 font-semibold whitespace-nowrap", row.ticker);
    if (state.outliers.has(row.ticker)) tickerCell.append(verifyBadge());
    tr.append(tickerCell);
    tr.append(el("td", "max-w-[12rem] truncate px-3 py-2 text-slate-700 dark:text-slate-300", row.name));
    tr.append(el("td", "px-3 py-2 text-right tabular-nums", money(row.close)));
    tr.append(pctCell(row[`${period}_pct`]));
    tr.append(el("td", "px-3 py-2 text-right tabular-nums whitespace-nowrap", marketCap(row.market_cap_billions)));
    fragment.append(tr);
  });
  tbody.replaceChildren(fragment);
}

/** Chart colours depend on the theme (a canvas cannot read Tailwind classes). */
function chartColors() {
  const dark = document.documentElement.classList.contains("dark");
  return {
    up: dark ? "#4ade80" : "#15803d",     // green-400 / green-700
    down: dark ? "#f87171" : "#b91c1c",   // red-400 / red-700
    grid: dark ? "#334155" : "#e2e8f0",   // slate-700 / slate-200
    text: dark ? "#cbd5e1" : "#475569",   // slate-300 / slate-600
  };
}

function signedPct(value) {
  if (!isNumber(value)) return "no data";
  return value > 0 ? `+${value.toFixed(2)}%` : value < 0 ? `−${Math.abs(value).toFixed(2)}%` : "0.00%";
}

function renderMoversChart(rows, period, direction) {
  const colors = chartColors();
  const values = rows.map((row) => row[`${period}_pct`]);
  const labels = rows.map((row) => (state.outliers.has(row.ticker) ? `${row.ticker} ⚠` : row.ticker));
  const barColors = values.map((v) => (isNumber(v) && v < 0 ? colors.down : colors.up));

  const canvas = document.getElementById("movers-chart");
  canvas.setAttribute("aria-label",
    `Horizontal bar chart of the top ${rows.length} ${period} ${direction}. The same numbers are in the table next to it.`);

  if (typeof Chart === "undefined") return;  // Chart.js failed to load; the table still works.

  if (moversChart) {
    moversChart.data.labels = labels;
    moversChart.data.datasets[0].data = values;
    moversChart.data.datasets[0].backgroundColor = barColors;
    applyChartTheme(colors);
    moversChart.update();
    return;
  }
  moversChart = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [{ label: "% change", data: values, backgroundColor: barColors, borderRadius: 4, maxBarThickness: 22 }] },
    options: {
      indexAxis: "y",                // horizontal bars
      responsive: true,
      maintainAspectRatio: false,    // fill the fixed-height box
      animation: { duration: 250 },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => signedPct(ctx.parsed.x) } },
      },
      scales: {
        x: { ticks: { callback: (value) => signedPct(value) } },
        y: { ticks: { autoSkip: false } },
      },
    },
  });
  applyChartTheme(colors);
  moversChart.update();
}

function applyChartTheme(colors) {
  const { x, y } = moversChart.options.scales;
  for (const axis of [x, y]) {
    axis.grid = { color: colors.grid };
    axis.border = { color: colors.grid };
    axis.ticks.color = colors.text;
  }
}

// ---------- top companies ----------

async function loadCompanies() {
  const tbody = document.getElementById("companies-body");
  skeletonRows(tbody, 8, 8);
  try {
    const data = await fetchJson(`/api/companies?n=${COMPANIES_N}`);
    const rows = data.companies || [];
    if (rows.length === 0) {
      messageRow(tbody, 8, "No companies to show.", false);
      return;
    }
    const fragment = document.createDocumentFragment();
    rows.forEach((row, index) => {
      const tr = el("tr", "border-b border-slate-100 last:border-0 dark:border-slate-800");
      tr.append(el("td", "px-3 py-2 text-right tabular-nums text-slate-500 dark:text-slate-400", index + 1));
      const tickerCell = el("td", "px-3 py-2 font-semibold whitespace-nowrap", row.ticker);
      if (state.outliers.has(row.ticker)) tickerCell.append(verifyBadge());
      tr.append(tickerCell);
      tr.append(el("td", "max-w-[14rem] truncate px-3 py-2 text-slate-700 dark:text-slate-300", row.name));
      tr.append(el("td", "px-3 py-2 text-right tabular-nums", money(row.close)));
      tr.append(el("td", "px-3 py-2 text-right tabular-nums whitespace-nowrap", marketCap(row.market_cap_billions)));
      tr.append(pctCell(row.day_pct), pctCell(row.week_pct), pctCell(row.month_pct));
      fragment.append(tr);
    });
    tbody.replaceChildren(fragment);
  } catch (error) {
    messageRow(tbody, 8, errorText(error), true);
    throw error;
  }
}

// ---------- groups ----------

function groupSkeletons() {
  const grid = document.getElementById("groups-grid");
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < 6; i++) {
    const card = el("div", "h-32 animate-pulse rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900");
    fragment.append(card);
  }
  grid.replaceChildren(fragment);
}

async function loadGroups() {
  groupSkeletons();
  try {
    state.groups = await fetchJson("/api/groups");
    document.getElementById("groups-note").textContent = state.groups.note || "";
    renderGroups();
  } catch (error) {
    const grid = document.getElementById("groups-grid");
    const message = el("p", "text-sm text-red-700 dark:text-red-400", errorText(error));
    message.setAttribute("role", "alert");
    grid.replaceChildren(message);
    throw error;
  }
}

function statLine(labelText, value) {
  const parts = pctParts(value);
  const row = el("div", "flex items-baseline justify-between");
  row.append(el("dt", "text-sm text-slate-600 dark:text-slate-400", labelText));
  const dd = el("dd", `tabular-nums font-semibold ${parts.cls}`);
  const visible = el("span", "", parts.text);
  visible.setAttribute("aria-hidden", "true");
  dd.append(visible, el("span", "sr-only", parts.label));
  row.append(dd);
  return row;
}

function renderGroups() {
  if (!state.groups) return;
  const period = state.period;
  document.getElementById("groups-period").textContent = `· ${period}`;
  const grid = document.getElementById("groups-grid");
  const entries = Object.entries(state.groups.groups || {});
  if (entries.length === 0) {
    grid.replaceChildren(el("p", "text-sm text-slate-600 dark:text-slate-400", "No groups defined."));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const [name, group] of entries) {
    const stats = group[period] || {};
    const card = el("article", "rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900");
    card.append(el("h3", "font-semibold", name.replaceAll("_", " ")));
    card.append(el("p", "mb-3 text-xs text-slate-500 dark:text-slate-400",
      `${group.stocks_with_data ?? "—"} of ${group.tickers_listed ?? "—"} stocks with data`));
    const list = el("dl", "space-y-1");
    list.append(statLine("Average", stats.average_pct), statLine("Median", stats.median_pct));
    card.append(list);
    const counts = el("p", "mt-3 text-sm text-slate-700 dark:text-slate-300");
    counts.append(
      el("span", "up font-medium", `▲ ${stats.up ?? "—"} up`),
      el("span", "text-slate-400", "  ·  "),
      el("span", "down font-medium", `▼ ${stats.down ?? "—"} down`),
    );
    card.append(counts);
    fragment.append(card);
  }
  grid.replaceChildren(fragment);
}

// ---------- controls: tabs, direction buttons, theme ----------

function selectPeriod(button, moveFocus) {
  const tabs = document.querySelectorAll("#period-tabs [role=tab]");
  for (const tab of tabs) {
    const selected = tab === button;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  if (moveFocus) button.focus();
  document.getElementById("movers-panel").setAttribute("aria-labelledby", button.id);
  state.period = button.dataset.period;
  showMovers();
  renderGroups();
}

function setupTabs() {
  const tabs = Array.from(document.querySelectorAll("#period-tabs [role=tab]"));
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectPeriod(tab, false));
    // Standard tab keyboard pattern: arrows move between tabs, Home/End jump to the ends.
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      else if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      else if (event.key === "Home") next = tabs[0];
      else if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        selectPeriod(next, true);
      }
    });
  });
}

function setupDirection() {
  const buttons = document.querySelectorAll("[data-direction]");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
      state.direction = button.dataset.direction;
      showMovers();
    });
  }
}

function updateThemeButton() {
  const dark = document.documentElement.classList.contains("dark");
  const button = document.getElementById("theme-toggle");
  button.setAttribute("aria-pressed", String(dark));
  document.getElementById("theme-icon").textContent = dark ? "☀" : "☾";
  document.getElementById("theme-label").textContent = dark ? "Light mode" : "Dark mode";
}

function setupTheme() {
  updateThemeButton();
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.classList.toggle("dark");
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* storage blocked */ }
    updateThemeButton();
    if (moversChart) {
      const colors = chartColors();
      moversChart.data.datasets[0].backgroundColor = moversChart.data.datasets[0].data.map(
        (v) => (isNumber(v) && v < 0 ? colors.down : colors.up));
      applyChartTheme(colors);
      moversChart.update();
    }
  });
}

// ---------- start ----------

async function init() {
  setupTheme();
  setupTabs();
  setupDirection();
  groupSkeletons();
  skeletonRows(document.getElementById("movers-body"), 6, 8);
  skeletonRows(document.getElementById("companies-body"), 8, 8);

  // The outlier list decides the "verify" badges, so fetch it before drawing the tables.
  // A failure here only means no badges; it must not block the rest of the page.
  await loadOutliers().catch(() => {});

  showMovers();  // shows its own errors inside the movers section
  const results = await Promise.allSettled([loadSummary(), loadCompanies(), loadGroups()]);
  // If every section failed for a network reason (no HTTP status), the server is down.
  const allDown = results.every((r) => r.status === "rejected" && !r.reason.status);
  if (allDown) showGlobalError();
}

init();
