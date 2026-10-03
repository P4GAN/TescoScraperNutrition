"use strict";

// Tesco Protein Finder: loads data.json (built by scraper/build.py), filters and ranks in memory.

const TESCO_PRODUCT_URL = "https://www.tesco.com/groceries/en-GB/products/";
const PAGE_SIZE = 200;
const DEFAULTS = {
  q: "", cat: "", dept: "", aisle: "", price: "shelf", multibuy: true, share: 25,
  salt: "", sat: "", sugar: "",
  hideSuspect: true, hideUnranked: true, hideEstimated: false, hideCooked: false, hideUnavailable: false,
  bestOnly: false, sort: "gpp", dir: "desc",
};
// Columns that sort ascending first (cheaper is better).
const ASCENDING_FIRST = new Set(["title", "priceActive", "ppkActive", "pp100", "sat", "sugar", "salt"]);

const $ = (id) => document.getElementById(id);

let data;            // data.json as loaded
let F;               // flag bits by name
let products = [];   // every product, as objects
let state = { ...DEFAULTS };
let view = [];       // filtered + sorted products
let shown = PAGE_SIZE;
let chart = null;
let highlighted = null;

main().catch((error) => {
  console.error(error);
  $("summary").textContent = "Couldn't load the product data. Try reloading the page.";
});

async function main() {
  const response = await fetch("data.json");
  if (!response.ok) throw new Error(`data.json: HTTP ${response.status}`);
  data = await response.json();
  F = data.flags;
  F.suspect = F.bad_macros | F.bad_kcal | F.bad_price;
  products = data.rows.map(toProduct);

  state = { ...DEFAULTS, ...readUrl() };
  fillSelect($("cat"), data.categories);
  writeControls();
  bindControls();
  writeDataNote();
  update();
}

function toProduct(row) {
  const p = {};
  data.columns.forEach((column, i) => { p[column] = row[i]; });
  p.share = p.kcal > 0 ? (4 * p.protein) / p.kcal : null;
  p.searchText = `${p.title} ${data.aisles[p.aisle]} ${data.departments[p.dept]}`.toLowerCase();
  return p;
}

// ------------------------------------------------------------------ state <-> controls <-> URL

function readControls() {
  const number = (id) => $(id).value.trim();
  state = {
    ...state,
    q: $("q").value.trim(),
    cat: $("cat").value,
    dept: $("dept").value,
    aisle: $("aisle").value,
    price: document.querySelector('input[name="price"]:checked').value,
    multibuy: $("multibuy").checked,
    share: Number($("share").value),
    salt: number("salt"),
    sat: number("sat"),
    sugar: number("sugar"),
    hideSuspect: $("hide-suspect").checked,
    hideUnranked: $("hide-unranked").checked,
    hideEstimated: $("hide-estimated").checked,
    hideCooked: $("hide-cooked").checked,
    hideUnavailable: $("hide-unavailable").checked,
    bestOnly: $("best-only").checked,
  };
}

function writeControls() {
  $("q").value = state.q;
  $("cat").value = state.cat;
  refreshDepartmentOptions();
  $("dept").value = state.dept;
  refreshAisleOptions();
  $("aisle").value = state.aisle;
  document.querySelector(`input[name="price"][value="${state.price}"]`).checked = true;
  $("multibuy").checked = state.multibuy;
  $("share").value = state.share;
  $("salt").value = state.salt;
  $("sat").value = state.sat;
  $("sugar").value = state.sugar;
  $("hide-suspect").checked = state.hideSuspect;
  $("hide-unranked").checked = state.hideUnranked;
  $("hide-estimated").checked = state.hideEstimated;
  $("hide-cooked").checked = state.hideCooked;
  $("hide-unavailable").checked = state.hideUnavailable;
  $("best-only").checked = state.bestOnly;
}

