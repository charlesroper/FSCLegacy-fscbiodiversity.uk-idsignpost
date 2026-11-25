import {
  DEFAULT_FILTERS,
  FORMATS,
  STATUSES,
  classify,
  filterEntries,
  filtersFromQuery,
  filtersToQuery,
  isHttpUrl,
  pageWindow,
  paginate,
  primaryLink,
  sortEntries,
  statusLabel,
  toCsv,
} from "./catalogue.js";
import { THEME_KEY, effectiveTheme, nextOverride } from "./theme.js";

const DATA_URLS = [
  "./idsignpost.json",
  "../../../idsignpost.json",
  "https://raw.githubusercontent.com/charlesroper/FSCLegacy-fscbiodiversity.uk-idsignpost/main/idsignpost.json",
];
const FILTERS_KEY = "idsignpost-filters";
const MOBILE_QUERY = "(max-width: 960px)";

let entries = [];
let filters = initialFilters();
let currentMatches = [];

function make(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = content;
  return node;
}

function externalLink(url, label, guideTitle) {
  const link = make("a", "govuk-link");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (guideTitle) {
    link.append(label, make("span", "govuk-visually-hidden", `: ${guideTitle} (opens in a new tab)`));
  } else {
    link.textContent = label;
  }
  return link;
}

