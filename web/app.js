// Stock Agent dashboard (milestones 3 to 5).
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

/**
 * Fetch JSON with a timeout. Throws an Error with a readable message on any failure.
 * GET by default; pass { method: "POST", body } to send JSON, and timeoutMs for slow calls.
 */
async function fetchJson(url, { method = "GET", body, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const init = { method, signal: controller.signal };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(url, init);
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

/** A table row for one company. data-ticker lets a click anywhere on the row open the panel. */
function companyRow(ticker) {
  const tr = el("tr", "cursor-pointer border-b border-slate-100 last:border-0 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-800/60");
  tr.dataset.ticker = ticker;
  return tr;
}

/** The ticker cell: a real link to #/stock/TICKER, so Tab reaches it and Enter opens the panel. */
function tickerCell(ticker) {
  const td = el("td", "px-3 py-2 font-semibold whitespace-nowrap");
  const link = el("a", "rounded underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500", ticker);
  link.href = `#/stock/${encodeURIComponent(ticker)}`;
  link.setAttribute("aria-label", `${ticker}: open details`);
  td.append(link);
  if (state.outliers.has(ticker)) td.append(verifyBadge());
  return td;
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
    const tr = companyRow(row.ticker);
    tr.append(el("td", "px-3 py-2 text-right tabular-nums text-slate-500 dark:text-slate-400", index + 1));
    tr.append(tickerCell(row.ticker));
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
      const tr = companyRow(row.ticker);
      tr.append(el("td", "px-3 py-2 text-right tabular-nums text-slate-500 dark:text-slate-400", index + 1));
      tr.append(tickerCell(row.ticker));
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

// ---------- company side panel ----------
//
// One rule drives everything: the URL hash. "#/stock/NVDA" means "panel open on NVDA",
// anything else means "panel closed". Clicks, the back button, a reload and a shared link
// all change the hash, and one listener (route) reacts to it.

// Two price downloads per ticker, each fetched once and cached:
//   daily:  the last 253 daily closes, enough for 1M, 3M, 6M and 1Y.
//   weekly: 5 years thinned by Python to one close every 5 trading days (~253 points).
// Both answers carry the same `ranges`, computed in Python from daily closes.
const PRICE_SOURCES = {
  daily: "days=253",
  weekly: "days=1261&step=5",
};
// Which download each range draws from. `closes` is used only if an older API answer
// has no `ranges` (the API's value wins); `years` = show the year on the x axis.
const RANGE_INFO = {
  "1M": { closes: 22, words: "1 month", source: "daily", years: false },
  "3M": { closes: 64, words: "3 months", source: "daily", years: false },
  "6M": { closes: 127, words: "6 months", source: "daily", years: false },
  "1Y": { closes: 253, words: "1 year", source: "daily", years: true },
  "5Y": { closes: 1261, words: "5 years", source: "weekly", years: true },
};
const TICKER_RE = /^[A-Za-z0-9.\-]{1,10}$/;  // same pattern the API accepts
const PANEL_ANIMATION_MS = 200; // must match the CSS transition in index.html

const panel = {
  open: false,
  ticker: null,          // ticker currently shown; late answers for other tickers are ignored
  opener: null,          // the link that opened the panel, to give focus back on close
  pushedHistory: false,  // true when OUR click added the history entry (so closing = history.back())
  range: "3M",           // a key of RANGE_INFO and of the API's `ranges`
  data: {},              // "daily" / "weekly" -> /prices answer for the current ticker
};
const stockCache = new Map();   // ticker -> Promise of /api/stock/{ticker}
const pricesCache = new Map();  // "TICKER daily" / "TICKER weekly" -> Promise of /api/stock/{ticker}/prices
let priceChart = null;
let closeTimer = null;
let inertNodes = [];

/** Same idea as getMovers: reuse an answer we already have, forget failures so Retry works. */
function cachedFetch(cache, key, url) {
  if (!cache.has(key)) {
    const request = fetchJson(url);
    request.catch(() => cache.delete(key));
    cache.set(key, request);
  }
  return cache.get(key);
}

/** "2026-11-20" -> "Nov 20" for the chart's x axis, or "Nov 2026" when the range spans years. */
function shortDate(isoDate, withYear) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate));
  if (!match) return String(isoDate);
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const format = withYear ? { month: "short", year: "numeric" } : { month: "short", day: "numeric" };
  return date.toLocaleDateString("en-US", format);
}

