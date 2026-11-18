"use strict";
// Page for the ccswap menu bar popover. All user strings go through textContent.
(function () {
  const ACCENT = { claude: "--claude", codex: "--codex", agy: "--antigravity" };
  const TITLE_PCT = ["off", "5h", "7d", "both"];
  let data = null;
  let tab = "overview";
  let pending = null; // {provider, number}: switch awaiting a second click
  let note = null; // {text, until}: reply note (restart hint, errors)
  let dirty = false;

  const post = (msg) => window.webkit.messageHandlers.ccswap.postMessage(msg);

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) el.append(kid);
    return el;
  }
  const svgNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  const level = (pct) => (pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok");
  const round = (n) => Math.round(n);

  function countdown(iso) {
    if (!iso) return "";
    const ms = Date.parse(iso) - Date.now();
    if (!isFinite(ms)) return "";
    if (ms <= 0) return "resetting";
    const m = Math.floor(ms / 60000);
    const d = Math.floor(m / 1440), hr = Math.floor((m % 1440) / 60);
    if (d) return `${d}d ${hr}h`;
    return hr ? `${hr}h ${m % 60}m` : `${m}m`;
  }

  function windowsOf(acct) {
    return Array.isArray(acct.windows) ? acct.windows : [];
  }
  // Account-wide windows when there are any, else every window (Antigravity is scoped only).
  function mainWindows(acct) {
    const w = windowsOf(acct);
    const wide = w.filter((x) => !x.scope);
    return wide.length ? wide : w;
  }
  function tightest(acct) {
    const w = mainWindows(acct).filter((x) => typeof x.pct === "number");
    return w.length ? w.reduce((a, b) => (b.pct > a.pct ? b : a)) : null;
  }

  function ring(pct, size) {
    const r = size / 2 - 5, c = 2 * Math.PI * r;
    const used = pct == null ? 0 : Math.min(100, Math.max(0, pct));
    const box = h("div", { class: "ring " + (pct == null ? "" : level(pct)), style: `width:${size}px;height:${size}px` });
    const s = svg("svg", { width: size, height: size });
    s.append(svg("circle", { cx: size / 2, cy: size / 2, r, fill: "none", stroke: "var(--track)", "stroke-width": 5 }));
    s.append(svg("circle", {
      cx: size / 2, cy: size / 2, r, fill: "none", stroke: pct == null ? "none" : "var(--level)",
      "stroke-width": 5, "stroke-linecap": "round", "stroke-dasharray": `${(c * used) / 100} ${c}`,
    }));
    box.append(s, h("b", { text: pct == null ? "–" : `${round(pct)}%` }));
    return box;
  }

  function bar(pct) {
    const i = h("i", { style: `width:${Math.min(100, Math.max(0, pct || 0))}%;background:var(--level)` });
    return h("div", { class: "bar " + level(pct || 0) }, i);
  }

  function fullWindow(w) {
    const reset = h("span", { class: "muted", "data-reset": w.resetsAt || "", text: countdown(w.resetsAt) });
    return h("div", { class: "win " + level(w.pct || 0) },
      h("div", { class: "head" }, h("span", { text: w.label || w.id }), h("span", { text: `${round(w.pct || 0)}%` })),
      bar(w.pct),
      reset);
  }

  const label = (acct) => acct.alias || acct.email || `#${acct.number}`;

  function accent(el, prov) {
    el.style.setProperty("--accent", `var(${ACCENT[prov.id] || "--muted"})`);
    return el;
  }

  function doSwitch(prov, acct) {
    const key = prov.id + ":" + acct.number;
    if (prov.capabilities.restartAfterSwitch && !(pending && pending.key === key)) {
      pending = { key };
      draw();
      return;
    }
    pending = null;
    post({ action: "switch", provider: prov.id, number: acct.number });
  }

  function switchButton(prov, acct) {
    if (!prov.capabilities.switch || acct.active) return null;
    const armed = pending && pending.key === prov.id + ":" + acct.number;
    return h("button", { text: armed ? "Restart sessions? Confirm" : "Switch", class: armed ? "on" : "",
      onclick: () => doSwitch(prov, acct) });
  }

  function compactRow(prov, acct) {
    const t = tightest(acct);
    return h("div", { class: "row" + (acct.active ? " active" : "") },
      h("span", { class: "name", text: label(acct) + (acct.disabled ? " (disabled)" : "") }),
      t ? h("span", { class: "muted", "data-reset": t.resetsAt || "", text: countdown(t.resetsAt) }) : null,
      h("span", { text: t ? `${round(t.pct)}%` : "–" }),
      switchButton(prov, acct));
  }

  function autoPill(prov) {
    if (!prov.capabilities.autoSwitch || !prov.autoSwitch) return null;
    const on = prov.autoSwitch.enabled;
    return h("button", { class: on ? "on" : "", title: prov.autoSwitch.failure || "Auto-switch",
      text: "Auto " + (on ? "on" : "off"),
      onclick: () => post({ action: "toggleAuto", provider: prov.id, enabled: !on }) });
  }

  function overviewCard(prov) {
    const card = accent(h("section", { class: "card" }), prov);
    card.append(h("h3", {}, h("span", { class: "dot" }), h("span", { text: prov.label }),
      h("span", { class: "spacer" }), prov.refreshing ? h("span", { class: "muted", text: "refreshing" }) : null,
      autoPill(prov)));
    if (prov.error) card.append(h("div", { class: "err", text: prov.error }));
    const accounts = prov.accounts || [];
    const active = accounts.find((a) => a.active);
    if (active) {
      const t = tightest(active);
      card.append(h("div", { class: "big" }, ring(t ? t.pct : null, 56),
        h("div", {}, h("div", { text: label(active) }),
          h("div", { class: "muted", "data-reset": t ? t.resetsAt || "" : "", text: t ? `${t.label} resets ${countdown(t.resetsAt)}` : "" }),
          active.note ? h("div", { class: "note", text: active.note }) : null)));
    } else if (!prov.error) card.append(h("div", { class: "muted", text: "No active account" }));
    for (const a of accounts.filter((x) => !x.active)) card.append(compactRow(prov, a));
    return card;
  }

  function providerTab(prov) {
    const wrap = accent(h("div", {}), prov);
    const caps = prov.capabilities;
    if (prov.error) wrap.append(h("div", { class: "card err", text: prov.error }));
    for (const a of prov.accounts || []) {
      const card = h("section", { class: "card" });
      card.append(h("h3", {}, h("span", { text: label(a) }), h("span", { class: "spacer" }),
        a.active ? h("span", { class: "muted", text: "active" }) : null, switchButton(prov, a)));
      if (a.note) card.append(h("div", { class: "note", text: a.note }));
      if (a.loginNote) card.append(h("div", { class: "muted", text: a.loginNote }));
      for (const w of windowsOf(a)) card.append(fullWindow(w));
      if (caps.disable) {
        card.append(h("div", { class: "actions" },
          h("button", { text: a.disabled ? "Enable" : "Disable",
            onclick: () => post({ action: "toggleDisabled", provider: prov.id, number: a.number, disabled: !a.disabled }) }),
          h("button", { text: "Remove", onclick: () => post({ action: "remove", provider: prov.id, number: a.number }) })));
      }
      wrap.append(card);
    }
    const bottom = h("div", { class: "actions" });
    for (const s of caps.strategies || []) {
      bottom.append(h("button", { text: s, onclick: () => post({ action: "rotate", provider: prov.id, strategy: s }) }));
    }
    bottom.append(h("button", { text: "Add account", onclick: () => post({ action: "addLogin", provider: prov.id }) }));
    if (prov.addToken) bottom.append(tokenForm(prov));
    const auto = autoPill(prov);
    if (auto) bottom.append(auto, h("button", { text: "Restart engine", onclick: () => post({ action: "restartEngine", provider: prov.id }) }));
    wrap.append(h("div", { class: "card" }, bottom));
    return wrap;
  }

  function tokenForm(prov) {
    const email = h("input", { type: "email", placeholder: "email", autocomplete: "off" });
    const token = h("input", { type: "password", placeholder: "token", autocomplete: "off" });
    return h("div", { class: "actions" }, email, token, h("button", { text: "Add token", onclick: () => {
      if (!email.value || !token.value) return;
      post({ action: "addToken", provider: prov.id, email: email.value, token: token.value });
      token.value = "";
    } }));
  }

  function toggle(text, on, action, extra) {
    return h("label", { class: "set" }, h("span", { text }),
      h("button", { class: on ? "on" : "", text: on ? "On" : "Off",
        onclick: () => post(Object.assign({ action }, extra || { value: !on })) }));
  }

  function select(text, options, current, action) {
    const sel = h("select", {}, options.map(([v, l]) => h("option", { value: v, text: l, selected: v === current })));
    sel.addEventListener("change", () => post({ action, value: sel.value }));
    return h("label", { class: "set" }, h("span", { text }), sel);
  }

  function numberInput(text, value, action, min, max) {
    const input = h("input", { type: "number", min, max, value });
    input.addEventListener("change", () => {
      const n = Number(input.value);
      if (Number.isFinite(n)) post({ action, value: n });
    });
    return h("label", { class: "set" }, h("span", { text }), input);
  }

  function settingsTab() {
    const s = data.settings;
    const wrap = h("div", { class: "card" });
    wrap.append(h("h4", { text: "Menu bar title" }));
    wrap.append(select("Provider", [["auto", "Auto (fullest)"]].concat(data.providers.map((p) => [p.id, p.label])),
      data.title.setting, "setTitleProvider"));
    wrap.append(select("Percentages", TITLE_PCT.map((v) => [v, v]), s.titlePct, "setTitlePct"));
    wrap.append(toggle("Account name", s.showAccountName, "toggleName"));
    wrap.append(toggle("Scoped limits", s.titleScoped, "toggleScoped"));
    wrap.append(h("h4", { text: "Behavior" }));
    wrap.append(numberInput("Refresh every (s)", s.refreshInterval, "setInterval", 10, 3600));
    wrap.append(numberInput("Auto-switch threshold (%)", s.threshold, "setThreshold", 1, 100));
    wrap.append(select("Style", [["popover", "Popover"], ["classic", "Classic menu"]], s.style, "setStyle"));
    for (const p of data.providers.filter((x) => x.capabilities.autoSwitch && x.autoSwitch)) {
      wrap.append(toggle(`Auto-switch: ${p.label}`, p.autoSwitch.enabled, "toggleAuto",
        { provider: p.id, enabled: !p.autoSwitch.enabled }));
    }
    const ci = s.claudeIntegrations || {};
    for (const key of Object.keys(ci)) wrap.append(toggle(`Claude ${key}`, ci[key], "toggleIntegration", { key }));
    wrap.append(h("div", { class: "actions" }, h("button", { text: "Open log", onclick: () => post({ action: "openLog" }) })));
    return wrap;
  }

  function draw() {
    if (!data) return;
    const tabs = document.getElementById("tabs");
    tabs.replaceChildren(
      tabButton("overview", "Overview"),
      ...data.providers.map((p) => tabButton(p.id, p.label)),
      tabButton("settings", "Settings"));
    const view = document.getElementById("view");
    const prov = data.providers.find((p) => p.id === tab);
    if (!prov && tab !== "overview" && tab !== "settings") tab = "overview";
    view.replaceChildren();
    if (note && note.until > Date.now()) view.append(h("div", { class: "card " + (note.bad ? "err" : "note"), text: note.text }));
    if (tab === "overview") for (const p of data.providers) view.append(overviewCard(p));
    else if (tab === "settings") view.append(settingsTab());
    else view.append(providerTab(prov));
    const at = data.lastCheckedAt;
    document.getElementById("checked").textContent = at ? "Checked " + new Date(at * 1000).toLocaleTimeString() : "Not checked yet";
    dirty = false;
  }

  function tabButton(id, text) {
    return h("button", { text, role: "tab", class: tab === id ? "sel" : "", onclick: () => { tab = id; pending = null; draw(); } });
  }

  function editing() {
    const a = document.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "SELECT");
  }

  function updateCountdowns() {
    for (const el of document.querySelectorAll("[data-reset]")) {
      const iso = el.getAttribute("data-reset");
      if (iso && el.closest(".big")) el.textContent = el.textContent.replace(/resets .*$/, "resets " + countdown(iso));
      else if (iso) el.textContent = countdown(iso);
    }
  }

  window.ccswap = {
    render(payload) {
      data = payload;
      if (editing()) { dirty = true; return; } // do not wipe a field the user is typing in
      draw();
    },
    reply(r) {
      if (r.restart && r.ok) note = { text: "Restart running sessions to use the new account.", until: Date.now() + 8000 };
      else if (!r.ok && !r.cancelled) note = { text: r.error || "Action failed", bad: true, until: Date.now() + 5000 };
      if (!editing()) draw();
    },
  };

  document.addEventListener("focusout", () => setTimeout(() => { if (dirty && !editing()) draw(); }, 0));
  document.getElementById("f-refresh").addEventListener("click", () => post({ action: "refresh" }));
  document.getElementById("f-settings").addEventListener("click", () => { tab = "settings"; draw(); });
  document.getElementById("f-quit").addEventListener("click", () => post({ action: "quit" }));
  setInterval(updateCountdowns, 30000);
  post({ action: "ready" });
})();
