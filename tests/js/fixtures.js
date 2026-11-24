"use strict";
// Hand-written to match menubar_controller.build_payload. Fake emails only.
const soon = (min) => new Date(Date.now() + min * 60000).toISOString();
const win = (id, label, pct, durationS, extra) =>
  Object.assign({ id, label, pct, durationS, resetsAt: soon(durationS / 60 / 2) }, extra);

const acct = (number, email, active, windows, extra) =>
  Object.assign({ number, email, alias: null, active, disabled: false, windows, windowsSource: "usage",
    note: null, plan: null, loginNote: null }, extra);

const caps = (extra) => Object.assign({ switch: true, strategies: [], disable: false, autoSwitch: true,
  restartAfterSwitch: false, sessionMode: false }, extra);

function payload() {
  const std = (a, b) => [win("5h", "5h", a, 18000), win("7d", "Weekly", b, 604800)];
  return {
    schemaVersion: 1,
    lastCheckedAt: Date.now() / 1000,
    health: { label: "", attention: false },
    title: { setting: "auto", resolved: "claude", text: "12%" },
    settings: { style: "popover", titleProvider: "auto", titlePct: "both", showAccountName: false,
      titleScoped: false, refreshInterval: 60, threshold: 90, strategy: "consume-first",
      claudeIntegrations: { statusline: true, mod: false } },
    history: ["2026-01-01 a@example.com -> b@example.com"],
    providers: [
      { id: "claude", label: "Claude Code", schemaVersion: 1, error: null, refreshing: false, addToken: true,
        capabilities: caps({ strategies: ["best", "next-available", "consume-first"], disable: true }),
        autoSwitch: { enabled: true, running: true, failure: null }, activeAccountNumber: 1,
        accounts: [acct(1, "a@example.com", true, std(12, 30), { plan: "Max" }),
                   acct(2, "b@example.com", false, std(40, 50))] },
      { id: "codex", label: "Codex", schemaVersion: 1, error: null, refreshing: false, addToken: false,
        capabilities: caps({ strategies: ["best", "next-available"], restartAfterSwitch: true }),
        autoSwitch: { enabled: false, running: false, failure: null }, activeAccountNumber: 1,
        accounts: [acct(1, "c@example.com", true, std(5, 6)), acct(2, "d@example.com", false, std(7, 8))] },
      { id: "agy", label: "Antigravity", schemaVersion: 1, error: null, refreshing: false, addToken: false,
        capabilities: caps({ autoSwitch: false }),
        autoSwitch: null, activeAccountNumber: 1,
        accounts: [acct(1, "e@example.com", true, [])] },
    ],
  };
}

// Optional fields missing, plus a provider error: nothing may print "null"/"undefined".
function sparsePayload() {
  const p = payload();
  p.lastCheckedAt = null;
  p.history = [];
  p.health = null;
  const [claude, codex, agy] = p.providers;
  claude.accounts.forEach((a) => { a.plan = null; a.windows = []; a.usage = null; a.note = null; a.loginNote = null; });
  codex.error = "usage fetch failed";
  codex.accounts.forEach((a) => { delete a.plan; delete a.windows; });
  agy.accounts[0].alias = null;
  agy.accounts[0].email = null;
  return p;
}

module.exports = { payload, sparsePayload };
