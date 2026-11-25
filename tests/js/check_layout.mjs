#!/usr/bin/env node
// Headless Chromium layout & interaction check (mobile 390x844 + desktop 1280x800).
// Resolves CHROME_PATH -> PRoot-safe Chromium 153 cache -> Playwright default -> system Chromium.
import assert from "node:assert/strict";
import { accessSync, constants, existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const WEB_DIR = path.join(REPO_ROOT, "src", "idsignpost", "web");
const requireFromWeb = createRequire(path.join(WEB_DIR, "package.json"));
const { chromium } = requireFromWeb("playwright-core");

const CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

function isExecutable(file) {
  try {
    if (!file || !statSync(file).isFile()) return false;
    accessSync(file, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function readBrowsersConfig() {
  try {
    const file = path.join(os.homedir(), ".config", "agent", "browsers.json");
    if (!existsSync(file)) return null;
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function findChromiumExecutable() {
  if (process.env.CHROME_PATH && isExecutable(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const hostExe = readBrowsersConfig()?.engines?.chromium?.executablePath;
  if (hostExe && isExecutable(hostExe)) {
    return hostExe;
  }
  const cacheDir =
    process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), ".cache", "ms-playwright");
  const preferredRoot = path.join(cacheDir, "chromium_headless_shell-1243");
  if (existsSync(preferredRoot)) {
    for (const child of readdirSync(preferredRoot, { withFileTypes: true })) {
      if (!child.isDirectory()) continue;
      for (const bin of ["chrome-headless-shell", "chrome-headless-shell.exe"]) {
        const candidate = path.join(preferredRoot, child.name, bin);
        if (isExecutable(candidate)) return candidate;
      }
    }
  }
  const pwDefault = chromium.executablePath();
  if (isExecutable(pwDefault)) return pwDefault;
  for (const cmd of ["chromium", "chromium-browser", "google-chrome"]) {
    for (const dir of (process.env.PATH || "").split(path.delimiter)) {
      const candidate = path.join(dir, cmd);
      if (isExecutable(candidate)) return candidate;
    }
  }
  return null;
}

function startStaticServer() {
  return http.createServer((req, res) => {
    const cleanPath = (req.url || "/").split("?")[0];
    const rel = cleanPath === "/" ? "index.html" : cleanPath.replace(/^\/+/, "");
    const target = rel === "idsignpost.json" ? path.join(REPO_ROOT, rel) : path.join(WEB_DIR, rel);
    if (!existsSync(target) || !statSync(target).isFile()) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    const ext = path.extname(target);
    res.writeHead(200, { "Content-Type": CONTENT_TYPES[ext] || "application/octet-stream" });
    res.end(readFileSync(target));
  });
}

async function checkViewport(browser, baseUrl, viewport, isMobile) {
  const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (err) => pageErrors.push(err.message));

  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => (document.getElementById("results-count")?.textContent || "").includes("Showing 1 to 25"),
    { timeout: 10000 },
  );

  const metrics = await page.evaluate(() => {
    const doc = document.documentElement;
    const header = document.querySelector(".site-header").getBoundingClientRect();
    const title = document.querySelector(".brand__title").getBoundingClientRect();
    const toggle = document.getElementById("theme-toggle").getBoundingClientRect();
    return {
      scrollWidth: doc.scrollWidth,
      innerWidth: window.innerWidth,
      headerTop: header.top,
      headerRight: header.right,
      titleRight: title.right,
      toggleTop: toggle.top,
      toggleLeft: toggle.left,
      toggleRight: toggle.right,
      toggleWidth: toggle.width,
      toggleHeight: toggle.height,
    };
  });

  assert.equal(pageErrors.length, 0, `JS errors: ${pageErrors.join("; ")}`);
  assert.ok(
    metrics.scrollWidth <= metrics.innerWidth,
    `Horizontal overflow at ${viewport.width}px: ${metrics.scrollWidth} > ${metrics.innerWidth}`,
  );
  assert.ok(metrics.toggleWidth >= 44 && metrics.toggleHeight >= 44, "Theme toggle < 44x44");
  assert.ok(metrics.toggleTop - metrics.headerTop <= 16, "Theme toggle is not at top of header");
  assert.ok(
    metrics.toggleLeft >= metrics.titleRight - 2 || metrics.toggleRight <= metrics.headerRight,
    "Theme toggle collides with title",
  );

  // Check two-state theme toggle updates DOM and contrast surface colour cleanly
  const initialBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.click("#theme-toggle");
  const toggled = await page.evaluate(() => ({
    theme: document.documentElement.getAttribute("data-theme"),
    pressed: document.getElementById("theme-toggle").getAttribute("aria-pressed"),
    bg: getComputedStyle(document.body).backgroundColor,
  }));
  assert.ok(toggled.theme === "dark" || toggled.theme === "light");
  assert.notEqual(toggled.bg, initialBg, "Theme toggle did not change body backgroundColor");
  await page.click("#theme-toggle");

  // Expand first Details disclosure & measure touch spacing, focus contrast, and card width
  const firstRow = page.locator("#table-body tr").first();
  await firstRow.locator(".row-details summary").click();
  const focusSummaryBg = await firstRow.locator(".row-details summary").evaluate((el) => {
    el.focus();
    return getComputedStyle(el).backgroundColor;
  });
  assert.equal(
    focusSummaryBg,
    "rgba(0, 0, 0, 0)",
    `Details summary focus background should stay transparent, got ${focusSummaryBg}`,
  );

  const paginationBg = await page.evaluate(() => {
    const li = document.querySelector(".govuk-pagination__item--current");
    return li ? getComputedStyle(li).backgroundColor : null;
  });
  assert.equal(
    paginationBg,
    "rgba(0, 0, 0, 0)",
    `Current pagination item li background should be transparent, got ${paginationBg}`,
  );

  const detailMetrics = await firstRow.evaluate((row) => {
    const link = row.querySelector(".guide-action a")?.getBoundingClientRect();
    const summary = row.querySelector(".row-details summary").getBoundingClientRect();
    const detailsText = row.querySelector(".row-details .govuk-details__text").getBoundingClientRect();
    const card = row.getBoundingClientRect();
    return {
      linkBottom: link?.bottom ?? null,
      linkHeight: link?.height ?? null,
      summaryTop: summary.top,
      summaryHeight: summary.height,
      detailsWidth: detailsText.width,
      cardWidth: card.width,
    };
  });

  if (detailMetrics.linkBottom !== null) {
    assert.ok(
      detailMetrics.summaryTop - detailMetrics.linkBottom >= 10,
      `Details too close to link (${detailMetrics.summaryTop - detailMetrics.linkBottom}px)`,
    );
  }
  if (isMobile) {
    assert.ok(detailMetrics.summaryHeight >= 44, `Mobile summary target < 44px (${detailMetrics.summaryHeight})`);
    if (detailMetrics.linkHeight !== null) {
      assert.ok(detailMetrics.linkHeight >= 44, `Mobile guide link target < 44px (${detailMetrics.linkHeight})`);
    }
    assert.ok(
      detailMetrics.detailsWidth >= detailMetrics.cardWidth * 0.78,
      `Mobile details too narrow (${detailMetrics.detailsWidth} vs card ${detailMetrics.cardWidth})`,
    );
    await page.click("#filter-toggle");
    assert.equal(await page.getAttribute("#filter-toggle", "aria-expanded"), "true");
    assert.ok(await page.locator("#filter-panel").isVisible(), "Mobile filter panel did not open");
  }

  await context.close();
}

async function main() {
  const executablePath = findChromiumExecutable();
  if (!executablePath) {
    console.error("No Chromium executable found; skipping headless layout check.");
    process.exit(2);
  }
  const server = startStaticServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  const baseUrl = `http://127.0.0.1:${port}/`;
  const launchArgs =
    readBrowsersConfig()?.launchArgs ?? (process.platform === "linux" ? ["--no-sandbox"] : []);
  const browser = await chromium.launch({ executablePath, headless: true, args: launchArgs });
  try {
    await checkViewport(browser, baseUrl, { width: 390, height: 844 }, true);
    await checkViewport(browser, baseUrl, { width: 768, height: 1024 }, true);
    await checkViewport(browser, baseUrl, { width: 1280, height: 800 }, false);
    console.log("LAYOUT_CHECK_OK");
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