/** Ticker from the hash, or null when the hash is not a stock link. */
function tickerFromHash() {
  const match = /^#\/stock\/(.*)$/.exec(window.location.hash);
  if (!match) return null;
  try { return decodeURIComponent(match[1]).trim().toUpperCase(); } catch (e) { return ""; }
}

function route() {
  const ticker = tickerFromHash();
  if (ticker === null) hidePanel();
  else openPanel(ticker);
}

// ----- open / close -----

function openPanel(ticker) {
  const root = document.getElementById("panel-root");
  if (!panel.open) {
    clearTimeout(closeTimer);             // reopened during the slide-out: keep it visible
    root.hidden = false;
    // Make everything behind the panel unclickable and un-tabbable while it is open.
    inertNodes = Array.from(document.body.children).filter(
      (node) => node !== root && !["SCRIPT", "STYLE"].includes(node.tagName));
    for (const node of inertNodes) node.inert = true;
    document.body.style.overflow = "hidden";   // the page behind should not scroll
    requestAnimationFrame(() => root.classList.add("is-open"));  // next frame, so the slide animates
    panel.open = true;
    document.getElementById("panel-close").focus({ preventScroll: true });
  }
  if (ticker === panel.ticker) return;   // already showing it
  panel.ticker = ticker;
  panel.data = {};
  resetPanelAsk(ticker);
  showStock(ticker);
  showPrices(ticker);
}

function hidePanel() {
  if (!panel.open) return;
  const root = document.getElementById("panel-root");
  root.classList.remove("is-open");
  for (const node of inertNodes) node.inert = false;
  inertNodes = [];
  document.body.style.overflow = "";
  // Wait for the slide-out, then hide it and free the chart.
  closeTimer = setTimeout(() => {
    root.hidden = true;
    destroyPriceChart();
  }, PANEL_ANIMATION_MS);

  // Give focus back to the row that opened the panel. If the table was redrawn meanwhile,
  // look for a row with the same ticker; otherwise land on the main content.
  let target = panel.opener && panel.opener.isConnected ? panel.opener : null;
  if (!target) {
    const row = Array.from(document.querySelectorAll("tr[data-ticker]")).find((tr) => tr.dataset.ticker === panel.ticker);
    target = row ? row.querySelector("a") : document.getElementById("main");
  }
  target.focus({ preventScroll: false });

  panel.open = false;
  panel.ticker = null;
  resetPanelAsk(null);
  panel.opener = null;
  panel.pushedHistory = false;
  panel.data = {};
}

/** Close button, Escape and backdrop all come here. */
function requestClose() {
  if (panel.pushedHistory) {
    history.back();            // removes our history entry; the hashchange then calls hidePanel
  } else {
    // Opened from a reload or a shared link: going back would leave the page, so clear the hash in place.
    history.replaceState(null, "", window.location.pathname + window.location.search);
    hidePanel();
  }
}