function formatDate(isoText) {
  const date = new Date(isoText);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function urlWithQuery(query) {
  return query ? `?${query}` : window.location.pathname;
}

// ---------- state ----------

function initialFilters() {
  let search = window.location.search;
  if (!search) {
    try {
      search = window.sessionStorage.getItem(FILTERS_KEY) ?? "";
    } catch {
      search = "";
    }
  }
  return filtersFromQuery(search);
}

function saveFilters() {
  const query = filtersToQuery(filters);
  window.history.replaceState(null, "", urlWithQuery(query));
  try {
    if (query) window.sessionStorage.setItem(FILTERS_KEY, query);
    else window.sessionStorage.removeItem(FILTERS_KEY);
  } catch {
    // Private browsing can block storage. The URL still holds the state.
  }
}

function resetFilters() {
  filters = { ...DEFAULT_FILTERS, statuses: [...DEFAULT_FILTERS.statuses] };
  render();
}

// ---------- rendering ----------

function statusTag(key) {
  return make("span", `status-tag status-tag--${key}`, statusLabel(key));
}

function detailRow(list, label, value) {
  if (!value) return;
  const term = make("dt", null, label);
  const definition = make("dd");
  if (value instanceof Node) definition.append(value);
  else definition.textContent = value;
  list.append(term, definition);
}

function detailUrlValue(url) {
  return isHttpUrl(url) ? externalLink(url, url) : url;
}

function detailsFor(entry, key) {
  const list = make("dl", "details-list");
  detailRow(list, "Description", entry.description);
  detailRow(list, "Keywords", entry.keywords);
  if (key === "alternative" || key === "broken") {
    detailRow(list, "Original link", detailUrlValue(entry.web_link));
  }
  if (key === "alternative") detailRow(list, "Why this alternative", entry.alternative_reasoning);
  for (const extra of entry.additional_web_links ?? []) {
    detailRow(list, "More links", detailUrlValue(extra));
  }
  if (entry.verified_at) detailRow(list, "Last checked", formatDate(entry.verified_at));
  if (!list.childNodes.length) return null;

  const details = make("details", "govuk-details row-details");
  const summary = make("summary", "govuk-details__summary");
  summary.append(make("span", "govuk-details__summary-text", "Details"));
  const body = make("div", "govuk-details__text");
  body.append(list);
  details.append(summary, body);
  return details;
}

function cell(tag, label, className) {
  const node = make(
    tag,
    `${tag === "th" ? "govuk-table__header" : "govuk-table__cell"} ${className ?? ""}`.trim(),
  );
  node.dataset.label = label;
  if (tag === "th") node.scope = "row";
  return node;
}

function valueBox(...children) {
  const box = make("div", "cell-value");
  box.append(...children);
  return box;
}

function buildRow(entry) {
  const key = classify(entry);
  const row = make("tr", "govuk-table__row");

  const guide = cell("th", "Guide", "cell--guide");
  const guideBox = valueBox(make("strong", "guide-title", entry.title));
  const link = primaryLink(entry);
  if (link) {
    const action = make("div", "guide-action");
    action.append(externalLink(link.url, link.label, entry.title));
    guideBox.append(action);
  }
  const details = detailsFor(entry, key);
  if (details) guideBox.append(details);
  guide.append(guideBox);

  const authors = cell("td", "Authors");
  authors.append(valueBox(entry.authors || "Not stated"));

  const format = cell("td", "Format and cost");
  const formatBox = valueBox(make("span", "cost", entry.is_free ? "Free" : "Paid"));
  const offered = (entry.available_as ?? []).join(", ");
  if (offered) formatBox.append(make("br"), make("span", "formats", offered));
  format.append(formatBox);

  const status = cell("td", "Status");
  status.append(valueBox(statusTag(key)));

  const archive = cell("td", "Archive");
  archive.append(
    valueBox(
      isHttpUrl(entry.wayback_link)
        ? externalLink(entry.wayback_link, "Wayback archive", entry.title)
        : make("span", "muted", "Not archived"),
    ),
  );

  row.append(guide, authors, format, status, archive);
  return row;
}

function chipDefinitions() {
  const chips = [];
  if (filters.q.trim()) chips.push({ kind: "q", value: "", label: `Search: ${filters.q.trim()}` });
  for (const key of filters.statuses) {
    chips.push({ kind: "status", value: key, label: `Status: ${statusLabel(key)}` });
  }
  for (const format of filters.formats) {
    chips.push({ kind: "format", value: format, label: `Format: ${format}` });
  }
  if (filters.freeOnly) chips.push({ kind: "free", value: "", label: "Cost: Free to use" });
  return chips;
}

function renderChips() {
  const chips = chipDefinitions();
  document.getElementById("active-filters").hidden = chips.length === 0;
  const items = chips.map((chip) => {
    const item = make("li");
    const button = make("button", "filter-chip");
    button.type = "button";
    button.dataset.kind = chip.kind;
    button.dataset.value = chip.value;
    button.setAttribute("aria-label", `Remove filter: ${chip.label}`);
    button.append(chip.label, make("span", "filter-chip__x", "\u00d7"));
    item.append(button);
    return item;
  });
  document.getElementById("active-filter-list").replaceChildren(...items);
}

function syncControls() {
  document.getElementById("search-input").value = filters.q;
  document.getElementById("sort-select").value = filters.sort;
  for (const box of document.querySelectorAll('input[name="status"]')) {
    box.checked = filters.statuses.includes(box.value);
  }
  for (const box of document.querySelectorAll('input[name="format"]')) {
    box.checked = filters.formats.includes(box.value);
  }
  document.getElementById("cost-free").checked = filters.freeOnly;
}

function pageHref(page) {
  return urlWithQuery(filtersToQuery({ ...filters, page }));
}

function renderPagination(paging) {
  const nav = document.getElementById("pagination");
  nav.hidden = paging.totalPages <= 1;
  if (nav.hidden) return;

  const previous = document.getElementById("page-prev");
  const next = document.getElementById("page-next");
  previous.hidden = paging.page <= 1;
  next.hidden = paging.page >= paging.totalPages;
  previous.firstElementChild.href = pageHref(paging.page - 1);
  previous.firstElementChild.dataset.page = String(paging.page - 1);
  next.firstElementChild.href = pageHref(paging.page + 1);
  next.firstElementChild.dataset.page = String(paging.page + 1);

  const items = pageWindow(paging.page, paging.totalPages).map((entryPage) => {
    if (entryPage === "gap") {
      const gap = make("li", "govuk-pagination__item govuk-pagination__item--ellipses", "\u22ef");
      gap.setAttribute("aria-hidden", "true");
      return gap;
    }
    const current = entryPage === paging.page;
    const item = make(
      "li",
      `govuk-pagination__item${current ? " govuk-pagination__item--current" : ""}`,
    );
    const link = make("a", "govuk-link govuk-pagination__link", String(entryPage));
    link.href = pageHref(entryPage);
    link.dataset.page = String(entryPage);
    link.setAttribute("aria-label", `Page ${entryPage}`);
    if (current) link.setAttribute("aria-current", "page");
    item.append(link);
    return item;
  });
  document.getElementById("pagination-list").replaceChildren(...items);
}

function countText(paging) {
  if (paging.total === 0) return "No guides found";
  if (paging.total === 1) return "Showing 1 guide";
  return `Showing ${paging.start} to ${paging.end} of ${paging.total} guides`;
}

function render() {
  currentMatches = sortEntries(filterEntries(entries, filters), filters);
  const paging = paginate(currentMatches, filters.page);
  filters.page = paging.page;

  saveFilters();
  syncControls();
  renderChips();

  const empty = paging.total === 0;
  document.getElementById("results-table").hidden = empty;
  document.getElementById("empty-state").hidden = !empty;
  document.getElementById("export-button").hidden = empty;
  document.getElementById("results-count").textContent = countText(paging);
  document.getElementById("table-body").replaceChildren(...paging.items.map(buildRow));
  renderPagination(paging);
}

function renderCounts() {
  const counts = new Map();
  const increment = (key) => counts.set(key, (counts.get(key) ?? 0) + 1);
  for (const entry of entries) {
    increment(`status:${classify(entry)}`);
    for (const format of entry.available_as ?? []) increment(`format:${format}`);
    if (entry.is_free) increment("free");
  }
  for (const node of document.querySelectorAll("[data-count]")) {
    node.textContent = `(${counts.get(node.dataset.count) ?? 0})`;
  }
}

// ---------- events ----------

function checkedValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((box) => box.value);
}