function bindControls() {
  let searchTimer;
  $("q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(onChange, 150);
  });
  $("cat").addEventListener("change", () => {
    state.cat = $("cat").value;
    refreshDepartmentOptions();
    refreshAisleOptions();
    onChange();
  });
  $("dept").addEventListener("change", () => {
    state.dept = $("dept").value;
    refreshAisleOptions();
    onChange();
  });
  $("share").addEventListener("input", onChange);
  for (const id of ["aisle", "multibuy", "salt", "sat", "sugar", "hide-suspect", "hide-unranked",
    "hide-estimated", "hide-cooked", "hide-unavailable", "best-only"]) {
    $(id).addEventListener(id === "salt" || id === "sat" || id === "sugar" ? "input" : "change", onChange);
  }
  for (const radio of document.querySelectorAll('input[name="price"]')) radio.addEventListener("change", onChange);

  $("reset").addEventListener("click", () => {
    state = { ...DEFAULTS, sort: state.sort, dir: state.dir };
    writeControls();
    update();
  });
  $("more").addEventListener("click", () => {
    shown += PAGE_SIZE;
    renderTable();
  });
  for (const th of document.querySelectorAll("th[data-sort]")) {
    th.tabIndex = 0;
    const sortBy = () => {
      const key = th.dataset.sort;
      if (state.sort === key) state.dir = state.dir === "asc" ? "desc" : "asc";
      else { state.sort = key; state.dir = ASCENDING_FIRST.has(key) ? "asc" : "desc"; }
      update();
    };
    th.addEventListener("click", sortBy);
    th.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); sortBy(); }
    });
  }
  $("rows").addEventListener("pointerover", (event) => {
    const row = event.target.closest("tr[data-index]");
    highlight(row ? view[Number(row.dataset.index)] : null);
  });
  $("rows").addEventListener("pointerleave", () => highlight(null));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    chart?.destroy();
    chart = null;
    renderChart();
  });
}

function onChange() {
  readControls();
  update();
}

const URL_KEYS = Object.keys(DEFAULTS);

function readUrl() {
  const params = new URLSearchParams(location.search);
  const out = {};
  for (const key of URL_KEYS) {
    if (!params.has(key)) continue;
    const value = params.get(key);
    const fallback = DEFAULTS[key];
    out[key] = typeof fallback === "boolean" ? value === "1"
      : typeof fallback === "number" ? Number(value) || 0
        : value;
  }
  return out;
}

function writeUrl() {
  const params = new URLSearchParams();
  for (const key of URL_KEYS) {
    const value = state[key];
    if (value === DEFAULTS[key]) continue;
    params.set(key, typeof value === "boolean" ? (value ? "1" : "0") : String(value));
  }
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}

// ------------------------------------------------------------------ select options

function fillSelect(select, names, counts) {
  const first = select.options[0];
  select.replaceChildren(first);
  for (const name of names) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = counts ? `${name} (${counts.get(name)})` : name;
    select.append(option);
  }
}

function inCategory(p) {
  return !state.cat || (p.cat & (1 << data.categories.indexOf(state.cat))) !== 0;
}

