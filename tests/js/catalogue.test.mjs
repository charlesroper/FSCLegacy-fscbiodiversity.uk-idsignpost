// Seam 2: catalogue logic and theme logic, exercised through their public exports.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const webDir = new URL("../../src/idsignpost/web/", import.meta.url);
const catalogue = await import(new URL("catalogue.js", webDir));
const theme = await import(new URL("theme.js", webDir));
const {
  DEFAULT_FILTERS,
  STATUSES,
  classify,
  filterEntries,
  filtersFromQuery,
  filtersToQuery,
  pageWindow,
  paginate,
  primaryLink,
  sortEntries,
  toCsv,
} = catalogue;

function entry(overrides = {}) {
  return {
    id: "example",
    filename: "example.html",
    title: "Example guide",
    submitted_by: null,
    submitted_at: "2016-01-01T00:00:00",
    description: null,
    authors: null,
    is_free: true,
    available_as: [],
    keywords: null,
    comment_body: null,
    web_link: "https://example.org/guide",
    additional_web_links: [],
    web_link_status: "200",
    web_link_state: "ok",
    web_link_resolved: null,
    wayback_link: null,
    alternative_link: null,
    alternative_probability: null,
    alternative_reasoning: null,
    alternative_note: null,
    verified_at: null,
    ...overrides,
  };
}

const live = entry({ id: "live", title: "Live guide" });
const alternative = entry({
  id: "alt",
  title: "Alternative guide",
  web_link_state: "broken",
  web_link_status: "404",
  alternative_link: "https://example.org/new",
});
const broken = entry({
  id: "broken",
  title: "Broken guide",
  web_link_state: "broken",
  web_link_status: "404",
});
const unverified = entry({ id: "unverified", title: "Unverified guide", web_link_state: "blocked" });
const noLink = entry({ id: "nolink", title: "No link guide", web_link: null, web_link_state: null });
const all = [live, alternative, broken, unverified, noLink];

test("classify names the five availability states", () => {
  assert.deepEqual(all.map(classify), ["live", "alternative", "broken", "unverified", "no-link"]);
});

test("STATUSES lists labels in the glossary vocabulary", () => {
  assert.deepEqual(
    STATUSES.map((s) => s.label),
    ["Live", "Alternative", "Broken", "Unverified", "No link"],
  );
});

test("every entry in the real catalogue has exactly one state", () => {
  const real = JSON.parse(readFileSync(new URL("../../idsignpost.json", import.meta.url), "utf8"));
  const keys = new Set(STATUSES.map((s) => s.key));
  assert.ok(real.length > 0);
  assert.ok(real.every((catalogEntry) => keys.has(classify(catalogEntry))));
});

test("primaryLink labels the original and alternative links", () => {
  assert.deepEqual(primaryLink(live), { url: "https://example.org/guide", label: "Open guide" });
  assert.deepEqual(primaryLink(alternative), {
    url: "https://example.org/new",
    label: "Open alternative",
  });
  assert.equal(primaryLink(unverified).label, "Open guide");
  assert.equal(primaryLink(broken), null);
  assert.equal(primaryLink(noLink), null);
});

test("primaryLink refuses non-http URLs", () => {
  assert.equal(primaryLink(entry({ web_link: "javascript:alert(1)" })), null);
});

test("default filters show only live and alternative guides", () => {
  assert.deepEqual(
    filterEntries(all, DEFAULT_FILTERS).map((e) => e.id),
    ["live", "alt"],
  );
});

test("clearing every status shows every guide", () => {
  const result = filterEntries(all, { ...DEFAULT_FILTERS, statuses: [] });
  assert.equal(result.length, 5);
});

test("search needs every word to match somewhere in the entry", () => {
  const beetles = entry({ id: "b", title: "Beetles", authors: "Jane Smith", keywords: "coleoptera" });
  const moths = entry({ id: "m", title: "Moths", authors: "Jane Jones" });
  const filters = { ...DEFAULT_FILTERS, statuses: [], q: "jane coleoptera" };
  assert.deepEqual(filterEntries([beetles, moths], filters).map((e) => e.id), ["b"]);
});

test("search matches the host of a link", () => {
  const filters = { ...DEFAULT_FILTERS, statuses: [], q: "bsbi.org" };
  const hit = entry({ id: "hit", web_link: "https://bsbi.org/keys" });
  assert.deepEqual(filterEntries([hit, live], filters).map((e) => e.id), ["hit"]);
});

test("format filter keeps entries offered in any chosen format", () => {
  const pdf = entry({ id: "pdf", available_as: ["PDF"] });
  const book = entry({ id: "book", available_as: ["Publication"] });
  const both = entry({ id: "both", available_as: ["Online", "PDF"] });
  const filters = { ...DEFAULT_FILTERS, formats: ["PDF"] };
  assert.deepEqual(filterEntries([pdf, book, both], filters).map((e) => e.id), ["pdf", "both"]);
});

test("free filter hides paid guides", () => {
  const paid = entry({ id: "paid", is_free: false });
  const filters = { ...DEFAULT_FILTERS, freeOnly: true };
  assert.deepEqual(filterEntries([live, paid], filters).map((e) => e.id), ["live"]);
});

