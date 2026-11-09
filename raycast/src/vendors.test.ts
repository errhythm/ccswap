import assert from "node:assert/strict";
import test from "node:test";
import type { ToolAccount } from "./vendors.ts";
import { parseUsage, resetLine, stateLabel, switchArgs, terminalCommand, usageArgs, windowLine } from "./vendors.ts";

const account = (over: object = {}): ToolAccount =>
  ({
    alias: "work",
    kind: "oauth",
    provider: "anthropic",
    identity: { account: "work", id: "c1" },
    registered: true,
    active: false,
    note: "",
    headline: "usage unavailable",
    usage: {
      state: "unavailable",
      reason: "",
      hint: "",
      plan: null,
      source: "",
      authoritative: false,
      attributedTo: null,
      observedAt: null,
      error: null,
      windows: [],
    },
    ...over,
  }) as ToolAccount;

const doc = (tools: object[]) => JSON.stringify({ schemaVersion: 1, tools });

test("parseUsage reads the shared contract and keeps unknown activity null", () => {
  const [tool] = parseUsage(
    doc([
      {
        tool: "muse",
        label: "Muse Code",
        installed: true,
        version: "1.4.14",
        headline: "5h 0% · wk 1%",
        error: null,
        refreshedAt: 1,
        usage: null,
        accounts: [account({ active: null })],
      },
    ]),
  );
  assert.equal(tool.headline, "5h 0% · wk 1%");
  assert.equal(tool.accounts[0].active, null);
});

test("parseUsage rejects other schemas and surfaces ccswap errors", () => {
  assert.throws(() => parseUsage(JSON.stringify({ schemaVersion: 2, tools: [] })), /not supported/);
  assert.throws(() => parseUsage(JSON.stringify({ error: { message: "boom" } })), /boom/);
});

test("usageArgs builds argv entries, never a shell string", () => {
  assert.deepEqual(usageArgs(["kiro"], true), ["usage", "kiro", "--json", "--refresh"]);
  assert.deepEqual(usageArgs(), ["usage", "--json"]);
});

test("switch only where it is non-interactive", () => {
  assert.deepEqual(switchArgs("opencode", account()), ["opencode", "switch", "anthropic", "c1", "--json"]);
  assert.deepEqual(switchArgs("claude", account({ identity: { number: "2" } })), ["switch", "2", "--json"]);
  assert.deepEqual(switchArgs("codex", account({ identity: { number: "3" } })), ["codex", "switch", "3", "--json"]);
  assert.equal(switchArgs("opencode", account({ active: true })), null);
  assert.equal(switchArgs("opencode", account({ active: null })), null);
  assert.equal(switchArgs("muse", account()), null);
  assert.equal(switchArgs("kiro", account()), null);
});

test("terminal commands for Muse and Kiro", () => {
  assert.match(terminalCommand("muse", account()), /ccswap muse switch work --yes/);
  assert.match(terminalCommand("kiro", account({ kind: "login" })), /--login --yes/);
  assert.match(terminalCommand("kiro", account({ kind: "api-key" })), /ccswap kiro run work/);
});

test("window and reset lines never invent numbers", () => {
  const base = {
    key: "k",
    label: "Plan credits",
    unit: "credits",
    used: 1136,
    limit: 2000,
    remaining: 864,
    usedPercent: 56.8,
    resetsAt: null,
    resetsOn: "2026-12-01",
    scope: "account",
    note: "",
  };
  assert.equal(windowLine(base), "Plan credits: 1,136 of 2,000 credits used (56.8%)");
  assert.equal(resetLine(base), "resets on 2026-12-01");
  assert.equal(windowLine({ ...base, used: null, limit: null, usedPercent: null }), "Plan credits: not reported");
  assert.equal(resetLine({ ...base, resetsAt: 1000 + 3 * 3600 + 120, resetsOn: null }, 1000), "resets in 3h 2m");
});

test("stale and unavailable states are labelled, available is not", () => {
  const u = account().usage;
  assert.equal(stateLabel({ ...u, state: "stale", error: "timed out" } as never), "Stale: timed out");
  assert.equal(stateLabel({ ...u, state: "available" } as never), null);
});