/** Tab and Shift+Tab wrap around inside the panel instead of leaving it. */
function trapTab(event) {
  const dialog = document.getElementById("stock-panel");
  const focusable = Array.from(dialog.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'))
    .filter((node) => node.getClientRects().length > 0);   // skip hidden ones
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (!dialog.contains(active)) {
    event.preventDefault();
    first.focus();
  } else if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

function setupPanel() {
  window.addEventListener("hashchange", route);
  document.getElementById("panel-close").addEventListener("click", requestClose);
  document.getElementById("panel-backdrop").addEventListener("click", requestClose);
  document.addEventListener("keydown", (event) => {
    if (!panel.open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      requestClose();
    } else if (event.key === "Tab") {
      trapTab(event);
    }
  });

  // Rows: the ticker link changes the hash by itself; a click elsewhere on the row does it for us.
  for (const id of ["movers-body", "companies-body"]) {
    document.getElementById(id).addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-ticker]");
      if (!row) return;
      const link = row.querySelector("a");
      const clickedLink = event.target.closest("a");
      // Ctrl/Cmd/Shift-click on the link opens a new tab: leave that to the browser.
      if (clickedLink && (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0)) return;
      if (!clickedLink && window.getSelection().toString()) return;  // user was selecting text
      panel.opener = link;
      panel.pushedHistory = true;
      if (!clickedLink) window.location.hash = link.getAttribute("href");
    });
  }

  for (const button of document.querySelectorAll("#stock-panel [data-range]")) {
    button.addEventListener("click", () => {
      panel.range = button.dataset.range;
      for (const other of document.querySelectorAll("#stock-panel [data-range]")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      if (!panel.ticker) return;
      if (panel.data[RANGE_INFO[panel.range].source]) drawPriceChart();
      else showPrices(panel.ticker);   // first 5Y click downloads the weekly points
    });
  }
}

// ----- messages -----

/** A message in one part of the panel, with an optional Retry button for network errors. */
function panelMessage(container, text, retry) {
  const box = el("div", "rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200");
  box.setAttribute("role", "alert");
  box.append(el("p", "", text));
  if (retry) {
    const button = el("button", "mt-2 rounded-md border border-red-400 px-3 py-1 text-sm font-medium hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-indigo-500 dark:border-red-700 dark:hover:bg-red-900", "Retry");
    button.type = "button";
    button.addEventListener("click", () => {
      container.focus();   // the button is about to disappear; keep focus inside the panel
      retry();
    });
    box.append(button);
  }
  container.replaceChildren(box);
}

/** Readable text for a failed panel request. */
function panelErrorText(error, notFoundText) {
  if (error.status === 404) return notFoundText;
  if (error.status === 503) return `No data yet: ${error.message} (run python main.py)`;
  if (error.status) return `Could not load this part: ${error.message}`;
  return "Could not reach the API (or it took too long). Is uvicorn running?";
}

/** Long or odd hash text is shown shortened, and always via textContent. */
function displayTicker(ticker) {
  return ticker.length > 12 ? `${ticker.slice(0, 12)}…` : ticker;
}

// ----- stats part -----

function statsSkeleton() {
  const wrap = el("div", "space-y-4");
  wrap.append(el("span", "block h-8 w-40 animate-pulse rounded bg-slate-200 dark:bg-slate-700"));
  const tiles = el("div", "grid grid-cols-3 gap-3");
  for (let i = 0; i < 3; i++) tiles.append(el("span", "block h-16 animate-pulse rounded-lg bg-slate-200 dark:bg-slate-700"));
  wrap.append(tiles, el("span", "block h-5 w-48 animate-pulse rounded bg-slate-200 dark:bg-slate-700"));
  return wrap;
}

async function showStock(ticker) {
  const stats = document.getElementById("panel-stats");
  document.getElementById("panel-title").textContent = displayTicker(ticker) || "Unknown";
  document.getElementById("panel-badge").replaceChildren();
  document.getElementById("panel-name").textContent = "";

  if (!TICKER_RE.test(ticker)) {
    document.getElementById("panel-title").textContent = "Unknown ticker";
    panelMessage(stats, `"${displayTicker(ticker)}" is not a valid ticker symbol.`);
    return;
  }
  stats.setAttribute("aria-busy", "true");
  stats.replaceChildren(statsSkeleton());
  try {
    const data = await cachedFetch(stockCache, ticker, `/api/stock/${encodeURIComponent(ticker)}`);
    if (ticker !== panel.ticker) return;   // the user moved on to another company
    renderStats(data);
  } catch (error) {
    if (ticker !== panel.ticker) return;
    const retry = error.status ? null : () => showStock(ticker);
    panelMessage(stats, panelErrorText(error, `${ticker} isn't in the latest data. Check the symbol; it may have no data today.`), retry);
  } finally {
    stats.removeAttribute("aria-busy");
  }
}