test("relevance puts a title that starts with the search first", () => {
  const prefix = entry({ id: "prefix", title: "Beetle keys" });
  const inside = entry({ id: "inside", title: "A guide to beetle larvae" });
  const meta = entry({ id: "meta", title: "Aardvarks", keywords: "beetle" });
  const filters = { ...DEFAULT_FILTERS, statuses: [], q: "beetle", sort: "relevance" };
  const ids = sortEntries([meta, inside, prefix], filters).map((e) => e.id);
  assert.deepEqual(ids, ["prefix", "inside", "meta"]);
});

test("title sort ignores case and does not change the input", () => {
  const input = [entry({ title: "zebra" }), entry({ title: "Apple" }), entry({ title: "mango" })];
  const sorted = sortEntries(input, { ...DEFAULT_FILTERS, sort: "title" });
  assert.deepEqual(sorted.map((e) => e.title), ["Apple", "mango", "zebra"]);
  assert.equal(input[0].title, "zebra");
});

test("date sorts order by submitted_at", () => {
  const older = entry({ id: "older", submitted_at: "2015-01-01T00:00:00" });
  const newer = entry({ id: "newer", submitted_at: "2020-01-01T00:00:00" });
  assert.deepEqual(
    sortEntries([older, newer], { ...DEFAULT_FILTERS, sort: "newest" }).map((e) => e.id),
    ["newer", "older"],
  );
  assert.deepEqual(
    sortEntries([newer, older], { ...DEFAULT_FILTERS, sort: "oldest" }).map((e) => e.id),
    ["older", "newer"],
  );
});

test("paginate returns one page of 25 with one-based positions", () => {
  const items = Array.from({ length: 60 }, (_, i) => i);
  const page = paginate(items, 2);
  assert.equal(page.items.length, 25);
  assert.deepEqual(
    { start: page.start, end: page.end, total: page.total, totalPages: page.totalPages },
    { start: 26, end: 50, total: 60, totalPages: 3 },
  );
});

test("paginate clamps the page into range", () => {
  const items = Array.from({ length: 60 }, (_, i) => i);
  assert.equal(paginate(items, 9).page, 3);
  assert.equal(paginate(items, 0).page, 1);
  assert.deepEqual(paginate([], 1), { items: [], page: 1, start: 0, end: 0, total: 0, totalPages: 1 });
});

test("pageWindow keeps first, last and neighbours, with gaps for the rest", () => {
  assert.deepEqual(pageWindow(1, 3), [1, 2, 3]);
  assert.deepEqual(pageWindow(1, 10), [1, 2, "gap", 10]);
  assert.deepEqual(pageWindow(5, 10), [1, "gap", 4, 5, 6, "gap", 10]);
  assert.deepEqual(pageWindow(4, 10), [1, 2, 3, 4, 5, "gap", 10]);
  assert.deepEqual(pageWindow(1, 1), [1]);
});

test("toCsv quotes commas, quotes and line breaks", () => {
  const tricky = entry({
    title: 'Keys, "new" edition',
    authors: "A\nB",
    available_as: ["Online", "PDF"],
    wayback_link: "https://web.archive.org/web/1/x",
  });
  const rows = toCsv([tricky]).split("\r\n");
  assert.equal(rows[0], "Title,Authors,Cost,Formats,Status,Guide link,Wayback archive");
  assert.equal(
    rows.slice(1).join("\r\n"),
    '"Keys, ""new"" edition","A\nB",Free,Online; PDF,Live,https://example.org/guide,https://web.archive.org/web/1/x',
  );
});

test("toCsv neutralises spreadsheet formulas", () => {
  const csv = toCsv([entry({ title: "=HYPERLINK(1)", authors: "+1" })]);
  assert.ok(csv.includes("'=HYPERLINK(1)"));
  assert.ok(csv.includes("'+1"));
});

test("default filters leave the URL clean", () => {
  assert.equal(filtersToQuery(DEFAULT_FILTERS), "");
  assert.deepEqual(filtersFromQuery(""), DEFAULT_FILTERS);
});

test("filters survive a round trip through the URL", () => {
  const filters = {
    q: "water beetles",
    statuses: ["broken", "no-link"],
    formats: ["PDF", "App"],
    freeOnly: true,
    sort: "newest",
    page: 3,
  };
  assert.deepEqual(filtersFromQuery(filtersToQuery(filters)), filters);
});

test("an empty status list survives a round trip", () => {
  const filters = { ...DEFAULT_FILTERS, statuses: [] };
  assert.deepEqual(filtersFromQuery(filtersToQuery(filters)).statuses, []);
});

test("unknown URL values fall back to defaults", () => {
  const parsed = filtersFromQuery("?status=bogus&format=Scroll&sort=random&page=-4");
  assert.deepEqual(parsed, DEFAULT_FILTERS);
});

test("theme: the OS preference applies until the visitor chooses", () => {
  assert.equal(theme.effectiveTheme(null, true), "dark");
  assert.equal(theme.effectiveTheme(null, false), "light");
  assert.equal(theme.effectiveTheme("light", true), "light");
});

test("theme: toggling flips the theme and stores only a real override", () => {
  assert.equal(theme.nextOverride(null, false), "dark");
  assert.equal(theme.nextOverride(null, true), "light");
  assert.equal(theme.nextOverride("dark", false), null);
  assert.equal(theme.nextOverride("light", true), null);
  assert.equal(theme.nextOverride("light", false), "dark");
});
