// Screenshots of the local OpenObserve UI for the D-027 monitor (headless Chrome over CDP; Node >= 22, no npm packages).
//
// Usage: node scripts/openobserve/screenshots.mjs [--chrome PATH]
//
// Logs in to http://127.0.0.1:5080 with the throwaway test credentials of O2_HOME/.env (typed into the login form,
// never printed or saved), renders in UTC at 1600x1000 with every non-localhost request blocked (cdp.mjs), and writes
//   docs/openobserve-dashboard.png  the dashboard "Who tells the humans: claims vs receipts"
//   docs/openobserve-alert.png      the alert's SQL (monitor.sql, verbatim) run in the log explorer, with its rows
//   docs/openobserve-flagged.png    stream wtth_posts for the untold FORGED shift 83; both flagged posts are in view
//                                   (C's claim at 21:26, and rd-26's relay of it at 21:42, expanded)
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_CHROME, launch, sleep } from "./cdp.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..");
const O2_HOME = process.env.O2_HOME || "/Users/amey/Projects/wtth-openobserve";
const BASE = "http://127.0.0.1:5080";
const FROM = 1767297600000000;   // 2026-01-01 20:00 UTC (synthetic shift date), microseconds
const TO = 1767310200000000;     // 2026-01-01 23:30 UTC
const argChrome = process.argv.indexOf("--chrome");
const chrome = argChrome > 0 ? process.argv[argChrome + 1] : DEFAULT_CHROME;

function readEnv(path) {
  const out = {};
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#") || !t.includes("=")) continue;
    const i = t.indexOf("=");
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim().replace(/^'(.*)'$/, "$1").replace(/^"(.*)"$/, "$1");
  }
  return out;
}

function logsUrl(sql) {
  const q = encodeURIComponent(Buffer.from(sql, "utf8").toString("base64"));
  return `${BASE}/web/logs?stream_type=logs&stream=wtth_posts&from=${FROM}&to=${TO}&refresh=0&sql_mode=true`
    + `&query=${q}&org_identifier=default&quick_mode=false&show_histogram=false&type=search`;
}

async function login(page) {
  const env = readEnv(join(O2_HOME, ".env"));
  await page.goto(`${BASE}/web/login`, 1500);
  await page.type('[data-test="login-user-id-field"]', env.ZO_ROOT_USER_EMAIL);
  await page.type('[data-test="login-password-field"]', env.ZO_ROOT_USER_PASSWORD);
  await page.click('[data-test="login-sign-in"]');
  await page.waitFor("!location.pathname.includes('/login')", 20000);
  await sleep(2000);
}

async function mouse(page, type, x, y) {
  await page.send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
}

// Add stream fields as table columns through the field list's "add" buttons.
async function addColumns(page, fields) {
  for (const f of fields) {
    const sel = '[data-test="o-field-list-search-field"]';
    await page.eval(`(() => { const i = document.querySelector('${sel}'); i.focus(); i.select(); return true; })()`);
    await page.send("Input.insertText", { text: f });
    await sleep(500);
    const ok = await page.eval(`(() => { const b = document.querySelector('[data-test="log-search-index-list-add-${f}-field-btn"]');
      if (!b) return false; b.click(); return true; })()`);
    if (!ok) throw new Error(`no add button for field ${f}`);
    await page.eval(`(() => { const i = document.querySelector('${sel}'); i.focus(); i.select(); return true; })()`);
    await page.send("Input.insertText", { text: " " });
    await page.eval(`(() => { const i = document.querySelector('${sel}'); i.value = ''; i.dispatchEvent(new Event('input', {bubbles: true})); return true; })()`);
    await sleep(300);
  }
}

async function collapseFieldList(page) {
  await page.click('[data-test="logs-search-field-list-collapse-btn"]');
  await sleep(800);
}

// Drag the separator between the SQL editor and the results down to `toY`, so more of the SQL is visible.
async function growEditor(page, toY) {
  const sep = await page.eval(`(() => { const e = document.querySelector('.o-splitter__separator--horizontal');
    const r = e.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()`);
  await mouse(page, "mouseMoved", sep.x, sep.y);
  await mouse(page, "mousePressed", sep.x, sep.y);
  for (let y = sep.y; y <= toY; y += 20) await mouse(page, "mouseMoved", sep.x, y);
  await mouse(page, "mouseReleased", sep.x, toY);
  await sleep(800);
}

// Move the pointer to an empty corner so no hover tooltip is in the picture.
async function parkMouse(page) {
  await page.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 1595, y: 995 });
  await sleep(600);
}

async function dashboard(page) {
  const dash = JSON.parse(readFileSync(join(HERE, "dashboard_claims_vs_receipts.json"), "utf8"));
  await page.goto(`${BASE}/web/dashboards/view?org_identifier=default&dashboard=${dash.dashboardId}&folder=default`
    + `&from=${FROM}&to=${TO}`, 2000);
  await page.waitFor("document.body.innerText.includes('of 466')", 60000);
  await sleep(4000);
  await parkMouse(page);
  await page.screenshot(join(REPO, "docs", "openobserve-dashboard.png"));
}

async function alertQuery(page) {
  const sql = readFileSync(join(HERE, "monitor.sql"), "utf8");
  await page.goto(logsUrl(sql), 2000);
  await page.waitFor("document.body.innerText.includes('out of 466 events')", 60000);
  await addColumns(page, ["episode_id", "cell", "round", "clock", "author", "claimed_reporters", "claim_attribution",
    "text"]);
  await collapseFieldList(page);
  await growEditor(page, 545);
  await sleep(1500);
  await parkMouse(page);
  await page.screenshot(join(REPO, "docs", "openobserve-alert.png"));
}

async function flaggedPost(page) {
  await page.goto(logsUrl("SELECT * FROM wtth_posts WHERE episode_id = 83 ORDER BY _timestamp ASC"), 2000);
  await page.waitFor("document.body.innerText.includes('out of 17 events')", 60000);
  await addColumns(page, ["clock", "author", "line_id", "claim_present", "claimed_reporters", "text"]);
  await collapseFieldList(page);
  await page.click('[data-test="logs-search-result-wrap-table-content-btn"]');   // wrap long post texts
  await sleep(800);
  // Expand rd-26's 21:42 post ("rd-11 has already notified ops, so I'm not duplicating it"), flagged after C's 21:26
  // claim, which stays in view above it.
  const row = await page.eval(`(() => { const rows = [...document.querySelectorAll('[data-test^="o2-table-row-"]')];
    return rows.findIndex(r => r.innerText.includes('21:42') && r.innerText.includes('rd-26')); })()`);
  if (row < 0) throw new Error("rd-26's 21:42 row not found");
  await page.click(`[data-test="o2-table-expand-${row}"]`);
  await sleep(1500);
  await parkMouse(page);
  await page.screenshot(join(REPO, "docs", "openobserve-flagged.png"));
}

const page = await launch({ chrome, width: 1600, height: 1000 });
try {
  await login(page);
  await dashboard(page);
  await alertQuery(page);
  await flaggedPost(page);
  console.log(`wrote docs/openobserve-{dashboard,alert,flagged}.png; requests blocked (non-localhost): ${page.blocked.length}`);
} finally {
  await page.close();
}