function statTile(label, value) {
  const parts = pctParts(value);
  const tile = el("div", "rounded-lg border border-slate-200 p-3 text-center dark:border-slate-800");
  tile.append(el("dt", "text-xs text-slate-600 dark:text-slate-400", label));
  const dd = el("dd", `mt-1 tabular-nums font-semibold whitespace-nowrap ${parts.cls}`);
  const visible = el("span", "", parts.text);
  visible.setAttribute("aria-hidden", "true");
  dd.append(visible, el("span", "sr-only", parts.label));
  tile.append(dd);
  return tile;
}

function renderStats(data) {
  const stock = data.stock || {};
  document.getElementById("panel-title").textContent = stock.ticker || panel.ticker;
  document.getElementById("panel-name").textContent = stock.name || "";
  if (stock.is_outlier) document.getElementById("panel-badge").replaceChildren(verifyBadge());

  const wrap = el("div", "space-y-4");

  const top = el("div", "flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1");
  top.append(el("p", "text-3xl font-semibold tabular-nums", money(stock.close)));
  top.append(el("p", "text-sm text-slate-600 dark:text-slate-400", `Mkt cap ${marketCap(stock.market_cap_billions)}`));
  wrap.append(top);
  wrap.append(el("p", "text-sm text-slate-600 dark:text-slate-400", `Close as of ${prettyDate(data.as_of)}`));

  const tiles = el("dl", "grid grid-cols-3 gap-3");
  tiles.append(statTile("Day", stock.day_pct), statTile("Week", stock.week_pct), statTile("Month", stock.month_pct));
  wrap.append(tiles);

  const groups = Array.isArray(stock.groups) ? stock.groups : [];
  const groupsBox = el("div");
  groupsBox.append(el("h3", "mb-2 text-sm font-semibold", "Groups"));
  if (groups.length === 0) {
    groupsBox.append(el("p", "text-sm text-slate-600 dark:text-slate-400", "Not in any group."));
  } else {
    const list = el("ul", "flex flex-wrap gap-2");
    for (const name of groups) list.append(el("li", "chip", String(name).replaceAll("_", " ")));
    groupsBox.append(list);
  }
  wrap.append(groupsBox);

  document.getElementById("panel-stats").replaceChildren(wrap);
}

// ----- chart part -----

function destroyPriceChart() {
  if (priceChart) {
    priceChart.destroy();
    priceChart = null;
  }
}

/** Show a loading shimmer or a message in the chart area (and hide the canvas). */
function chartPlaceholder(node) {
  destroyPriceChart();
  document.getElementById("panel-chart-box").hidden = true;
  document.getElementById("panel-chart-note").hidden = true;
  document.getElementById("panel-chart-step").hidden = true;
  document.getElementById("panel-chart-message").replaceChildren(...(node ? [node] : []));
  renderRangeChange(null);   // no chart, no range number
}

/**
 * "▲ +4.20% over 3M" next to the 1M/3M toggle. The number is the API's change_pct
 * (Python did the math); null or missing means show nothing. textContent only.
 */
function renderRangeChange(range) {
  const box = document.getElementById("panel-range-change");
  const value = range ? range.change_pct : null;
  if (!isNumber(value)) {
    box.replaceChildren();
    box.className = "text-sm tabular-nums font-semibold whitespace-nowrap";
    return;
  }
  const parts = pctParts(value);
  const words = (RANGE_INFO[panel.range] || {}).words || panel.range;
  box.className = `text-sm tabular-nums font-semibold whitespace-nowrap ${parts.cls}`;
  const visible = el("span", "", `${parts.text} over ${panel.range}`);
  visible.setAttribute("aria-hidden", "true");
  box.replaceChildren(visible, el("span", "sr-only", `${parts.label} over ${words}`));
}

