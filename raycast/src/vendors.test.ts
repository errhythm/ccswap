import assert from "node:assert/strict";
import test from "node:test";
import { canSwitchFromRaycast, parseVendorAccounts, parseVendors, switchHint, vendorSwitchArgs } from "./vendors.ts";

const acct = (over: object = {}) => ({
  alias: "anthropic/work",
  kind: "oauth",
  provider: "anthropic",
  identity: { account: "work", id: "c1" },
  registered: false,
  active: false,
  note: "",
  ...over,
});

test("parseVendors reads the documented contract", () => {
  const out = parseVendors(
    JSON.stringify({
      schemaVersion: 1,
      vendors: [{ tool: "muse", label: "Muse Code", installed: false, version: null, accounts: 0, error: null }],
    }),
  );
  assert.equal(out[0].tool, "muse");
  assert.equal(out[0].installed, false);
});

test("parseVendors rejects an unknown schema and surfaces ccswap errors", () => {
  assert.throws(() => parseVendors(JSON.stringify({ schemaVersion: 2, vendors: [] })), /not supported/);
  assert.throws(() => parseVendors(JSON.stringify({ error: { message: "boom" } })), /boom/);
});

test("unknown activity stays null instead of becoming inactive", () => {
  const [a] = parseVendorAccounts(JSON.stringify({ schemaVersion: 1, accounts: [acct({ active: null })] }));
  assert.equal(a.active, null);
});

test("only inactive, known OpenCode accounts are switchable from Raycast", () => {
  const a = parseVendorAccounts(JSON.stringify({ schemaVersion: 1, accounts: [acct()] }))[0];
  assert.equal(canSwitchFromRaycast("opencode", a), true);
  assert.equal(canSwitchFromRaycast("opencode", { ...a, active: true }), false);
  assert.equal(canSwitchFromRaycast("opencode", { ...a, active: null }), false);
  assert.equal(canSwitchFromRaycast("muse", a), false);
  assert.equal(canSwitchFromRaycast("kiro", a), false);
});

test("switch args pass provider and account as separate argv entries", () => {
  const a = parseVendorAccounts(JSON.stringify({ schemaVersion: 1, accounts: [acct()] }))[0];
  assert.deepEqual(vendorSwitchArgs("opencode", a), ["opencode", "switch", "anthropic", "c1", "--json"]);
  assert.throws(() => vendorSwitchArgs("kiro", a), /cannot be switched/);
});

test("hints point to the CLI for non-switchable vendors", () => {
  const a = parseVendorAccounts(JSON.stringify({ schemaVersion: 1, accounts: [acct({ alias: "w" })] }))[0];
  assert.match(switchHint("muse", a), /ccswap muse switch w/);
  assert.match(switchHint("kiro", a), /--login/);
});
