#!/usr/bin/env node
// Pull the Centium calendar through the logged-in BrowserOS profile.
// Google Workspace blocks the secret iCal address for this tenant, so the only
// read path left is the authenticated export endpoint.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

const CFG = path.join(os.homedir(), "Library/Application Support/BrowserClaw/.browseros/config.json");
const CACHE = path.join(os.homedir(), ".cache/meetnext");
const OUT = path.join(CACHE, "centium.ics");
const ACCOUNT_FILE = path.join(os.homedir(), ".config/meetnext/centium.email");

function account() {
  if (process.env.MEETNEXT_EMAIL) return process.env.MEETNEXT_EMAIL.trim();
  try {
    return fs.readFileSync(ACCOUNT_FILE, "utf8").trim();
  } catch {
    throw new Error(`set MEETNEXT_EMAIL or write the address to ${ACCOUNT_FILE}`);
  }
}

const EMAIL = account();
const AUTHUSER = process.env.MEETNEXT_AUTHUSER ?? "2";

function cdpPort() {
  try {
    return JSON.parse(fs.readFileSync(CFG, "utf8")).ports?.cdp ?? 9114;
  } catch {
    return 9114;
  }
}

async function browserCookies() {
  const version = await (await fetch(`http://127.0.0.1:${cdpPort()}/json/version`)).json();
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((ok, err) => {
    ws.onopen = ok;
    ws.onerror = err;
  });
  const { result, error } = await new Promise((ok) => {
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id === 1) ok(msg);
    };
    ws.send(JSON.stringify({ id: 1, method: "Storage.getCookies" }));
  });
  ws.close();
  if (error) throw new Error(`CDP: ${error.message}`);

  const host = "calendar.google.com";
  const jar = new Map();
  for (const cookie of result?.cookies ?? []) {
    const domain = cookie.domain.replace(/^\./, "");
    if (domain !== host && domain !== "google.com") continue;
    const prev = jar.get(cookie.name);
    if (!prev || domain.length > prev.domain.length) {
      jar.set(cookie.name, { domain, value: cookie.value });
    }
  }
  if (!jar.has("SID") || !jar.has("__Secure-1PSID")) {
    throw new Error("no Google session in the browser profile — sign in to calendar.google.com");
  }
  return [...jar].map(([name, c]) => `${name}=${c.value}`).join("; ");
}

async function download(cookie) {
  const cexp = Buffer.from(EMAIL).toString("base64").replace(/=+$/, "");
  const url = `https://calendar.google.com/calendar/u/${AUTHUSER}/exporticalzip?cexp=${cexp}`;
  const res = await fetch(url, {
    headers: { cookie, "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" },
    redirect: "follow",
  });
  if (!res.ok) throw new Error(`export failed: HTTP ${res.status}`);
  const body = Buffer.from(await res.arrayBuffer());
  if (body.subarray(0, 2).toString() !== "PK") {
    throw new Error("export returned HTML instead of a zip — the browser session expired");
  }
  return body;
}

function extractIcs(zipPath, dir) {
  execFileSync("/usr/bin/unzip", ["-o", "-q", zipPath, "-d", dir]);
  const names = fs.readdirSync(dir).filter((f) => f.toLowerCase().endsWith(".ics"));
  const primary = names.find((f) => f.toLowerCase() === `${EMAIL.toLowerCase()}.ics`) ?? names[0];
  if (!primary) throw new Error("export archive has no .ics");
  return path.join(dir, primary);
}

const cookie = await browserCookies();
const body = await download(cookie);
fs.mkdirSync(CACHE, { recursive: true });

const tmpZip = path.join(CACHE, ".centium-export.zip");
const tmpDir = fs.mkdtempSync(path.join(CACHE, ".centium-"));
fs.writeFileSync(tmpZip, body);
try {
  const icsPath = extractIcs(tmpZip, tmpDir);
  const events = (fs.readFileSync(icsPath, "utf8").match(/BEGIN:VEVENT/g) ?? []).length;
  if (!events) throw new Error("exported calendar has no events");
  fs.copyFileSync(icsPath, `${OUT}.tmp`);
  fs.renameSync(`${OUT}.tmp`, OUT);
  console.log(`${OUT}: ${events} events`);
} finally {
  fs.rmSync(tmpZip, { force: true });
  fs.rmSync(tmpDir, { recursive: true, force: true });
}
