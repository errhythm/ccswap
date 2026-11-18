"use strict";
// Page for the ccswap menu bar popover. All user strings go through textContent.
// The logos in logos/ are the providers' own marks, used only to identify the service.
(function () {
  // The DOM turns a null child into the text "null"; conditional children are
  // written as `cond ? el : null` throughout, so drop them at the source.
  for (const proto of [Element.prototype, DocumentFragment.prototype]) {
    for (const m of ["append", "prepend", "replaceChildren"]) {
      const orig = proto[m];
      proto[m] = function (...kids) { return orig.apply(this, kids.filter((k) => k != null)); };
    }
  }
  const COLORS = { claude: "#E07A5F", codex: "#C9C6C1", agy: "#7FA8FF" };
  const LOGOS = { claude: "logos/claude-ai-icon.svg", agy: "logos/antigravity.svg" };
  const TITLE_PCT = ["off", "5h", "7d", "both"];
  const REFRESH = [15, 30, 60, 120, 300, 600];
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  let data = null;
  let tab = "overview"; // "overview" | "settings" | provider id
  let pending = null; // {key}: switch awaiting a second click
  let tokenOpen = false; // the add-token form, opened from the Add menu
  let addOpen = false; // the Add menu on the Other accounts card
  const chosen = {}; // provider id -> strategy picked on its Switch card
  let note = null; // {text, until}: reply note (restart hint, errors)
  let dirty = false;

  const post = (msg) => window.webkit.messageHandlers.ccswap.postMessage(msg);

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "style") el.style.cssText = v; // the CSP blocks style attributes, not the CSSOM
      else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) el.append(kid);
    return el;
  }
  const svgNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, ...kids) {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    for (const kid of kids) if (kid != null) el.append(kid);
    return el;
  }

  const ICONS = {
    refresh: [["path", { d: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" }]],
    settings: [["path", { d: "M4 7h9M17 7h3M4 17h3M11 17h9" }], ["circle", { cx: 15, cy: 7, r: 2 }], ["circle", { cx: 9, cy: 17, r: 2 }]],
    power: [["path", { d: "M12 3v8M7 6.3a8 8 0 1 0 10 0" }]],
  };
  const icon = (name) => svg("svg", { viewBox: "0 0 24 24" }, ...ICONS[name].map(([t, a]) => svg(t, a)));

  const level = (pct) => (pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok");
  const round = (n) => Math.round(n);
  const label = (acct) => acct.alias || acct.email || `#${acct.number}`;
  const hhmm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

  function dur(ms) {
    const m = Math.floor(ms / 60000);
    const d = Math.floor(m / 1440), hr = Math.floor((m % 1440) / 60);
    if (d) return `${d}d ${hr}h`;
    return hr ? `${hr}h ${m % 60}m` : `${m}m`;
  }
  function countdown(iso) {
    const ms = iso ? Date.parse(iso) - Date.now() : NaN;
    if (!isFinite(ms)) return "";
    return ms <= 0 ? "resetting" : dur(ms);
  }
  function clock(iso) {
    const t = Date.parse(iso);
    if (!isFinite(t)) return "";
    const d = new Date(t);
    return t - Date.now() > 20 * 3600000 ? `${d.toLocaleDateString([], { weekday: "short" })} ${hhmm(d)}` : hhmm(d);
  }

  // --- window model: every window tagged 5h/wk and account-wide vs per-model ---
  function norm(w, model, kind) {
    const short = w.durationS ? w.durationS <= 21600 : /5h/i.test(`${w.id} ${w.label}`);
    return { w, model, kind: kind || (short ? "5h" : "wk"), label: w.label || w.id || "" };
  }
  function wins(acct) {
    const out = (Array.isArray(acct.windows) ? acct.windows : []).map((w) => norm(w, !!w.scope));
    if (!out.some((x) => x.model)) { // Claude lists per-model limits under usage.scoped
      for (const s of (acct.usage && acct.usage.scoped) || []) {
        out.push(norm(Object.assign({}, s, { label: s.window ? s.name : `${s.name} weekly` }), true, s.window === "5h" ? "5h" : "wk"));
      }
    }
    return out;
  }
  // Highest pct of a kind; account-wide windows win unless there are none (Antigravity is all scoped).
  function pick(ws, kind) {
    const k = ws.filter((x) => x.kind === kind && typeof x.w.pct === "number");
    const wide = k.filter((x) => !x.model);
    const pool = wide.length ? wide : k;
    return pool.length ? pool.reduce((a, b) => (b.w.pct > a.w.pct ? b : a)) : null;
  }
  function tight(acct) {
    const ws = wins(acct);
    const a = pick(ws, "5h"), b = pick(ws, "wk");
    return [a, b].filter(Boolean).reduce((x, y) => (!x || y.w.pct > x.w.pct ? y : x), null);
  }
  function runsOut(w) {
    const pe = Date.parse(w.projectedExhaustionAt), rs = Date.parse(w.resetsAt);
    return isFinite(pe) && isFinite(rs) && pe < rs && w.willLastToReset !== true ? { at: w.projectedExhaustionAt, before: rs - pe } : null;
  }
  const kindName = (x) => (x.kind === "5h" ? "5h" : "Weekly");
  function lasts(w) {
    return w.willLastToReset === true ? ". Lasts until reset" : "";
  }

  // --- logos ---
  function logoSrc(id) {
    return id === "codex" ? (dark.matches ? "logos/codex_dark.svg" : "logos/codex_light.svg") : LOGOS[id];
  }
  const colorOf = (id) => COLORS[id] || "var(--neutral)";
  function logo(id) {
    const src = logoSrc(id);
    return src ? h("img", { src, alt: "" }) : h("span", { class: "dot", style: `background:${colorOf(id)}` });
  }
  const ltile = (id, sm) => h("span", { class: "ltile" + (sm ? " sm" : "") }, logo(id));
  const accent = (el, id) => { el.style.setProperty("--accent", colorOf(id)); return el; };
  const planOf = (prov) => {
    const a = (prov.accounts || []).find((x) => x.active);
    const p = (a && (a.plan || a.planType)) || prov.plan;
    return typeof p === "string" && p ? p : null;
  };

  // --- tooltip + rings ---
  const tip = () => document.getElementById("tip");
  function showTip(x, anchor) {
    const w = x.w, t = tip();
    const name = x.kind === "5h" ? (x.model ? x.label : "5-hour session") : (x.model ? x.label : "Weekly, all models");
    const ro = runsOut(w);
    const line = w.resetsAt
      ? `Resets in ${countdown(w.resetsAt)} (${clock(w.resetsAt)})${ro ? `. Runs out around ${clock(ro.at)}` : lasts(w)}`
      : "No active window";
    t.replaceChildren(h("b", { class: level(w.pct), text: `${name}` }), h("b", { class: level(w.pct), text: `${round(w.pct)}%` }), h("div", { text: line }));
    t.style.display = "block";
    placeTip(anchor);
  }
  // left of the ring, vertically centred on it; flips below when there is no room on the left
  function placeTip(anchor) {
    const t = tip(), r = anchor.getBoundingClientRect();
    let x = r.left - t.offsetWidth - 8, y = r.top + r.height / 2 - t.offsetHeight / 2;
    if (x < 6) { x = Math.min(window.innerWidth - t.offsetWidth - 6, r.left); y = r.bottom + 6; }
    t.style.left = Math.max(6, x) + "px";
    t.style.top = Math.max(6, Math.min(y, window.innerHeight - t.offsetHeight - 6)) + "px";
  }

  // outer = weekly, inner = 5h; each arc is its own element with a wider invisible hit area
  // The centre must stay inside the inner ring's hole (inner radius minus half its stroke);
  // shrink the text until it fits, since the real width depends on the system font.
  const HOLE = 2 * (17.5 - 2) - 6; // px of usable width/height, with a little air
  function fitCentre(c) {
    if (!c.isConnected) return requestAnimationFrame(() => c.isConnected && fitCentre(c));
    c.style.transform = "";
    const r = document.createRange(); r.selectNodeContents(c);
    const b = r.getBoundingClientRect();
    const k = Math.min(1, HOLE / Math.max(b.width, 1), HOLE / Math.max(b.height, 1));
    if (k < 1) c.style.transform = `scale(${k.toFixed(3)})`;
  }
  function ring(outer, inner) {
    const S = 54, C = S / 2;
    const box = h("div", { class: "ring" });
    const s = svg("svg", { viewBox: `0 0 ${S} ${S}` });
    const centre = h("div", { class: "c" });
    const best = [outer, inner].filter(Boolean).reduce((a, b) => (!a || b.w.pct > a.w.pct ? b : a), null);
    const setCentre = (x, text) => {
      centre.className = "c " + (x ? level(x.w.pct) : "");
      centre.style.color = x ? "var(--level)" : "";
      centre.replaceChildren(
        h("span", { class: "n" }, x ? String(round(x.w.pct)) : "–", x ? h("span", { class: "u", text: "%" }) : null),
        h("small", { text: x ? text : "" }));
      fitCentre(centre);
    };
    const rest = () => { box.className = "ring"; setCentre(best, best ? (best.kind === "5h" ? "5h" : "wk") : ""); tip().style.display = "none"; };
    const arcs = [];
    for (const [x, r, cls, sw, cls2, word] of [[outer, 24.5, "out", 4, "ho", "weekly"], [inner, 17.5, "in", 4, "hi", "5h"]]) {
      s.append(svg("circle", { class: "t", cx: C, cy: C, r, "stroke-width": sw }));
      if (!x) continue;
      const used = Math.min(100, Math.max(0, x.w.pct));
      const visible = used > 0; // a round cap would paint a dot at 0%
      const arc = svg("circle", { class: `a ${cls} ${level(x.w.pct)}`, cx: C, cy: C, r, "stroke-width": sw, pathLength: 100,
        "stroke-dasharray": `${Math.max(used, 2)} 100`, visibility: visible ? "visible" : "hidden" });
      const hit = svg("circle", { class: "hit", cx: C, cy: C, r });
      hit.addEventListener("mouseenter", () => { box.className = "ring " + cls2; setCentre(x, word); showTip(x, box); });
      hit.addEventListener("mouseleave", rest);
      arcs.push(arc, hit);
    }
    s.append(...arcs);
    box.append(s, centre);
    setCentre(best, best ? (best.kind === "5h" ? "5h" : "wk") : "");
    return box;
  }

  function bar(pct, notch) {
    const kids = [h("i", { style: `width:${Math.min(100, Math.max(0, pct || 0))}%` })];
    if (typeof notch === "number") kids.push(h("b", { style: `left:${Math.min(100, Math.max(0, notch))}%` }));
    return h("div", { class: "bar " + level(pct || 0) }, ...kids);
  }
  const chipWarn = (text) => h("span", { class: "chip warn" }, h("i"), text);
  function runsOutChip(w) {
    const ro = runsOut(w);
    return ro ? chipWarn(`Runs out around ${clock(ro.at)}, ${dur(ro.before)} before the reset`) : null;
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
    return h("button", { class: "btn" + (armed ? " on" : ""), text: armed ? "Restart sessions? Confirm" : "Switch",
      onclick: (e) => { e.stopPropagation(); doSwitch(prov, acct); } });
  }

  function miniBars(acct) {
    const ws = wins(acct);
    const a = pick(ws, "5h"), b = pick(ws, "wk");
    const line = (name, x) => [h("span", { text: name }), x ? bar(x.w.pct) : h("div", { class: "bar" })];
    return h("div", { class: "mini2" }, ...line("5h", a), ...line("wk", b));
  }
  function otherRow(prov, acct) {
    const caps = prov.capabilities;
    const manage = caps.disable && tab === prov.id;
    const email = h("span", { class: "em", title: label(acct), text: label(acct) + (acct.disabled ? " (disabled)" : "") });
    if (!manage) return h("div", { class: "acct" }, email, miniBars(acct), switchButton(prov, acct));
    return h("div", { class: "acctwrap" }, h("div", { class: "acct" }, email), h("div", { class: "actions" }, miniBars(acct), h("span", { class: "spacer" }), switchButton(prov, acct),
      h("button", { class: "btn", text: acct.disabled ? "Enable" : "Disable",
        onclick: () => post({ action: "toggleDisabled", provider: prov.id, number: acct.number, disabled: !acct.disabled }) }),
      h("button", { class: "btn", text: "Remove", onclick: () => post({ action: "remove", provider: prov.id, number: acct.number }) })));
  }

  function autoOn(prov) { return !!(prov.autoSwitch && prov.autoSwitch.enabled); }
  function autoChip(text, onclick, title) {
    return h(onclick ? "button" : "span", { class: "chip", onclick, title }, h("i"), text);
  }
  function failureRow(prov) {
    const f = prov.autoSwitch && prov.autoSwitch.failure;
    if (!f) return null;
    return h("div", { class: "actions" }, h("span", { class: "err", text: f }),
      h("button", { class: "btn", text: "Restart", onclick: () => post({ action: "restartEngine", provider: prov.id }) }));
  }

  function overviewCard(prov) {
    const card = accent(h("section", { class: "pcard", onclick: () => { tab = prov.id; pending = null; draw(); } }), prov.id);
    const active = (prov.accounts || []).find((a) => a.active);
    const ws = active ? wins(active) : [];
    const outer = pick(ws, "wk"), inner = pick(ws, "5h");
    const bind = tight(active || { windows: [] });
    const plan = planOf(prov);
    let sub = prov.refreshing ? "refreshing" : "No active account";
    if (bind) sub = `${bind.model && bind.kind === "wk" ? bind.label : kindName(bind)}` + (bind.w.resetsAt
      ? ` resets in ${countdown(bind.w.resetsAt)} (${clock(bind.w.resetsAt)})${lasts(bind.w)}` : ", no active window");
    card.append(h("div", { class: "head" }, ltile(prov.id),
      h("div", { class: "meta" },
        h("div", { class: "name" }, h("span", { text: prov.label }), plan ? h("span", { class: "chip plan", text: plan }) : null),
        h("div", { class: "sub", text: sub })),
      outer || inner ? ring(outer, inner) : null));
    if (prov.error) card.append(h("div", { class: "err", text: prov.error }));
    const fail = failureRow(prov);
    if (fail) card.append(fail); // append(null) would print "null"
    if (active && active.note) card.append(h("div", { class: "note", text: active.note }));
    const chip = bind && runsOutChip(bind.w);
    if (chip) card.append(h("div", { class: "actions" }, chip));
    if (active) {
      const others = (prov.accounts || []).filter((x) => !x.active);
      if (!others.length) card.append(h("div", { class: "acct" }, h("span", { class: "em", text: label(active) })));
      for (const a of others) card.append(otherRow(prov, a));
    } else for (const a of prov.accounts || []) card.append(otherRow(prov, a));
    return card;
  }

  // --- provider detail ---
  function winBlock(x) {
    const w = x.w;
    const name = x.model ? x.label : kindName(x);
    const wrap = h("div", { class: "win " + level(w.pct || 0) + (x.model ? " model" : "") },
      h("div", { class: "l" },
        h("span", { class: "n" }, name, x.model ? h("small", { text: "per-model" }) : (x.kind === "wk" ? h("small", { text: "all models" }) : null)),
        h("span", { style: "color:var(--level);font-weight:600", text: `${round(w.pct || 0)}%` })),
      bar(w.pct, w.expectedPct),
      w.resetsAt ? h("div", { class: "muted", text: `Resets in ${countdown(w.resetsAt)} (${clock(w.resetsAt)})` }) : null);
    if (typeof w.expectedPct === "number" && !x.model) {
      const d = round(w.pct - w.expectedPct);
      const ro = runsOut(w);
      wrap.append(h("div", { class: "pace" + (w.aheadOfPace ? " ahead" : ""),
        text: (w.aheadOfPace ? `Pace: ahead +${d} pts` : `Pace: ${Math.abs(d)} pts under budget`)
          + (ro ? `. Runs out around ${clock(ro.at)}` : lasts(w)) }));
    }
    return wrap;
  }

  // Words from `ccswap switch --strategy` help.
  const STRATEGY = {
    "best": ["Best", "Moves to the account with the most room left."],
    "next-available": ["Next available", "Moves to the next account, skipping any at their limit."],
    "consume-first": ["Consume first", "Moves to the account whose weekly limit resets soonest, so no quota is lost at a reset."],
  };
  function switchCard(prov, strategies) {
    const saved = data.settings && data.settings.strategy;
    const pick = chosen[prov.id] || (strategies.includes(saved) ? saved : strategies[0]);
    const seg = h("div", { class: "seg full", role: "radiogroup", "aria-label": "Strategy" }, ...strategies.map((s) =>
      h("button", { class: s === pick ? "sel" : "", role: "radio", "aria-checked": String(s === pick), text: (STRATEGY[s] || [s])[0],
        onclick: () => { chosen[prov.id] = s; draw(); } })));
    return h("section", { class: "card" },
      h("div", { class: "actions" }, h("span", { class: "title", text: "Switch account" }), h("span", { class: "spacer" }),
        h("button", { class: "btn on", text: "Switch", onclick: () => post({ action: "rotate", provider: prov.id, strategy: pick }) })),
      seg, STRATEGY[pick] ? h("div", { class: "muted", text: STRATEGY[pick][1] }) : null);
  }

  function providerTab(prov) {
    const caps = prov.capabilities;
    const wrap = accent(h("div", {}), prov.id);
    const active = (prov.accounts || []).find((a) => a.active);
    const ws = active ? wins(active) : [];
    if (prov.error) wrap.append(h("div", { class: "card err", text: prov.error }));
    const fail = failureRow(prov);
    if (fail) wrap.append(h("div", { class: "card" }, fail));
    const plan = planOf(prov);
    wrap.append(h("div", { class: "row phead" }, h("span", { class: "title", text: prov.label }),
      plan ? h("span", { class: "chip plan", text: plan }) : null, h("span", { class: "spacer" }),
      caps.autoSwitch && prov.autoSwitch
        ? (autoOn(prov) ? autoChip("Auto on", () => post({ action: "toggleAuto", provider: prov.id, enabled: false }))
          : h("button", { class: "chip", text: "Auto off", onclick: () => post({ action: "toggleAuto", provider: prov.id, enabled: true }) }))
        : null));
    if (active) {
      const age = typeof active.usageAgeSeconds === "number" ? `checked ${dur(active.usageAgeSeconds * 1000) || "0m"} ago` : "";
      wrap.append(h("div", { class: "row" }, h("span", { class: "em", text: label(active) + (active.disabled ? " (disabled)" : "") }),
        h("span", { class: "chip active", text: "Active" }), h("span", { class: "muted", text: age })));
      if (active.note) wrap.append(h("div", { class: "note", text: active.note }));
      if (active.loginNote) wrap.append(h("div", { class: "muted", text: active.loginNote }));
      const hero = pick(ws, "5h") || pick(ws, "wk");
      if (hero) {
        const w = hero.w;
        const card = h("section", { class: "card hero big " + level(w.pct) });
        card.append(h("div", { class: "head" },
          h("div", { class: "meta" }, h("div", { class: "name", text: hero.model ? hero.label : hero.kind === "5h" ? "Session" : "Weekly" }),
            h("div", { class: "sub", text: hero.kind === "5h" ? "5-hour window" : "weekly window" })),
          h("div", { class: "pct" }, `${round(w.pct)}`, h("small", { text: "%" }))));
        card.append(h("div", { style: "margin:10px 0 6px" }, bar(w.pct, w.expectedPct)));
        if (w.resetsAt) card.append(h("div", { class: "muted", text: `Resets in ${countdown(w.resetsAt)} (${clock(w.resetsAt)})` }));
        const chip = runsOutChip(w);
        if (chip) card.append(h("div", { class: "actions" }, chip));
        wrap.append(card);
      }
      const rest = ws.filter((x) => x !== hero).sort((a, b) => (a.model - b.model) || (a.kind === b.kind ? 0 : a.kind === "wk" ? -1 : 1));
      if (rest.length) wrap.append(h("section", { class: "card" }, ...rest.map(winBlock)));
    }
    const strategies = caps.strategies || [];
    if (strategies.length && (prov.accounts || []).length > 1) wrap.append(switchCard(prov, strategies));

    const others = (prov.accounts || []).filter((a) => !a.active);
    const add = h("button", { class: "btn", text: "+ Add", "aria-expanded": String(addOpen), onclick: () => { addOpen = !addOpen; tokenOpen = false; draw(); } });
    const oc = h("section", { class: "card" }, h("div", { class: "actions" }, h("span", { class: "sect", text: others.length ? "Other accounts" : "Add an account" }), h("span", { class: "spacer" }), add));
    if (addOpen) {
      const choice = (title, sub, onclick) => h("button", { class: "choice", onclick }, h("span", { text: title }), h("span", { class: "muted", text: sub }));
      oc.append(h("div", { class: "menu" },
        choice("Current login", `Saves the account ${prov.label} is signed in to`, () => { addOpen = false; post({ action: "addLogin", provider: prov.id }); draw(); }),
        prov.addToken ? choice("Setup token or API key", "Same as ccswap add-token", () => { addOpen = false; tokenOpen = true; draw(); }) : null));
    }
    if (prov.addToken && tokenOpen) oc.append(tokenForm(prov));
    oc.append(...others.map((a) => otherRow(prov, a)));
    wrap.append(oc);

    // Tools for the active account, under its email so it's clear which account they act on.
    const tools = [];
    if (prov.id === "claude") tools.push(h("button", { class: "btn", text: "Refresh credentials", onclick: () => post({ action: "refreshCreds" }) }));
    if (active && caps.disable) {
      tools.push(h("button", { class: "btn", text: active.disabled ? "Enable" : "Disable",
        onclick: () => post({ action: "toggleDisabled", provider: prov.id, number: active.number, disabled: !active.disabled }) }));
      tools.push(h("button", { class: "btn bad", text: "Remove", onclick: () => post({ action: "remove", provider: prov.id, number: active.number }) }));
    }
    if (active && tools.length) {
      // Laid out like an Other accounts row, so the two cards read the same way.
      wrap.append(h("section", { class: "card" }, h("div", { class: "sect", text: "Active account" }),
        h("div", { class: "acctwrap" }, h("div", { class: "acct" }, h("span", { class: "em", title: label(active), text: label(active) + (active.disabled ? " (disabled)" : "") })),
          h("div", { class: "actions" }, h("span", { class: "spacer" }), ...tools))));
    }
    return wrap;
  }

  // Same as `ccswap add-token [TOKEN] [--email]`: the token is required, the email only labels it.
  function tokenForm(prov) {
    const token = h("input", { type: "password", autocomplete: "off", id: "tok", spellcheck: "false" });
    const email = h("input", { type: "email", autocomplete: "off", id: "tokmail", placeholder: "Detected from the token" });
    const add = h("button", { class: "btn on", text: "Add", disabled: true, onclick: () => {
      post({ action: "addToken", provider: prov.id, email: email.value.trim(), token: token.value.trim() });
      token.value = ""; tokenOpen = false; draw();
    } });
    token.addEventListener("input", () => { add.disabled = !token.value.trim(); });
    return h("div", { class: "tokform" },
      h("div", { class: "sect", text: "Add a setup token or API key" }),
      h("label", { for: "tok", text: "Setup token or API key" }), token,
      h("div", { class: "muted", text: "Get a setup token with claude setup-token in Terminal." }),
      h("label", { for: "tokmail", text: "Email (optional)" }), email,
      h("div", { class: "actions" }, h("span", { class: "spacer" }),
        h("button", { class: "btn", text: "Cancel", onclick: () => { tokenOpen = false; draw(); } }), add));
  }

  // --- settings ---
  function setRow(text, sub, control, stacked) {
    return h("div", { class: "set" + (stacked ? " col" : "") },
      h("div", { class: "tx" }, h("span", { text }), sub ? h("small", { text: sub }) : null), control);
  }
  function sw(on, onclick) {
    return h("button", { class: "sw", role: "switch", "aria-checked": on ? "true" : "false", onclick });
  }
  function seg(options, current, onpick, full) {
    return h("div", { class: "seg" + (full ? " full" : "") }, options.map(([v, text, id]) =>
      h("button", { class: v === current ? "sel" : "", onclick: () => onpick(v) }, id ? logo(id) : null, text)));
  }
  function selectRow(text, options, current, action) {
    const sel = h("select", {}, options.map(([v, l]) => h("option", { value: v, text: l, selected: v === current })));
    sel.addEventListener("change", () => post({ action, value: Number(sel.value) }));
    return setRow(text, null, sel);
  }
  function group(title, ...rows) {
    return h("div", {}, h("div", { class: "sect", text: title }), h("section", { class: "card" }, ...rows));
  }

  function settingsTab() {
    const s = data.settings;
    const wrap = h("div", {}, h("div", { class: "row phead" }, h("span", { class: "title", text: "Settings" })));
    wrap.append(group("Menu bar title",
      setRow("Show", "Auto follows the account closest to its limit",
        seg([["auto", "Auto"]].concat(data.providers.map((p) => [p.id, p.label.split(" ")[0], p.id])), data.title.setting,
          (v) => post({ action: "setTitleProvider", value: v }), true), true),
      setRow("Percentages", null, seg(TITLE_PCT.map((v) => [v, v === "both" ? "Both" : v === "off" ? "Off" : v]), s.titlePct,
        (v) => post({ action: "setTitlePct", value: v }))),
      setRow("Account name", null, sw(s.showAccountName, () => post({ action: "toggleName", value: !s.showAccountName }))),
      setRow("Per-model limits", "e.g. Fable", sw(s.titleScoped, () => post({ action: "toggleScoped", value: !s.titleScoped })))));
    const capable = data.providers.filter((x) => x.capabilities.autoSwitch && x.autoSwitch);
    wrap.append(group("Auto-switch",
      ...capable.map((p) => setRow(p.label, null, sw(p.autoSwitch.enabled,
        () => post({ action: "toggleAuto", provider: p.id, enabled: !p.autoSwitch.enabled })))),
      (() => {
        const out = h("span", { class: "muted", text: `${round(s.threshold)}%` });
        const rng = h("input", { type: "range", min: 1, max: 100, value: round(s.threshold) });
        rng.addEventListener("input", () => { out.textContent = `${rng.value}%`; });
        rng.addEventListener("change", () => post({ action: "setThreshold", value: Number(rng.value) }));
        return setRow("Switch at", null, h("div", { class: "row" }, rng, out));
      })()));
    const ci = s.claudeIntegrations || {};
    const names = { statusline: "Claude Code statusline", mod: "Claude Code mod" };
    const iv = REFRESH.includes(s.refreshInterval) ? REFRESH : REFRESH.concat(s.refreshInterval).sort((a, b) => a - b);
    wrap.append(group("General",
      selectRow("Refresh every", iv.map((v) => [v, `${v} s`]), s.refreshInterval, "setInterval"),
      setRow("Style", null, seg([["popover", "Popover"], ["classic", "Classic"]], s.style, (v) => post({ action: "setStyle", value: v }))),
      ...Object.keys(ci).map((key) => setRow(names[key] || `Claude ${key}`, null, sw(ci[key], () => post({ action: "toggleIntegration", key }))))));
    const lines = Array.isArray(data.history) ? data.history : [];
    wrap.append(group("Switch history", ...lines.map((l) => h("div", { class: "logline", text: l })),
      lines.length ? null : h("div", { class: "muted", text: "No switches logged yet" }),
      h("div", { class: "actions" }, h("button", { class: "btn", text: "Open log", onclick: () => post({ action: "openLog" }) }))));
    return wrap;
  }

  // --- chrome ---
  // The header and the provider strip stay put in every view, so switching is one click in the strip.
  function drawHead() {
    const head = document.getElementById("head");
    const any = data.providers.some(autoOn);
    head.replaceChildren(h("span", { class: "tile" }, h("img", { src: "mark.svg", alt: "" })), h("span", { class: "title", text: "ccswap" }),
      h("span", { class: "spacer" }),
      any ? autoChip(`Auto at ${round(data.settings.threshold)}%`, () => { tab = "settings"; draw(); }, data.settings.strategy ? `Strategy: ${data.settings.strategy}` : null) : null,
      h("button", { class: "ib", title: "Refresh", "aria-label": "Refresh", onclick: () => post({ action: "refresh" }) }, icon("refresh")));
  }

  function drawTabs() {
    const tabs = document.getElementById("tabs");
    const mk = (id, kids, extra, name) => h("button", { role: "tab", class: tab === id ? "sel" : "", title: name, "aria-label": name,
      onclick: () => { tab = id; pending = null; draw(); } }, h("span", { class: "lbl" }, ...kids), extra);
    tabs.replaceChildren(mk("overview", [h("span", { text: "All" })], h("div", { class: "mini" })),
      ...data.providers.map((p) => {
        const a = (p.accounts || []).find((x) => x.active);
        const t = a && tight(a);
        const m = h("div", { class: "mini " + (t ? level(t.w.pct) : "") });
        if (t) m.append(h("i", { style: `display:block;height:100%;border-radius:2px;background:var(--level);width:${Math.min(100, t.w.pct)}%` }));
        // logo only; the provider name is the tooltip and the accessible label
        return mk(p.id, [logo(p.id)], m, p.label);
      }));
  }

  function draw() {
    if (!data) return;
    tip().style.display = "none"; // the hovered ring is about to be replaced and won't get mouseleave
    const view = document.getElementById("view");
    const keep = view.scrollTop;
    const prov = data.providers.find((p) => p.id === tab);
    if (!prov && tab !== "overview" && tab !== "settings") tab = "overview";
    drawHead();
    drawTabs();
    view.replaceChildren();
    if (note && note.until > Date.now()) view.append(h("div", { class: "card " + (note.bad ? "err" : "note"), text: note.text }));
    if (tab === "overview" && data.health && data.health.attention && data.health.label) {
      view.append(h("div", { class: "card err", text: data.health.label }));
    }
    if (tab === "overview") for (const p of data.providers) view.append(overviewCard(p));
    else if (tab === "settings") view.append(settingsTab());
    else view.append(providerTab(prov));
    view.scrollTop = keep;
    const gear = document.getElementById("f-settings");
    gear.classList.toggle("sel", tab === "settings");
    gear.setAttribute("aria-pressed", String(tab === "settings"));
    const at = data.lastCheckedAt;
    document.getElementById("checked").textContent = at ? "Checked " + hhmm(new Date(at * 1000)) : "Not checked yet";
    dirty = false;
  }

  function editing() {
    const a = document.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "SELECT");
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
  for (const [id, name] of [["f-refresh", "refresh"], ["f-settings", "settings"], ["f-quit", "power"]]) document.getElementById(id).append(icon(name));
  document.getElementById("f-refresh").addEventListener("click", () => post({ action: "refresh" }));
  // The gear toggles: a second click goes back to All.
  document.getElementById("f-settings").addEventListener("click", () => { tab = tab === "settings" ? "overview" : "settings"; pending = null; draw(); });
  document.getElementById("f-quit").addEventListener("click", () => post({ action: "quit" }));
  dark.addEventListener("change", () => { if (!editing()) draw(); });
  setInterval(() => { if (!editing()) draw(); }, 30000); // countdowns
  post({ action: "ready" });
})();
