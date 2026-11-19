"use strict";
const { test, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const { payload, sparsePayload } = require("./fixtures");

const WEB = path.join(__dirname, "../../src/ccswap/menubar_web");
// CCSWAP_APP_JS lets a one-off run point at a scratch copy (used to prove the tests bite).
const APP_JS = process.env.CCSWAP_APP_JS || path.join(WEB, "app.js");
const HTML = fs.readFileSync(path.join(WEB, "index.html"), "utf8").replace(/<(script|link)\b[^>]*>(<\/script>)?/g, "");

let w, doc, posts;

// By default the fake bridge behaves like the app: frames are immediate and every action
// gets its reply. Busy-state tests pass { live: true } and drive both by hand.
function boot(p = payload(), { live = false } = {}) {
  if (w) w.close(); // each window owns a 30 s setInterval
  const dom = new JSDOM(HTML, { runScripts: "outside-only", pretendToBeVisual: true });
  w = dom.window;
  doc = w.document;
  posts = [];
  // jsdom has no matchMedia, WKWebView bridge or layout; stub only those.
  w.matchMedia = () => ({ matches: false, addEventListener() {} });
  w.webkit = { messageHandlers: { ccswap: { postMessage: (m) => {
    posts.push(JSON.parse(JSON.stringify(m)));
    if (!live && m.action !== "ready" && w.ccswap) w.ccswap.reply({ ok: true, action: m.action });
  } } } };
  if (!live) w.requestAnimationFrame = (cb) => { cb(0); return 0; };
  w.Range.prototype.getBoundingClientRect = () => ({ width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0 });
  w.eval(fs.readFileSync(APP_JS, "utf8"));
  w.ccswap.render(p);
  return w;
}
afterEach(() => { if (w) w.close(); w = null; });

const $ = (s) => doc.querySelector(s);
const $$ = (s) => [...doc.querySelectorAll(s)];
const tabBtn = (name) => $$("#tabs button").find((b) => b.getAttribute("aria-label") === name || b.textContent.trim() === name);
const btn = (text, root = doc) => [...root.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
const btns = (text) => $$("button").filter((b) => b.textContent.trim() === text);
const click = (el) => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
const open = (name) => click(tabBtn(name));
const card = (title) => $$("section.card").find((c) => c.textContent.includes(title));
const lastPost = () => posts[posts.length - 1];
const tn = (b) => b.getAttribute("aria-label") || b.textContent.trim();
const VIEWS = ["All", "Claude Code", "Codex", "Antigravity"];

test("boot posts ready", () => {
  boot();
  assert.deepEqual(posts[0], { action: "ready" });
});

test("header and provider strip are in every view, never a back button", () => {
  boot();
  for (const v of [...VIEWS, "settings"]) {
    if (v === "settings") click($("#f-settings")); else open(v);
    assert.ok($("#head .title") && $("#head .title").textContent === "ccswap", `header in ${v}`);
    assert.deepEqual($$("#tabs button").map((b) => tn(b)), VIEWS, `strip in ${v}`);
    assert.ok(!$$("button").some((b) => /back/i.test(b.textContent + b.title + (b.getAttribute("aria-label") || ""))), `no back in ${v}`);
  }
});

test("clicking a strip logo switches view and marks only that tab sel", () => {
  boot();
  open("Codex");
  assert.deepEqual($$("#tabs button.sel").map((b) => tn(b)), ["Codex"]);
  assert.ok($("#view").textContent.includes("c@example.com"));
  assert.ok(card("Switch account"));
  open("All");
  assert.deepEqual($$("#tabs button.sel").map((b) => tn(b)), ["All"]);
  assert.equal($$("#view .pcard").length, 3);
});

test("switch card only with strategies and more than one account", () => {
  boot();
  open("Claude Code");
  assert.ok(card("Switch account"));
  open("Antigravity"); // no strategies, one account
  assert.ok(!card("Switch account"));
  w.close();
  const p = payload();
  p.providers[0].accounts.pop(); // strategies but a single account
  boot(p);
  open("Claude Code");
  assert.ok(!card("Switch account"));
});

test("switch card: default strategy, caption, rotate post", () => {
  boot();
  open("Claude Code");
  const sel = () => $$("[role=radio]").filter((b) => b.classList.contains("sel")).map((b) => b.textContent);
  assert.deepEqual(sel(), ["Consume first"]); // payload settings.strategy
  assert.match(card("Switch account").textContent, /weekly limit resets soonest/);
  click(btn("Best"));
  assert.deepEqual(sel(), ["Best"]);
  assert.match(card("Switch account").textContent, /most room left/);
  click(btn("Switch", card("Switch account")));
  assert.deepEqual(lastPost(), { action: "rotate", provider: "claude", strategy: "best" });
});

test("switch card falls back to the provider's first strategy when settings.strategy is unsupported", () => {
  boot();
  open("Codex"); // the fixture gives Codex best + next-available only, to reach the fallback
  const sel = $$("[role=radio].sel").map((b) => b.textContent);
  assert.deepEqual(sel, ["Best"]);
  click(btn("Next available"));
  click(btn("Switch", card("Switch account")));
  assert.deepEqual(lastPost(), { action: "rotate", provider: "codex", strategy: "next-available" });
  boot();
  open("Codex");
  click(btn("Switch", card("Switch account")));
  assert.deepEqual(lastPost(), { action: "rotate", provider: "codex", strategy: "best" });
});

test("+ Add menu: Current login, and token entry only where addToken", () => {
  boot();
  open("Claude Code");
  click(btn("+ Add"));
  assert.deepEqual($$(".menu .choice").map((c) => c.firstChild.textContent), ["Current login", "Setup token or API key"]);
  click($$(".choice")[0]);
  assert.deepEqual(lastPost(), { action: "addLogin", provider: "claude" });
  assert.equal($$(".menu").length, 0);

  open("Codex");
  click(btn("+ Add"));
  assert.deepEqual($$(".menu .choice").map((c) => c.firstChild.textContent), ["Current login"]);
  assert.equal($(".tokform"), null);
});

test("token form: Add disabled until token typed, email optional, posts addToken", () => {
  boot();
  open("Claude Code");
  click(btn("+ Add"));
  click($$(".choice")[1]);
  assert.ok($(".tokform"));
  const add = btn("Add", $(".tokform"));
  assert.equal(add.disabled, true);
  const tok = $("#tok");
  tok.value = "   ";
  tok.dispatchEvent(new w.Event("input"));
  assert.equal(add.disabled, true);
  tok.value = " sk-fake ";
  tok.dispatchEvent(new w.Event("input"));
  assert.equal(add.disabled, false);
  click(add); // email left empty
  assert.deepEqual(lastPost(), { action: "addToken", provider: "claude", email: "", token: "sk-fake" });
  assert.equal($(".tokform"), null);

  click(btn("+ Add"));
  click($$(".choice")[1]);
  $("#tok").value = "t2";
  $("#tok").dispatchEvent(new w.Event("input"));
  $("#tokmail").value = "n@example.com";
  click(btn("Add", $(".tokform")));
  assert.deepEqual(lastPost(), { action: "addToken", provider: "claude", email: "n@example.com", token: "t2" });
});

test("Other accounts rows: Switch, Disable/Enable by capability, Remove always", () => {
  const p = payload();
  p.providers[0].accounts[1].disabled = true;
  boot(p);
  open("Claude Code"); // disable capable
  const row = () => card("Other accounts").querySelector(".acctwrap");
  const names = () => [...row().querySelectorAll("button")].map((b) => b.textContent);
  assert.deepEqual(names(), ["Switch", "Enable", "Remove"]);
  click(btn("Switch", row()));
  assert.deepEqual(lastPost(), { action: "switch", provider: "claude", number: 2 });
  click(btn("Enable", row()));
  assert.deepEqual(lastPost(), { action: "toggleDisabled", provider: "claude", number: 2, disabled: false });
  click(btn("Remove", row()));
  assert.deepEqual(lastPost(), { action: "remove", provider: "claude", number: 2 });

  open("Codex"); // not disable capable
  assert.deepEqual(names(), ["Switch", "Remove"]);
});

test("Codex switch needs a confirm click (restartAfterSwitch)", () => {
  boot();
  open("Codex");
  const row = () => card("Other accounts");
  click(btn("Switch", row()));
  assert.equal(posts.filter((m) => m.action === "switch").length, 0);
  click(btn("Restart sessions? Confirm", row()));
  assert.deepEqual(lastPost(), { action: "switch", provider: "codex", number: 2 });
});

test("Active account card: Refresh credentials only claude, Disable by capability, Remove always", () => {
  boot();
  const names = () => [...card("Active account").querySelectorAll("button")].map((b) => b.textContent);
  open("Claude Code");
  assert.deepEqual(names(), ["Refresh credentials", "Disable", "Remove"]);
  click(btn("Refresh credentials", card("Active account")));
  assert.deepEqual(lastPost(), { action: "refreshCreds" });
  click(btn("Disable", card("Active account")));
  assert.deepEqual(lastPost(), { action: "toggleDisabled", provider: "claude", number: 1, disabled: true });
  click(btn("Remove", card("Active account")));
  assert.deepEqual(lastPost(), { action: "remove", provider: "claude", number: 1 });
  open("Codex");
  assert.deepEqual(names(), ["Remove"]);
  open("Antigravity");
  assert.deepEqual(names(), ["Remove"]);
});

test("reply(): note shows, error shows, cancelled shows nothing", () => {
  boot();
  w.ccswap.reply({ ok: true, note: "Switched to b@example.com" });
  assert.match($("#view .card.note").textContent, /Switched to b@example.com/);
  w.close();

  boot();
  w.ccswap.reply({ ok: false, error: "Keychain locked" });
  assert.match($("#view .card.err").textContent, /Keychain locked/);
  w.ccswap.reply({ ok: false });
  assert.match($("#view .card.err").textContent, /Action failed/);
  w.close();

  boot();
  const before = $("#view").textContent;
  w.ccswap.reply({ ok: false, cancelled: true });
  assert.equal($("#view").textContent, before);
  assert.equal($$("#view .card.err, #view .card.note").length, 0);
});

for (const [name, make] of [["full", payload], ["sparse", sparsePayload]]) {
  test(`no element renders "null" or "undefined" (${name} payload)`, () => {
    boot(make());
    const check = (where) => {
      const bad = $$("body *").filter((e) => [...e.childNodes].some((n) => n.nodeType === 3 && /\b(null|undefined)\b/.test(n.nodeValue)));
      assert.deepEqual(bad.map((e) => e.outerHTML), [], where);
    };
    for (const v of VIEWS) { open(v); check(v); }
    click($("#f-settings")); check("settings");
  });
}

test("sparse payload still lists the provider error", () => {
  boot(sparsePayload());
  assert.match($("#view").textContent, /usage fetch failed/);
  open("Codex");
  assert.match($("#view").textContent, /usage fetch failed/);
});

test("settings gear is sel while Settings is open; second click returns to All", () => {
  boot();
  const gear = $("#f-settings");
  assert.ok(!gear.classList.contains("sel"));
  click(gear);
  assert.ok(gear.classList.contains("sel"));
  assert.equal(gear.getAttribute("aria-pressed"), "true");
  assert.match($("#view").textContent, /Menu bar title/);
  assert.deepEqual($$("#tabs button.sel"), []);
  click(gear);
  assert.ok(!gear.classList.contains("sel"));
  assert.deepEqual($$("#tabs button.sel").map((b) => tn(b)), ["All"]);
  assert.equal($$("#view .pcard").length, 3);
});

test("strip: each provider's logo sits in a ring filled to its active account's tightest window", () => {
  const p = payload();
  boot(p);
  for (const prov of p.providers) {
    const b = tabBtn(prov.label);
    const active = prov.accounts.find((a) => a.active);
    const pcts = (active ? active.windows : []).filter((w) => !w.scope && typeof w.pct === "number").map((w) => w.pct);
    const top = pcts.length ? Math.max(...pcts) : 0;
    assert.ok(b.querySelector(".tring img"), `${prov.id} logo inside the ring`);
    const arc = b.querySelector(".tring .a");
    if (top > 0) {
      assert.equal(arc.getAttribute("stroke-dasharray"), `${Math.max(Math.min(top, 100), 2)} 100`, prov.id);
      const lvl = top >= 90 ? "bad" : top >= 70 ? "warn" : "ok";
      assert.ok(b.querySelector(".tring").classList.contains(lvl), `${prov.id} is ${lvl}`);
      assert.match(b.title, new RegExp(`${prov.label}: ${Math.round(top)}% of the`));
    } else {
      assert.equal(arc, null, `${prov.id} draws no arc at 0%`);
    }
    assert.equal(b.getAttribute("aria-label"), prov.label);
  }
  assert.equal($$("#tabs .mini").length, 0); // the old line under the logo is gone
  assert.equal(tabBtn("All").querySelector(".tring"), null);
});

const frames = () => new Promise((r) => w.requestAnimationFrame(() => w.requestAnimationFrame(() => setTimeout(r, 0))));

test("an action shows its busy label until the reply, and blocks other actions", async () => {
  boot(payload(), { live: true });
  open("Claude Code");
  const before = posts.length;
  click(btn("Switch", card("Switch account")));
  const busyBtn = $("button.busy");
  assert.equal(busyBtn.textContent, "Switching…");
  assert.equal(busyBtn.getAttribute("aria-busy"), "true");
  assert.ok(btns("Remove").every((b) => b.disabled), "other actions wait");
  assert.equal(posts.length, before, "posted only after the busy frame is drawn");
  await frames();
  assert.equal(lastPost().action, "rotate");
  click(btns("Remove")[0]); // ignored while busy
  assert.equal(lastPost().action, "rotate");
  w.ccswap.reply({ ok: true, action: "rotate" });
  assert.equal($("button.busy"), null);
  assert.ok(btns("Remove").every((b) => !b.disabled));
});

test("+ Add shows Adding… while the current login is saved", async () => {
  boot(payload(), { live: true });
  open("Claude Code");
  click(btn("+ Add"));
  click($$(".choice")[0]);
  assert.equal($("button.busy").textContent, "Adding…");
  await frames();
  assert.deepEqual(lastPost(), { action: "addLogin", provider: "claude" });
  w.ccswap.reply({ ok: true, action: "addLogin", note: "Claude Code: account 3 added. c@example.com" });
  assert.ok(btn("+ Add"));
  assert.match($("#view").textContent, /account 3 added/);
});

test("refresh spins and the footer says Checking… while any provider is fetching", () => {
  const p = payload();
  p.providers[1].refreshing = true;
  boot(p);
  assert.equal($("#checked").textContent, "Checking…");
  assert.ok($("#f-refresh").classList.contains("spin"));
  const q = payload();
  q.providers.forEach((x) => { x.refreshing = false; });
  w.ccswap.render(q);
  assert.match($("#checked").textContent, /^(Checked|Not checked)/);
  assert.ok(!$("#f-refresh").classList.contains("spin"));
});

test("header mark fills with the title provider's tightest window, like the menu bar glyph", () => {
  boot();
  const fill = $("#head .tile path.mf");
  assert.ok(fill, "fill path drawn");
  assert.equal(fill.getAttribute("stroke-dasharray"), "30 101"); // claude active: 5h 12%, weekly 30%
  const p = payload();
  p.title.resolved = "agy"; // active agy account has no windows
  boot(p);
  assert.ok($("#head .tile path.mt") && !$("#head .tile path.mf"), "track only when nothing is used");
});
