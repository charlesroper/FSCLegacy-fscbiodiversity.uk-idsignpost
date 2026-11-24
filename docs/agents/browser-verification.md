# Browser Verification

Headless browser verification for the static directory in `src/idsignpost/web/`.

## Setup

`src/idsignpost/web/package.json` pins `playwright-core` (not `playwright`) so `npm --prefix src/idsignpost/web ci` never downloads incompatible browsers automatically:

```bash
npm --prefix src/idsignpost/web ci
node tests/js/check_layout.mjs
```

`tests/js/check_layout.mjs` checks mobile (`390x844`) and desktop (`1280x800`) viewports against a local static server serving `src/idsignpost/web/` and `idsignpost.json`:

- Zero horizontal overflow (`scrollWidth <= innerWidth`)
- Non-overlapping header title and top-right `#theme-toggle` (`>= 44x44`)
- Two-state theme switching (`data-theme` attribute and background contrast change)
- Touch-target separation between primary guide links and `.row-details summary`
- Full-width mobile **Details** body and mobile filter disclosure toggle

## Browser resolution order

`check_layout.mjs` checks executables in this order:

1. `CHROME_PATH` environment variable
2. Cached Chromium 153 headless shell (`chromium_headless_shell-1243`) in `PLAYWRIGHT_BROWSERS_PATH` or `~/.cache/ms-playwright` (newer Chromium builds crash with `SIGTRAP` under Debian PRoot)
3. Default `playwright-core` Chromium executable
4. System `chromium`, `chromium-browser` or `google-chrome` on `PATH`

On Linux, Chromium launches with `--no-sandbox`. `tests/test_web_catalogue.py` runs `check_layout.mjs` automatically when `playwright-core` and an executable browser are present, and skips cleanly on hosts without a local browser binary.
