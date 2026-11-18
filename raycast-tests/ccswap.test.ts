import assert from "node:assert/strict";
import { test } from "node:test";
import {
  canBest,
  errorMessage,
  parseList,
  switchArgs,
  windowsOf,
} from "../raycast/src/ccswap.ts";

// Trimmed from real `ccswap list --json` output, with fake emails.
const LIST = JSON.stringify({
  claude: {
    schemaVersion: 1,
    activeAccountNumber: 1,
    accounts: [
      {
        number: 1,
        email: "a@example.com",
        organizationName: "Org A",
        active: true,
        usageStatus: "ok",
        usage: {
          fiveHour: { pct: 19, countdown: "1h 39m" },
          sevenDay: { pct: 40, countdown: "2d 2h" },
        },
      },
      {
        number: 2,
        email: "b@example.com",
        alias: "work",
        active: false,
        usageStatus: "token_expired",
        usage: null,
        lastGoodUsage: { fiveHour: { pct: 5 } },
      },
    ],
  },
  codex: { provider: "codex", activeAccountNumber: null, accounts: [] },
});

test("parseList reads both providers and falls back to last good usage", () => {
  const [claude, codex] = parseList(LIST);
  assert.equal(claude.label, "Claude Code");
  assert.equal(claude.accounts.length, 2);
  assert.equal(claude.accounts[0].usage?.sevenDay?.pct, 40);
  assert.equal(claude.accounts[1].usage?.fiveHour?.pct, 5);
  assert.deepEqual(codex.accounts, []);
  assert.deepEqual(
    windowsOf(claude.accounts[0]).map((w) => w.label),
    ["5h", "7d"],
  );
  assert.ok(canBest(claude));
});

test("parseList tolerates a missing codex section", () => {
  assert.deepEqual(
    parseList(JSON.stringify({ claude: { schemaVersion: 1, accounts: [] } }))[1]
      .accounts,
    [],
  );
});

test("parseList rejects an unknown schema and surfaces CLI errors", () => {
  assert.throws(
    () =>
      parseList(JSON.stringify({ claude: { schemaVersion: 2, accounts: [] } })),
    /schema 2/,
  );
  assert.throws(
    () =>
      parseList(
        JSON.stringify({ schemaVersion: 1, error: { message: "boom" } }),
      ),
    /boom/,
  );
});

test("errorMessage prefers the JSON envelope, then stderr", () => {
  assert.equal(errorMessage('{"error":{"message":"nope"}}', "x"), "nope");
  assert.equal(errorMessage("plain", " bad \n"), "bad");
  assert.equal(errorMessage("", ""), undefined);
});

test("switchArgs builds Claude and Codex commands", () => {
  assert.deepEqual(switchArgs("claude"), [
    "switch",
    "--strategy",
    "best",
    "--json",
  ]);
  assert.deepEqual(switchArgs("codex", 2), ["codex", "switch", "2", "--json"]);
  assert.deepEqual(switchArgs("agy", 3), ["agy", "switch", "3", "--json"]);
});

const PROVIDERS = JSON.stringify({
  providers: [
    {
      id: "claude",
      label: "Claude Code",
      schemaVersion: 1,
      capabilities: { strategies: ["best"] },
      accounts: [],
      error: null,
    },
    {
      id: "agy",
      label: "Antigravity",
      schemaVersion: 1,
      capabilities: { strategies: [], restartAfterSwitch: true },
      accounts: [
        {
          number: 1,
          email: "g@example.com",
          active: true,
          usage: null,
          windows: [
            { id: "b1/weekly", label: "Gemini Pro", pct: 12, countdown: "3d" },
          ],
        },
      ],
      error: null,
    },
    {
      id: "codex",
      label: "Codex",
      schemaVersion: 1,
      capabilities: { strategies: ["best"] },
      accounts: [],
      error: "codex broke",
    },
  ],
});

test("parseList prefers providers[], lists agy, shows a provider error without failing the rest", () => {
  const out = parseList(PROVIDERS);
  assert.deepEqual(
    out.map((p) => p.id),
    ["claude", "agy", "codex"],
  );
  assert.equal(out[1].label, "Antigravity");
  assert.equal(out[1].restartAfterSwitch, true);
  assert.deepEqual(
    windowsOf(out[1].accounts[0]).map((w) => [w.label, w.pct]),
    [["Gemini Pro", 12]],
  );
  assert.equal(out[2].error, "codex broke");
  assert.deepEqual(out[2].accounts, []);
});

test("best is offered only when the provider has the strategy", () => {
  const out = parseList(PROVIDERS);
  assert.deepEqual(out.map(canBest), [true, false, true]);
});