function wireFilters() {
  document.getElementById("search-input").addEventListener("input", (event) => {
    filters = { ...filters, q: event.target.value, page: 1 };
    render();
  });

  document.getElementById("filter-panel").addEventListener("change", () => {
    const statusKeys = STATUSES.map((status) => status.key);
    filters = {
      ...filters,
      statuses: checkedValues("status").filter((value) => statusKeys.includes(value)),
      formats: checkedValues("format").filter((value) => FORMATS.includes(value)),
      freeOnly: document.getElementById("cost-free").checked,
      page: 1,
    };
    render();
  });

  document.getElementById("sort-select").addEventListener("change", (event) => {
    filters = { ...filters, sort: event.target.value, page: 1 };
    render();
  });

  document.getElementById("reset-filters").addEventListener("click", resetFilters);
  document.getElementById("empty-reset").addEventListener("click", resetFilters);

  document.getElementById("active-filter-list").addEventListener("click", (event) => {
    const button = event.target.closest(".filter-chip");
    if (!button) return;
    const { kind, value } = button.dataset;
    const next = { ...filters, page: 1 };
    if (kind === "q") next.q = "";
    if (kind === "status") next.statuses = filters.statuses.filter((key) => key !== value);
    if (kind === "format") next.formats = filters.formats.filter((format) => format !== value);
    if (kind === "free") next.freeOnly = false;
    filters = next;
    render();
  });

  document.getElementById("pagination").addEventListener("click", (event) => {
    const link = event.target.closest("a[data-page]");
    if (!link) return;
    event.preventDefault();
    filters = { ...filters, page: Number(link.dataset.page) };
    render();
    document.getElementById("results-count").focus();
  });

  document.getElementById("export-button").addEventListener("click", downloadCsv);
}

function wireFilterToggle() {
  const button = document.getElementById("filter-toggle");
  const panel = document.getElementById("filter-panel");
  const setPanelOpen = (open) => {
    panel.classList.toggle("is-open", open);
    button.setAttribute("aria-expanded", String(open));
    button.textContent = open ? "Hide filters" : "Show filters";
  };
  button.addEventListener("click", () => {
    setPanelOpen(!panel.classList.contains("is-open"));
  });
  window.matchMedia(MOBILE_QUERY).addEventListener("change", (event) => {
    if (!event.matches) setPanelOpen(false);
  });
}

function downloadCsv() {
  const blob = new Blob(["\ufeff", toCsv(currentMatches)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = make("a");
  link.href = url;
  link.download = `id-resource-signpost-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- theme ----------

function wireTheme() {
  const root = document.documentElement;
  const button = document.getElementById("theme-toggle");
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let memoryOverride = null;

  const stored = () => {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch {
      return memoryOverride;
    }
  };

  const apply = () => {
    const choice = stored();
    if (choice === "light" || choice === "dark") root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");
    button.setAttribute("aria-pressed", String(effectiveTheme(choice, media.matches) === "dark"));
  };

  button.addEventListener("click", () => {
    const next = nextOverride(stored(), media.matches);
    memoryOverride = next;
    try {
      if (next) window.localStorage.setItem(THEME_KEY, next);
      else window.localStorage.removeItem(THEME_KEY);
    } catch {
      // Keep the override in memory when storage is unavailable.
    }
    apply();
  });
  media.addEventListener("change", apply);
  apply();
}

// ---------- start ----------

async function loadCatalogue() {
  let lastError = null;
  for (const url of DATA_URLS) {
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
      return await response.json();
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError ?? new Error("No catalogue data found");
}

function showError(error) {
  document.getElementById("error-message").textContent =
    `Could not load the catalogue (${error.message}). Check your connection, then reload the page.`;
  document.getElementById("error-summary").hidden = false;
  document.getElementById("results-count").textContent = "No guides loaded";
  document.getElementById("results-table").hidden = true;
}

async function start() {
  wireTheme();
  wireFilterToggle();
  wireFilters();
  syncControls();
  try {
    entries = await loadCatalogue();
  } catch (error) {
    showError(error);
    return;
  }
  renderCounts();
  render();
}

start();
