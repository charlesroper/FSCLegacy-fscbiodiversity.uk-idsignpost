// Pure catalogue logic: classification, filtering, sorting, paging, CSV and URL state.
// No DOM access, so Node can test it directly (tests/js/catalogue.test.mjs).

export const STATUSES = Object.freeze([
  { key: "live", label: "Live" },
  { key: "alternative", label: "Alternative" },
  { key: "broken", label: "Broken" },
  { key: "unverified", label: "Unverified" },
  { key: "no-link", label: "No link" },
]);

export const FORMATS = Object.freeze(["Online", "PDF", "Publication", "App"]);

export const SORTS = Object.freeze([
  { key: "relevance", label: "Best match" },
  { key: "title", label: "Title (A to Z)" },
  { key: "newest", label: "Date added (newest)" },
  { key: "oldest", label: "Date added (oldest)" },
]);

export const PAGE_SIZE = 25;

export const DEFAULT_FILTERS = Object.freeze({
  q: "",
  statuses: ["live", "alternative"],
  formats: [],
  freeOnly: false,
  sort: "relevance",
  page: 1,
});

const STATUS_KEYS = STATUSES.map((status) => status.key);
const SORT_KEYS = SORTS.map((sort) => sort.key);
const CSV_HEADER = ["Title", "Authors", "Cost", "Formats", "Status", "Guide link", "Wayback archive"];

export function classify(entry) {
  if (entry.web_link_state === "ok") return "live";
  if (entry.alternative_link) return "alternative";
  if (entry.web_link_state === "broken") return "broken";
  if (entry.web_link_state === "blocked") return "unverified";
  return "no-link";
}

export function statusLabel(key) {
  return STATUSES.find((status) => status.key === key)?.label ?? key;
}

export function isHttpUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

export function primaryLink(entry) {
  const key = classify(entry);
  if ((key === "live" || key === "unverified") && isHttpUrl(entry.web_link)) {
    return { url: entry.web_link, label: "Open guide" };
  }
  if (key === "alternative" && isHttpUrl(entry.alternative_link)) {
    return { url: entry.alternative_link, label: "Open alternative" };
  }
  return null;
}

function hostOf(value) {
  if (!isHttpUrl(value)) return "";
  try {
    return new URL(value).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function searchText(entry) {
  return [
    entry.title,
    entry.authors,
    entry.keywords,
    entry.description,
    entry.comment_body,
    (entry.available_as ?? []).join(" "),
    hostOf(entry.web_link),
    hostOf(entry.alternative_link),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function tokensOf(query) {
  return query.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

export function filterEntries(entries, filters) {
  const tokens = tokensOf(filters.q);
  return entries.filter((entry) => {
    if (filters.statuses.length > 0 && !filters.statuses.includes(classify(entry))) return false;
    if (filters.freeOnly && !entry.is_free) return false;
    if (filters.formats.length > 0) {
      const offered = entry.available_as ?? [];
      if (!filters.formats.some((format) => offered.includes(format))) return false;
    }
    if (tokens.length === 0) return true;
    const text = searchText(entry);
    return tokens.every((token) => text.includes(token));
  });
}

function relevance(entry, tokens) {
  const title = (entry.title ?? "").toLowerCase();
  const meta = `${entry.authors ?? ""} ${entry.keywords ?? ""}`.toLowerCase();
  let score = 0;
  for (const token of tokens) {
    if (title.startsWith(token)) score += 6;
    else if (title.includes(token)) score += 4;
    if (meta.includes(token)) score += 2;
    score += 1;
  }
  return score;
}

function byTitle(a, b) {
  return (a.title ?? "").localeCompare(b.title ?? "", "en", { sensitivity: "base" });
}

export function sortEntries(entries, filters) {
  const tokens = tokensOf(filters.q);
  const copy = [...entries];
  switch (filters.sort) {
    case "newest":
      return copy.sort((a, b) => (b.submitted_at ?? "").localeCompare(a.submitted_at ?? "") || byTitle(a, b));
    case "oldest":
      return copy.sort((a, b) => (a.submitted_at ?? "").localeCompare(b.submitted_at ?? "") || byTitle(a, b));
    case "title":
      return copy.sort(byTitle);
    default:
      if (tokens.length === 0) return copy.sort(byTitle);
      return copy.sort((a, b) => relevance(b, tokens) - relevance(a, tokens) || byTitle(a, b));
  }
}

export function paginate(items, page, size = PAGE_SIZE) {
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(Math.trunc(page) || 1, 1), totalPages);
  return {
    items: items.slice((current - 1) * size, current * size),
    page: current,
    start: total === 0 ? 0 : (current - 1) * size + 1,
    end: Math.min(current * size, total),
    total,
    totalPages,
  };
}

export function pageWindow(page, totalPages) {
  const wanted = [1, totalPages, page - 1, page, page + 1].filter((n) => n >= 1 && n <= totalPages);
  const sorted = [...new Set(wanted)].sort((a, b) => a - b);
  const pages = [];
  sorted.forEach((n, index) => {
    if (index > 0) {
      const gap = n - sorted[index - 1];
      if (gap === 2) pages.push(n - 1);
      else if (gap > 2) pages.push("gap");
    }
    pages.push(n);
  });
  return pages;
}

function csvCell(value) {
  let text = value == null ? "" : String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function toCsv(entries) {
  const rows = entries.map((entry) => [
    entry.title,
    entry.authors,
    entry.is_free ? "Free" : "Paid",
    (entry.available_as ?? []).join("; "),
    statusLabel(classify(entry)),
    primaryLink(entry)?.url,
    entry.wayback_link,
  ]);
  return [CSV_HEADER, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
}

function sameSet(a, b) {
  return a.length === b.length && a.every((item) => b.includes(item));
}

export function filtersToQuery(filters) {
  const params = new URLSearchParams();
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (!sameSet(filters.statuses, DEFAULT_FILTERS.statuses)) {
    if (filters.statuses.length === 0) params.append("status", "any");
    for (const status of filters.statuses) params.append("status", status);
  }
  for (const format of filters.formats) params.append("format", format);
  if (filters.freeOnly) params.set("free", "1");
  if (filters.sort !== DEFAULT_FILTERS.sort) params.set("sort", filters.sort);
  if (filters.page > 1) params.set("page", String(filters.page));
  return params.toString();
}

export function filtersFromQuery(search) {
  const params = new URLSearchParams(search);
  const requested = params.getAll("status");
  const chosen = STATUS_KEYS.filter((key) => requested.includes(key));
  let statuses = [...DEFAULT_FILTERS.statuses];
  if (chosen.length > 0) statuses = chosen;
  else if (requested.includes("any")) statuses = [];

  const formats = FORMATS.filter((format) => params.getAll("format").includes(format));
  const sort = params.get("sort");
  const page = Number.parseInt(params.get("page") ?? "", 10);

  return {
    q: params.get("q") ?? "",
    statuses,
    formats,
    freeOnly: params.get("free") === "1",
    sort: SORT_KEYS.includes(sort) ? sort : DEFAULT_FILTERS.sort,
    page: page >= 1 ? page : 1,
  };
}
