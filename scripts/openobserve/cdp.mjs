// Minimal Chrome DevTools Protocol driver for headless screenshots (Node >= 22: built-in WebSocket, no npm packages).
//
// launch() starts a headless Chrome with a throwaway profile, resolves no host name except localhost and fails every
// request that is not to 127.0.0.1 or localhost (blocked URLs are collected in page.blocked). The page renders in UTC
// at the requested size.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_CHROME = join(homedir(),
  ".cache/puppeteer/chrome-headless-shell/mac_arm-150.0.7871.24/chrome-headless-shell-mac-arm64/chrome-headless-shell");

const LOCAL = /^(https?|wss?):\/\/(127\.0\.0\.1|localhost)(:\d+)?\//;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch({ chrome = DEFAULT_CHROME, width = 1600, height = 1000 } = {}) {
  const profile = mkdtempSync(join(tmpdir(), "wtth-chrome-"));
  const proc = spawn(chrome, [
    "--headless", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--no-first-run",
    "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
    "--disable-component-update", "--disable-sync", "--disable-default-apps", "--metrics-recording-only",
    "--mute-audio", "--hide-scrollbars", "--force-device-scale-factor=1", "--lang=en-US",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
    `--window-size=${width},${height}`, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = "";
    const t = setTimeout(() => reject(new Error("Chrome did not start")), 20000);
    proc.stderr.on("data", (d) => {
      buf += d.toString();
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(t); resolve(m[1]); }
    });
    proc.on("exit", (code) => reject(new Error(`Chrome exited (${code})`)));
  });
  const ws = new WebSocket(wsUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(`${msg.error.message} ${msg.error.data || ""}`)) : resolve(msg.result);
    } else if (msg.method) {
      for (const l of listeners) l(msg);
    }
  };
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  const page = new Page(send, sessionId, listeners);
  await page.init(width, height);
  page.close = async () => {
    try { await send("Browser.close"); } catch { /* already gone */ }
    proc.kill();
    await sleep(300);
    rmSync(profile, { recursive: true, force: true });
  };
  return page;
}

class Page {
  constructor(send, sessionId, listeners) {
    this.send = (m, p) => send(m, p, sessionId);
    this.sessionId = sessionId;
    this.blocked = [];
    listeners.push((msg) => {
      if (msg.sessionId !== sessionId) return;
      if (msg.method === "Fetch.requestPaused") this.onPaused(msg.params);
    });
  }

  async init(width, height) {
    await this.send("Page.enable");
    await this.send("Runtime.enable");
    await this.send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
    await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    await this.send("Emulation.setTimezoneOverride", { timezoneId: "UTC" });
    await this.send("Emulation.setLocaleOverride", { locale: "en-US" });
  }

  onPaused({ requestId, request }) {
    const url = request.url;
    if (LOCAL.test(url) || url.startsWith("data:") || url.startsWith("blob:")) {
      this.send("Fetch.continueRequest", { requestId }).catch(() => {});
    } else {
      this.blocked.push(url);
      this.send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }).catch(() => {});
    }
  }

  async goto(url, settleMs = 1500) {
    await this.send("Page.navigate", { url });
    await this.waitFor("document.readyState === 'complete'", 30000);
    await sleep(settleMs);
  }

  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(`page error: ${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description || ""}`);
    return r.result.value;
  }

  async waitFor(expression, timeout = 30000) {
    const t0 = Date.now();
    for (;;) {
      try { if (await this.eval(`Boolean(${expression})`)) return; } catch { /* page still loading */ }
      if (Date.now() - t0 > timeout) throw new Error(`timeout waiting for: ${expression}`);
      await sleep(250);
    }
  }

  // Focus the element matched by a CSS selector and type text into it (real key input, so frameworks see it).
  async type(selector, text) {
    await this.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); el.focus(); el.select && el.select(); return true; })()`);
    await this.send("Input.insertText", { text });
  }

  async click(selector) {
    const box = await this.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null; el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()`);
    if (!box) throw new Error(`no element for ${selector}`);
    for (const type of ["mousePressed", "mouseReleased"]) {
      await this.send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
    }
  }

  async key(key, code = key, keyCode = 13) {
    for (const type of ["keyDown", "keyUp"]) {
      await this.send("Input.dispatchKeyEvent", { type, key, code, windowsVirtualKeyCode: keyCode });
    }
  }

  async screenshot(path, clip) {
    const params = { format: "png", captureBeyondViewport: false };
    if (clip) params.clip = { ...clip, scale: 1 };
    const { data } = await this.send("Page.captureScreenshot", params);
    writeFileSync(path, Buffer.from(data, "base64"));
  }
}

export { sleep };
