"use strict";

const PAGE_SIZE = 40;
const STORAGE_KEY = "idsignpost:filters:v1";
const DATA_CANDIDATES = [
  "./idsignpost.json",
  "../../../idsignpost.json",
  "https://raw.githubusercontent.com/charlesroper/FSCLegacy-fscbiodiversity.uk-idsignpost/main/idsignpost.json",
];

const state = {
  entries: [],
  view: "working",
  q: "",
  freeOnly: false,
  format: "",
  sort: "relevance",
  limit: PAGE_SIZE,
};

function isHttpUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

function isWorking(entry) {
  return entry.web_link_state === "ok" || Boolean(entry.alternative_link);
}

function isRecovered(entry) {
  return entry.web_link_state === "broken" && Boolean(entry.alternative_link);
}

function isBrokenNoAlt(entry) {
  return entry.web_link_state === "broken" && !entry.alternative_link;
}

function primaryLink(entry) {
  if (entry.web_link_state === "ok" && isHttpUrl(entry.web_link)) {
    return { url: entry.web_link, kind: "original", label: "Open guide" };
  }
  if (isHttpUrl(entry.alternative_link)) {
    return { url: entry.alternative_link, kind: "alternative", label: "Open replacement" };
  }
  return null;
}

function urlHost(value) {
  if (!isHttpUrl(value)) return "";
  try {
    return new URL(value).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function buildSearchBlob(entry) {
  return [
    entry.title,
    entry.authors,
    entry.keywords,
    entry.description,
    entry.comment_body,
    Array.isArray(entry.available_as) ? entry.available_as.join(" ") : "",
    urlHost(entry.web_link),
    urlHost(entry.alternative_link),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function scoreEntry(entry, tokens) {
  if (tokens.length === 0) return 0;
  const title = (entry.title || "").toLowerCase();
  const meta = [entry.authors || "", entry.keywords || ""].join(" ").toLowerCase();
  let score = 0;
  for (const tok of tokens) {
    if (title.startsWith(tok)) score += 6;
    else if (title.includes(tok)) score += 4;
    if (meta.includes(tok)) score += 2;
    score += 1;
  }
  return score;
}

function readFiltersFromLocation() {
  const search = window.location.search;
  let params = new URLSearchParams(search);
  if (!search) {
    try {
      const saved = window.sessionStorage.getItem(STORAGE_KEY);
      if (saved) params = new URLSearchParams(saved);
    } catch {
      // Ignore storage errors in strict privacy modes.
    }
  }
  const view = params.get("view");
  if (view === "working" || view === "recovered" || view === "all" || view === "broken") {
    state.view = view;
  }
  state.q = params.get("q") || "";
  state.freeOnly = params.get("free") === "1";
  state.format = params.get("format") || "";
  const sort = params.get("sort");
  if (sort === "relevance" || sort === "newest" || sort === "confidence") {
    state.sort = sort;
  }
}

function syncLocationWithFilters() {
  const params = new URLSearchParams();
  if (state.q.trim()) params.set("q", state.q.trim());
  if (state.view !== "working") params.set("view", state.view);
  if (state.freeOnly) params.set("free", "1");
  if (state.format) params.set("format", state.format);
  if (state.sort !== "relevance") params.set("sort", state.sort);

  const queryString = params.toString();
  const targetUrl = queryString
    ? `${window.location.pathname}?${queryString}`
    : window.location.pathname;
  if (window.location.pathname + window.location.search !== targetUrl) {
    window.history.replaceState(null, "", targetUrl);
  }
  try {
    if (queryString) window.sessionStorage.setItem(STORAGE_KEY, queryString);
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Ignore storage errors.
  }
}

function applyControlValues() {
  const searchInput = document.getElementById("search-input");
  const clearBtn = document.getElementById("clear-btn");
  const freeToggle = document.getElementById("free-toggle");
  const sortSelect = document.getElementById("sort-select");

  searchInput.value = state.q;
  clearBtn.hidden = !state.q;
  freeToggle.setAttribute("aria-pressed", String(state.freeOnly));
  sortSelect.value = state.sort;

  for (const btn of document.querySelectorAll(".tab-btn")) {
    const active = btn.dataset.view === state.view;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  }

  for (const chip of document.querySelectorAll("[data-format]")) {
    chip.setAttribute("aria-pressed", String(chip.dataset.format === state.format));
  }
}

function filterEntries() {
  const tokens = state.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const matches = [];

  for (const entry of state.entries) {
    if (state.view === "working" && !isWorking(entry)) continue;
    if (state.view === "recovered" && !isRecovered(entry)) continue;
    if (state.view === "broken" && !isBrokenNoAlt(entry)) continue;
    if (state.freeOnly && !entry.is_free) continue;
    if (state.format && !(entry.available_as || []).includes(state.format)) continue;
    if (tokens.length > 0 && !tokens.every((tok) => entry._search.includes(tok))) continue;
    matches.push({ entry, score: scoreEntry(entry, tokens) });
  }

  matches.sort((a, b) => {
    if (state.sort === "newest") {
      return (b.entry.submitted_at || "").localeCompare(a.entry.submitted_at || "");
    }
    if (state.sort === "confidence") {
      return (b.entry.alternative_probability || -1) - (a.entry.alternative_probability || -1)
        || a.entry.title.localeCompare(b.entry.title);
    }
    if (tokens.length > 0 && b.score !== a.score) {
      return b.score - a.score;
    }
    return a.entry.title.localeCompare(b.entry.title);
  });

  return matches.map((item) => item.entry);
}

function makeBadge(text, className) {
  const span = document.createElement("span");
  span.className = className ? `badge ${className}` : "badge";
  span.textContent = text;
  return span;
}

function createCard(entry) {
  const article = document.createElement("article");
  article.className = "card";

  const title = document.createElement("h2");
  title.className = "card-title";
  title.textContent = entry.title || entry.id;
  article.appendChild(title);

  const bylineParts = [];
  if (entry.authors) bylineParts.push(entry.authors);
  if (entry.submitted_at) bylineParts.push(entry.submitted_at.slice(0, 4));
  if (bylineParts.length > 0) {
    const byline = document.createElement("p");
    byline.className = "card-byline";
    byline.textContent = bylineParts.join(" · ");
    article.appendChild(byline);
  }

  const badges = document.createElement("div");
  badges.className = "badge-row";
  if (entry.web_link_state === "ok") {
    badges.appendChild(makeBadge("Live link", "badge-ok"));
  } else if (entry.alternative_link) {
    badges.appendChild(makeBadge("Recovered link", "badge-alt"));
  } else if (entry.web_link_state === "broken") {
    badges.appendChild(makeBadge("Link broken", "badge-warn"));
  } else if (entry.web_link_state === "blocked") {
    badges.appendChild(makeBadge("Unverified"));
  } else {
    badges.appendChild(makeBadge("No web link"));
  }
  badges.appendChild(makeBadge(entry.is_free ? "Free" : "Paid"));
  for (const fmt of entry.available_as || []) {
    badges.appendChild(makeBadge(fmt));
  }
  article.appendChild(badges);

  if (entry.description) {
    const desc = document.createElement("p");
    desc.className = "card-desc";
    desc.textContent = entry.description.length > 240
      ? `${entry.description.slice(0, 237)}...`
      : entry.description;
    article.appendChild(desc);
  }

  if (entry.keywords) {
    const kw = document.createElement("p");
    kw.className = "card-keywords";
    kw.textContent = `Keywords: ${entry.keywords}`;
    article.appendChild(kw);
  }

  const actions = document.createElement("div");
  actions.className = "card-actions";
  const mainLink = primaryLink(entry);
  if (mainLink) {
    const a = document.createElement("a");
    a.className = "action-btn primary";
    a.href = mainLink.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = mainLink.label;
    actions.appendChild(a);
  }
  if (isHttpUrl(entry.wayback_link)) {
    const wb = document.createElement("a");
    wb.className = "action-btn";
    wb.href = entry.wayback_link;
    wb.target = "_blank";
    wb.rel = "noopener noreferrer";
    wb.textContent = "Wayback archive";
    actions.appendChild(wb);
  }
  if (actions.childNodes.length > 0) {
    article.appendChild(actions);
  }

  const details = document.createElement("details");
  details.className = "card-details";
  const summary = document.createElement("summary");
  summary.textContent = "Link status & notes";
  details.appendChild(summary);

  const list = document.createElement("dl");
  list.className = "details-list";
  const appendItem = (label, value, isUrl = false) => {
    if (!value) return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (isUrl && isHttpUrl(value)) {
      const link = document.createElement("a");
      link.href = value;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = value;
      dd.appendChild(link);
    } else {
      dd.textContent = value;
    }
    list.append(dt, dd);
  };

  if (entry.description && entry.description.length > 240) {
    appendItem("Full description", entry.description);
  }
  appendItem("Original link", entry.web_link, true);
  if (entry.web_link_resolved && entry.web_link_resolved !== entry.web_link) {
    appendItem("Resolved destination", entry.web_link_resolved, true);
  }
  appendItem("HTTP check", entry.web_link_status ? `${entry.web_link_status} (${entry.web_link_state || "unknown"})` : "");
  appendItem("Discovery reasoning", entry.alternative_reasoning);
  appendItem("Notes", entry.alternative_note);
  for (const extra of entry.additional_web_links || []) {
    appendItem("Additional link", extra, true);
  }
  appendItem("Source record", entry.filename);

  details.appendChild(list);
  article.appendChild(details);
  return article;
}

function render() {
  syncLocationWithFilters();
  applyControlValues();

  const matches = filterEntries();
  const visible = matches.slice(0, state.limit);

  const resultsEl = document.getElementById("results");
  const statusEl = document.getElementById("results-status");
  const resetBtn = document.getElementById("reset-filters");
  const paginationEl = document.getElementById("pagination");
  const showMoreBtn = document.getElementById("show-more-btn");

  const hasCustomFilters = Boolean(state.q.trim() || state.freeOnly || state.format || state.view !== "working" || state.sort !== "relevance");
  resetBtn.hidden = !hasCustomFilters;

  statusEl.textContent = `Showing ${visible.length} of ${matches.length} matching guides (${state.entries.length} total).`;

  const fragment = document.createDocumentFragment();
  if (visible.length === 0) {
    const empty = document.createElement("p");
    empty.textContent = "No matching identification guides found. Try clearing one of the active filters.";
    fragment.appendChild(empty);
  } else {
    for (const entry of visible) {
      fragment.appendChild(createCard(entry));
    }
  }
  resultsEl.replaceChildren(fragment);

  const remaining = matches.length - visible.length;
  paginationEl.hidden = remaining <= 0;
  if (remaining > 0) {
    showMoreBtn.textContent = `Show ${Math.min(PAGE_SIZE, remaining)} more (${remaining} remaining)`;
  }
}

function updateSummaryCounts() {
  let working = 0;
  let recovered = 0;
  let broken = 0;
  let wayback = 0;
  for (const entry of state.entries) {
    if (isWorking(entry)) working += 1;
    if (isRecovered(entry)) recovered += 1;
    if (isBrokenNoAlt(entry)) broken += 1;
    if (entry.wayback_link) wayback += 1;
  }
  document.getElementById("stat-working").textContent = String(working);
  document.getElementById("stat-recovered").textContent = String(recovered);
  document.getElementById("stat-wayback").textContent = String(wayback);
  document.getElementById("stat-total").textContent = String(state.entries.length);

  document.getElementById("tab-count-working").textContent = `(${working})`;
  document.getElementById("tab-count-recovered").textContent = `(${recovered})`;
  document.getElementById("tab-count-all").textContent = `(${state.entries.length})`;
  document.getElementById("tab-count-broken").textContent = `(${broken})`;
}

async function fetchCatalogJson() {
  let lastError = null;
  for (const url of DATA_CANDIDATES) {
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
      return await response.json();
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError || new Error("Unable to load idsignpost.json");
}

function wireEvents() {
  const searchInput = document.getElementById("search-input");
  const clearBtn = document.getElementById("clear-btn");
  const freeToggle = document.getElementById("free-toggle");
  const sortSelect = document.getElementById("sort-select");
  const resetBtn = document.getElementById("reset-filters");
  const showMoreBtn = document.getElementById("show-more-btn");

  searchInput.addEventListener("input", () => {
    state.q = searchInput.value;
    state.limit = PAGE_SIZE;
    render();
  });

  clearBtn.addEventListener("click", () => {
    state.q = "";
    state.limit = PAGE_SIZE;
    render();
    searchInput.focus();
  });

  for (const tab of document.querySelectorAll(".tab-btn")) {
    tab.addEventListener("click", () => {
      state.view = tab.dataset.view;
      state.limit = PAGE_SIZE;
      render();
    });
  }

  freeToggle.addEventListener("click", () => {
    state.freeOnly = !state.freeOnly;
    state.limit = PAGE_SIZE;
    render();
  });

  for (const chip of document.querySelectorAll("[data-format]")) {
    chip.addEventListener("click", () => {
      state.format = state.format === chip.dataset.format ? "" : chip.dataset.format;
      state.limit = PAGE_SIZE;
      render();
    });
  }

  sortSelect.addEventListener("change", () => {
    state.sort = sortSelect.value;
    render();
  });

  resetBtn.addEventListener("click", () => {
    state.q = "";
    state.view = "working";
    state.freeOnly = false;
    state.format = "";
    state.sort = "relevance";
    state.limit = PAGE_SIZE;
    try {
      window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore storage errors.
    }
    render();
  });

  showMoreBtn.addEventListener("click", () => {
    state.limit += PAGE_SIZE;
    render();
  });
}

async function init() {
  readFiltersFromLocation();
  wireEvents();
  try {
    const rawEntries = await fetchCatalogJson();
    state.entries = rawEntries.map((entry) => ({
      ...entry,
      _search: buildSearchBlob(entry),
    }));
    updateSummaryCounts();
    render();
  } catch (err) {
    const banner = document.getElementById("error-banner");
    banner.hidden = false;
    banner.textContent = `Could not load catalogue data: ${err && err.message ? err.message : err}`;
    document.getElementById("results-status").textContent = "Catalogue failed to load.";
  }
}

init();