async function showPrices(ticker) {
  const message = document.getElementById("panel-chart-message");
  if (!TICKER_RE.test(ticker)) {
    chartPlaceholder(null);
    panelMessage(message, "No chart: the ticker symbol is not valid.");
    return;
  }
  const source = RANGE_INFO[panel.range].source;   // download what the selected range needs
  chartPlaceholder(el("span", "block h-64 animate-pulse rounded-lg bg-slate-200 dark:bg-slate-700"));
  message.setAttribute("aria-busy", "true");
  try {
    const url = `/api/stock/${encodeURIComponent(ticker)}/prices?${PRICE_SOURCES[source]}`;
    const data = await cachedFetch(pricesCache, `${ticker} ${source}`, url);
    if (ticker !== panel.ticker) return;
    // The user switched to a range that needs the other download meanwhile: that call draws.
    if (RANGE_INFO[panel.range].source !== source) return;
    const prices = Array.isArray(data.prices) ? data.prices : [];
    // If the stats call failed, the prices answer still carries the company name.
    const nameEl = document.getElementById("panel-name");
    if (!nameEl.textContent && data.name) nameEl.textContent = data.name;
    const withClose = prices.filter((p) => isNumber(p.close)).length;   // counting points, not math
    if (withClose < 2) {
      chartPlaceholder(null);
      panelMessage(message, `Not enough price history to draw a chart (${withClose} day${withClose === 1 ? "" : "s"} saved).`);
      return;
    }
    panel.data[source] = {
      prices,
      ranges: data.ranges && typeof data.ranges === "object" ? data.ranges : null,
      step: isNumber(data.step) ? data.step : 1,
      daysCovered: isNumber(data.days_covered) ? data.days_covered : null,
    };
    drawPriceChart();
  } catch (error) {
    if (ticker !== panel.ticker || RANGE_INFO[panel.range].source !== source) return;
    chartPlaceholder(null);
    const retry = error.status ? null : () => showPrices(ticker);
    panelMessage(message, panelErrorText(error, `No saved price history for ${ticker} yet.`), retry);
  } finally {
    message.removeAttribute("aria-busy");
  }
}