function namesWithCounts(filter, nameOf) {
  const counts = new Map();
  for (const p of products) {
    if (!filter(p)) continue;
    const name = nameOf(p);
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  return { names: [...counts.keys()].sort((a, b) => a.localeCompare(b)), counts };
}

function refreshDepartmentOptions() {
  const { names, counts } = namesWithCounts(inCategory, (p) => data.departments[p.dept]);
  fillSelect($("dept"), names, counts);
  if (!names.includes(state.dept)) state.dept = "";
  $("dept").value = state.dept;
}

function refreshAisleOptions() {
  const { names, counts } = namesWithCounts(
    (p) => inCategory(p) && (!state.dept || data.departments[p.dept] === state.dept),
    (p) => data.aisles[p.aisle],
  );
  fillSelect($("aisle"), names, counts);
  if (!names.includes(state.aisle)) state.aisle = "";
  $("aisle").value = state.aisle;
}

// ------------------------------------------------------------------ filter, rank, frontier

function applyPrice(p) {
  const useClubcard = state.price === "clubcard" && p.ccppk != null && (state.multibuy || !(p.flags & F.multibuy));
  p.onClubcard = useClubcard;
  p.priceActive = useClubcard && p.cc != null ? p.cc : p.price;
  p.ppkActive = useClubcard ? p.ccppk : p.ppk;
  p.gpp = p.ppkActive ? (p.protein * 10) / p.ppkActive : null;
  p.pp100 = p.gpp ? 100 / p.gpp : null;
}

function matches(p, words, limits) {
  if (state.hideSuspect && p.flags & F.suspect) return false;
  if (state.hideUnranked && p.gpp == null) return false;
  if (state.hideEstimated && p.flags & F.est_weight) return false;
  if (state.hideCooked && p.flags & F.cooked) return false;
  if (state.hideUnavailable && p.flags & F.not_for_sale) return false;
  if (!inCategory(p)) return false;
  if (state.dept && data.departments[p.dept] !== state.dept) return false;
  if (state.aisle && data.aisles[p.aisle] !== state.aisle) return false;
  if (state.share > 0 && !(p.share >= state.share / 100)) return false;
  for (const [key, max] of limits) if (p[key] != null && p[key] > max) return false;
  for (const word of words) if (!p.searchText.includes(word)) return false;
  return true;
}

// Products that no other product beats on both protein per £ and protein share of calories.
function markFrontier(list) {
  const ranked = list.filter((p) => p.gpp != null && p.share != null);
  ranked.sort((a, b) => Math.min(b.share, 1) - Math.min(a.share, 1) || b.gpp - a.gpp);
  let bestGpp = -Infinity;
  for (const p of ranked) {
    p.best = p.gpp > bestGpp;
    if (p.best) bestGpp = p.gpp;
  }
}

function compare(key, dir) {
  const sign = dir === "asc" ? 1 : -1;
  return (a, b) => {
    const x = a[key], y = b[key];
    if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;  // blanks last
    if (typeof x === "string") return sign * x.localeCompare(y);
    return sign * (x - y) || (b.gpp ?? -1) - (a.gpp ?? -1);
  };
}

function update() {
  const words = state.q.toLowerCase().split(/\s+/).filter(Boolean);
  const limits = ["salt", "sat", "sugar"]
    .filter((key) => state[key] !== "" && !Number.isNaN(Number(state[key])))
    .map((key) => [key, Number(state[key])]);

  for (const p of products) { applyPrice(p); p.best = false; }
  let list = products.filter((p) => matches(p, words, limits));
  markFrontier(list);
  const frontierSize = list.filter((p) => p.best).length;
  if (state.bestOnly) list = list.filter((p) => p.best);
  list.sort(compare(state.sort, state.dir));

  view = list;
  shown = PAGE_SIZE;
  $("multibuy-wrap").hidden = state.price !== "clubcard";
  $("share-out").textContent = `${state.share}%`;
  renderSummary(list.length, frontierSize);
  renderTable();
  renderChart();
  writeUrl();
}

// ------------------------------------------------------------------ rendering

const fmtMoney = (v) => (v < 0.995 ? `${Math.round(v * 100)}p` : `£${v.toFixed(2)}`);
const fmtPpk = (v) => (v >= 100 ? `£${Math.round(v)}` : `£${v.toFixed(2)}`);
const fmt1 = (v) => (v == null ? "–" : v >= 100 ? String(Math.round(v)) : v.toFixed(1));

function fmtWeight(p) {
  if (!p.kg) return "";
  const liquid = p.flags & F.liquid;
  if (p.kg < 1) return `${Math.round(p.kg * 1000)}${liquid ? " ml" : " g"}`;
  return `${+p.kg.toFixed(2)}${liquid ? " L" : " kg"}`;
}

function renderSummary(count, frontierSize) {
  const summary = $("summary");
  summary.replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = count.toLocaleString("en-GB");
  summary.append(strong, ` product${count === 1 ? "" : "s"} match`);
  if (!state.bestOnly && count) {
    summary.append(`, ${frontierSize} of them on the best-value frontier`);
  }
  summary.append(`. Ranked by ${state.price === "clubcard" ? "Clubcard" : "shelf"} price.`);
}

function badgesFor(p) {
  const badges = [];
  if (p.flags & F.suspect) {
    const reasons = [];
    if (p.flags & F.bad_macros) reasons.push("the macros add up to more than 100 g");
    if (p.flags & F.bad_kcal) reasons.push("the kcal don't match protein, carbs and fat");
    if (p.flags & F.bad_price) reasons.push("the price per kg looks wrong");
    badges.push(["check data", `Suspect label data: ${reasons.join("; ")}.`, true]);
  }
  if (p.flags & F.est_weight) badges.push(["~weight", "Tesco gives no pack weight; it was estimated from the label's serving size or the title."]);
  if (p.flags & F.cooked) badges.push(["cooked values", "The label gives values for the cooked or prepared food, so protein per £ may be off."]);
  if (p.flags & F.scaled) badges.push(["per serving", "The label has no per 100 g column; values were scaled from a per-serving column."]);
  if (p.onClubcard && p.flags & F.multibuy) badges.push(["multibuy", "The Clubcard price needs a multibuy; shown per item."]);
  if (p.flags & F.not_for_sale) badges.push(["unavailable", "Listed but currently not for sale."]);
  return badges;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function productCell(p) {
  const td = el("td", "col-product");
  const wrap = el("div", "product");
  td.append(wrap);
  if (p.img) {
    const img = el("img", "thumb");
    img.src = `${data.imagePrefix}${p.img}?h=90&w=90`;
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    wrap.append(img);
  } else {
    wrap.append(el("span", "thumb thumb-empty"));
  }
  const text = el("div");
  const link = el("a", "product-name", p.title);
  link.href = TESCO_PRODUCT_URL + p.tpnc;
  link.target = "_blank";
  link.rel = "noopener";
  text.append(link);
  const badges = badgesFor(p);
  if (badges.length) {
    const wrap = el("span", "badges");
    for (const [label, title, warn] of badges) {
      const badge = el("span", warn ? "badge badge-warn" : "badge", label);
      badge.title = title;
      wrap.append(badge);
    }
    text.append(wrap);
  }
  const meta = [data.aisles[p.aisle], fmtWeight(p)].filter(Boolean).join(" · ");
  text.append(el("div", "product-meta", meta));
  wrap.append(text);
  return td;
}

function priceCell(p) {
  const td = el("td", "num");
  td.append(el("span", p.onClubcard ? "secondary" : "", fmtMoney(p.price)));
  if (p.cc != null) {
    const cc = el("span", p.onClubcard ? "cc-price active" : "cc-price", `${fmtMoney(p.cc)} Clubcard`);
    cc.title = data.promos[p.promo] || "";
    td.append(cc);
  }
  return td;
}

function renderTable() {
  const tbody = $("rows");
  const fragment = document.createDocumentFragment();
  const end = Math.min(shown, view.length);
  for (let i = 0; i < end; i++) {
    const p = view[i];
    const tr = el("tr", p.best ? "best" : "");
    tr.dataset.index = i;
    tr.append(
      productCell(p),
      el("td", "num metric", p.gpp == null ? "–" : fmt1(p.gpp)),
      el("td", "num", p.share == null ? "–" : `${Math.round(p.share * 100)}%`),
      priceCell(p),
      el("td", "num", p.ppkActive ? fmtPpk(p.ppkActive) : "–"),
      el("td", "num", fmt1(p.protein)),
      el("td", "num", p.pp100 == null ? "–" : fmtMoney(p.pp100)),
      el("td", "num secondary", p.kcal == null ? "–" : String(Math.round(p.kcal))),
      el("td", "num secondary", fmt1(p.sat)),
      el("td", "num secondary", fmt1(p.sugar)),
      el("td", "num secondary", fmt1(p.fibre)),
      el("td", "num secondary", fmt1(p.salt)),
    );
    fragment.append(tr);
  }
  if (!view.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No products match. Try a lower protein share or clearing the search.");
    td.colSpan = 12;
    tr.append(td);
    fragment.append(tr);
  }
  tbody.replaceChildren(fragment);

  for (const th of document.querySelectorAll("th[data-sort]")) {
    if (th.dataset.sort === state.sort) th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
  }
  const more = $("more");
  more.hidden = view.length <= shown;
  more.textContent = `Show ${Math.min(PAGE_SIZE, view.length - shown).toLocaleString("en-GB")} more`
    + ` (${(view.length - shown).toLocaleString("en-GB")} left)`;
}

// ------------------------------------------------------------------ chart

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function point(p) {
  return { x: Math.min(p.share, 1) * 100, y: p.gpp, p };
}

function renderChart() {
  if (!window.Chart) return;  // CDN blocked: the table still works
  const plotted = view.filter((p) => p.gpp != null && p.share != null);
  const best = plotted.filter((p) => p.best).sort((a, b) => a.share - b.share).map(point);
  const others = plotted.filter((p) => !p.best).map(point);
  if (!chart) chart = createChart();
  chart.data.datasets[1].data = best;
  chart.data.datasets[2].data = others;
  chart.data.datasets[0].data = highlighted && highlighted.gpp != null && highlighted.share != null ? [point(highlighted)] : [];
  chart.update("none");
}

function highlight(p) {
  if (p === highlighted) return;
  highlighted = p;
  if (!chart) return;
  chart.data.datasets[0].data = p && p.gpp != null && p.share != null ? [point(p)] : [];
  chart.update("none");
}

function createChart() {
  const surface = cssVar("--surface");
  const accent = cssVar("--accent");
  const muted = cssVar("--muted");
  const grid = cssVar("--grid");
  const axis = cssVar("--axis");
  Chart.defaults.font.family = cssVar("--font");
  Chart.defaults.color = muted;

  return new Chart($("chart"), {
    type: "scatter",
    data: {
      datasets: [
        {
          label: "Hovered in table", data: [], order: 0,
          pointRadius: 7, pointHoverRadius: 7, pointBackgroundColor: cssVar("--highlight"),
          pointBorderColor: surface, pointBorderWidth: 2,
        },
        {
          label: "Best value", data: [], order: 1, showLine: true,
          borderColor: accent, borderWidth: 2, pointRadius: 4, pointHoverRadius: 6,
          pointBackgroundColor: accent, pointBorderColor: surface, pointBorderWidth: 2,
        },
        {
          label: "Other matching products", data: [], order: 2,
          pointRadius: 2.5, pointHoverRadius: 5, pointBackgroundColor: cssVar("--other"),
          pointBorderWidth: 0, pointHoverBackgroundColor: muted,
        },
      ],
    },
    options: {
      animation: false,
      parsing: false,
      normalized: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false, axis: "xy" },
      onClick: (_event, elements) => {
        const p = elements[0] && chart.data.datasets[elements[0].datasetIndex].data[elements[0].index].p;
        if (p) window.open(TESCO_PRODUCT_URL + p.tpnc, "_blank", "noopener");
      },
      scales: {
        x: {
          type: "linear", min: 0, max: 100,
          title: { display: true, text: "Protein share of calories" },
          ticks: { callback: (v) => `${v}%`, stepSize: 10, maxRotation: 0, autoSkipPadding: 12 },
          grid: { color: grid }, border: { color: axis },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: "Grams of protein per £" },
          grid: { color: grid }, border: { color: axis },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: surface,
          borderColor: axis,
          borderWidth: 1,
          titleColor: cssVar("--text"),
          bodyColor: cssVar("--text-2"),
          padding: 10,
          displayColors: false,
          callbacks: {
            title: (items) => items[0].raw.p.title,
            label: (item) => {
              const p = item.raw.p;
              return [
                `${fmt1(p.gpp)} g protein per £ · ${Math.round(p.share * 100)}% of kcal from protein`,
                `${fmtMoney(p.priceActive)}${p.onClubcard ? " Clubcard" : ""} · ${fmtPpk(p.ppkActive)}/kg · ${fmt1(p.protein)} g protein/100g`,
              ];
            },
          },
        },
      },
    },
  });
}

function writeDataNote() {
  const note = $("data-note");
  const excluded = data.excluded["no nutrition"] || 0;
  note.textContent = `Data as of ${new Date(data.scraped).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}: `
    + `${products.length.toLocaleString("en-GB")} products from Tesco's Fresh Food, Bakery, Frozen Food, Food Cupboard `
    + `and Treats & Snacks categories. ${excluded.toLocaleString("en-GB")} more have no nutrition label and aren't shown.`;
}