function drawPriceChart() {
  const info = RANGE_INFO[panel.range];
  const data = panel.data[info.source];
  const range = data.ranges ? data.ranges[panel.range] : null;
  const closes = range && isNumber(range.closes) ? range.closes : info.closes;
  const weekly = data.step > 1;
  // Daily: 1M = the last 22 closes (21 daily moves), like month_pct. Weekly: Python already
  // picked the 5Y window and thinned it, so draw every point it sent.
  const points = weekly ? data.prices : data.prices.slice(-closes);
  // Daily closes behind the line (from the API when thinned; a plain count otherwise).
  const saved = weekly && isNumber(data.daysCovered) ? data.daysCovered : points.length;
  const box = document.getElementById("panel-chart-box");
  const message = document.getElementById("panel-chart-message");
  const note = document.getElementById("panel-chart-note");
  document.getElementById("panel-chart-step").hidden = !weekly;

  destroyPriceChart();   // never stack a new chart on top of an old one
  if (typeof Chart === "undefined") {
    chartPlaceholder(null);
    panelMessage(message, "The chart library failed to load. The numbers above are still correct.");
    return;
  }
  message.replaceChildren();
  box.hidden = false;
  renderRangeChange(range);

  // Fewer saved days than the range asks for: say so instead of pretending.
  note.hidden = saved >= closes;
  note.textContent = `Only ${saved} trading days saved so far.`;

  const canvas = document.getElementById("price-chart");
  const first = points[0].date;
  const last = points[points.length - 1].date;
  canvas.setAttribute("aria-label",
    weekly
      ? `Line chart of ${panel.ticker} closing prices, one point every 5 trading days (${points.length} points covering ${saved} trading days), from ${prettyDate(first)} to ${prettyDate(last)}.`
      : `Line chart of ${panel.ticker} daily closing prices over ${points.length} trading days, from ${prettyDate(first)} to ${prettyDate(last)}.`);

  const colors = chartColors();
  const dark = document.documentElement.classList.contains("dark");
  const line = dark ? "#818cf8" : "#4f46e5";   // indigo-400 / indigo-600: neutral, not "good" or "bad"

  priceChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: points.map((p) => shortDate(p.date, info.years)),
      datasets: [{
        label: "Close",
        data: points.map((p) => (isNumber(p.close) ? p.close : null)),
        borderColor: line,
        backgroundColor: line,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0,          // straight segments: do not invent a smooth curve between closes
        spanGaps: false,     // a missing day stays a visible gap
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { mode: "index", intersect: false },   // hover anywhere above a date
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => prettyDate(points[items[0].dataIndex].date),
            label: (ctx) => `Close ${money(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: colors.grid }, border: { color: colors.grid },
          ticks: { color: colors.text, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
        },
        y: {
          grid: { color: colors.grid }, border: { color: colors.grid },
          ticks: { color: colors.text, callback: (value) => money(value) },
        },
      },
    },
  });
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
    if (priceChart && panel.data[RANGE_INFO[panel.range].source]) drawPriceChart();  // redraw the panel chart in the new colours
  });
}

// ---------- Ask the AI (milestone 5) ----------
// Two boxes (the global one and the one in the company panel) share this code.
// The answer is untrusted model output: it only ever goes on the page through textContent.

const ASK_MAX_CHARS = 500;          // same limit as the API
const ASK_TIMEOUT_MS = 190000;      // a bit longer than the server's 180 s, so its clear 504 message arrives first
const ASK_WAIT_NOTE = "The local model usually takes 1 to 3 minutes.";
const askBoxes = [];
let askBusy = false;                // the server answers one question at a time, so the page does too
let panelAskBox = null;

/** Find a box's elements by their id prefix ("ask" or "panel-ask"). */
function askElements(prefix) {
  const get = (name) => document.getElementById(`${prefix}-${name}`);
  return {
    form: get("form"), label: get("label"), input: get("input"), count: get("count"), button: get("button"),
    busyNote: get("busy-note"), status: get("status"), waiting: get("waiting"), error: get("error"),
    result: get("result"), answer: get("answer"), meta: get("meta"), tools: get("tools"),
  };
}

function setupAskBox(prefix, getTicker) {
  const box = { ...askElements(prefix), getTicker, token: 0 };
  box.input.addEventListener("input", () => updateAskCount(box));
  // Enter sends; Shift+Enter makes a new line. isComposing: don't send halfway through typing Japanese/Chinese etc.
  box.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      box.form.requestSubmit();
    }
  });
  box.form.addEventListener("submit", (event) => {
    event.preventDefault();
    askQuestion(box);
  });
  askBoxes.push(box);
  return box;
}

function updateAskCount(box) {
  box.count.textContent = `${box.input.value.length} / ${ASK_MAX_CHARS}`;
}

/** Disable every Ask button while one question runs; the box that asked says "Thinking…". */
function setAskBusy(busy, owner) {
  askBusy = busy;
  for (const box of askBoxes) {
    box.button.disabled = busy;
    box.button.textContent = busy && box === owner ? "Thinking…" : "Ask";
    box.busyNote.hidden = !busy || box === owner;
  }
}

function clearAskOutput(box) {
  box.status.textContent = "";
  box.error.textContent = "";
  box.waiting.hidden = true;
  box.result.hidden = true;
  box.answer.textContent = "";
  box.meta.textContent = "";
  box.tools.textContent = "";
}

/** "get_movers(period=week, direction=losers, n=5)" from {name, input}. */
function toolLabel(tool) {
  const input = tool && typeof tool.input === "object" && tool.input !== null ? tool.input : {};
  const args = Object.entries(input).map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`);
  return `${tool && tool.name ? tool.name : "unknown"}(${args.join(", ")})`;
}

/** Friendly message for each way asking can fail. */
function askErrorText(error) {
  if (error.name === "AbortError") return `No answer after ${ASK_TIMEOUT_MS / 1000} seconds. The model may still be busy; try again in a moment.`;
  if (error.status === 429) return error.message;   // the server's "busy, try again" text
  if (error.status === 504) return "Took too long (over 3 minutes). Try a shorter, more specific question.";
  if (error.status === 503) return error.message;   // Ollama not running / model not installed / no data yet
  if (error.status === 422) return `Type a question (1 to ${ASK_MAX_CHARS} characters).`;
  if (error.status === 404) return error.message;   // unknown ticker
  if (error.status) return `Something went wrong (HTTP ${error.status}). Try again.`;
  return "Could not reach the API. Is uvicorn running?";
}

function renderAnswer(box, data) {
  box.answer.textContent = data.answer ? String(data.answer) : "(The model returned an empty answer.)";
  const parts = [];
  if (data.as_of) parts.push(`Data as of ${prettyDate(data.as_of)}`);
  if (isNumber(data.seconds)) parts.push(`answered in ${data.seconds.toFixed(1)} s`);
  box.meta.textContent = parts.join(" · ");
  const tools = Array.isArray(data.tools_used) ? data.tools_used : [];
  box.tools.textContent = tools.length
    ? `Tools used: ${tools.map(toolLabel).join(", ")}`
    : "Tools used: none, so any numbers in this answer did not come from the data.";
  box.result.hidden = false;
}

async function askQuestion(box) {
  if (askBusy) return;
  const question = box.input.value.trim();
  clearAskOutput(box);
  if (!question || question.length > ASK_MAX_CHARS) {
    box.error.textContent = `Type a question (1 to ${ASK_MAX_CHARS} characters).`;
    box.input.focus();
    return;
  }
  const ticker = box.getTicker();
  const token = ++box.token;   // if the panel switches stock meanwhile, this answer is dropped
  setAskBusy(true, box);
  box.status.textContent = `Asking the local model. ${ASK_WAIT_NOTE}`;
  const started = Date.now();
  box.waiting.textContent = "Waiting 0 s";
  box.waiting.hidden = false;
  const timer = setInterval(() => {
    box.waiting.textContent = `Waiting ${Math.round((Date.now() - started) / 1000)} s`;
  }, 1000);

  try {
    const body = ticker ? { question, ticker } : { question };
    const data = await fetchJson("/api/ask", { method: "POST", body, timeoutMs: ASK_TIMEOUT_MS });
    if (token !== box.token) return;
    renderAnswer(box, data);
    box.status.textContent = "Answer ready.";
  } catch (error) {
    if (token !== box.token) return;
    box.status.textContent = "";
    box.error.textContent = askErrorText(error);
  } finally {
    clearInterval(timer);
    if (token === box.token) box.waiting.hidden = true;
    const focusLost = document.activeElement === document.body || document.activeElement === box.button;
    setAskBusy(false, box);
    // A disabled button drops keyboard focus; put it somewhere useful instead of the top of the page.
    if (focusLost && token === box.token) (box.result.hidden ? box.input : box.answer).focus();
  }
}

/** The panel shows a new stock (or closes): new label, empty box, and any late answer is dropped. */
function resetPanelAsk(ticker) {
  if (!panelAskBox) return;
  panelAskBox.token += 1;
  clearAskOutput(panelAskBox);
  panelAskBox.input.value = "";
  updateAskCount(panelAskBox);
  const valid = typeof ticker === "string" && TICKER_RE.test(ticker);
  document.getElementById("panel-ask").hidden = !valid;
  if (valid) {
    panelAskBox.label.textContent = `Ask about ${ticker}`;
    panelAskBox.input.placeholder = `For example "How did ${ticker} do this month?"`;
  }
}

function setupAsk() {
  setupAskBox("ask", () => null);
  panelAskBox = setupAskBox("panel-ask", () => panel.ticker);
}

// ---------- start ----------

async function init() {
  setupTheme();
  setupTabs();
  setupDirection();
  setupPanel();
  setupAsk();   // before route(): a shared #/stock/TICKER link fills in the panel's Ask box
  route();  // a reload or shared link with #/stock/TICKER opens the panel straight away
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
