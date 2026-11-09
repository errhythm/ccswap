import { run } from "./ccswap.ts";

// Kept free of @raycast/api imports so `npm test` runs it under plain Node.

export const VENDOR_SCHEMA_VERSION = 1;

export type VendorTool = "muse" | "opencode" | "kiro";
export const VENDOR_TOOLS: VendorTool[] = ["muse", "opencode", "kiro"];

export type VendorSummary = {
  tool: VendorTool;
  label: string;
  installed: boolean;
  version: string | null;
  accounts: number;
  error: string | null;
};

export type VendorAccount = {
  alias: string;
  kind: string;
  provider: string | null;
  identity: Record<string, string>;
  registered: boolean;
  // null means the vendor cannot say; never rendered as "inactive".
  active: boolean | null;
  note: string;
};

function checkSchema(data: { schemaVersion?: number; error?: { message: string } }): void {
  if (data.error) throw new Error(data.error.message);
  if (data.schemaVersion !== VENDOR_SCHEMA_VERSION) {
    throw new Error(`ccswap vendor JSON schema ${data.schemaVersion} is not supported. Update this extension.`);
  }
}

export function parseVendors(stdout: string): VendorSummary[] {
  const data = JSON.parse(stdout) as { schemaVersion?: number; error?: { message: string }; vendors?: VendorSummary[] };
  checkSchema(data);
  return (data.vendors ?? []).map((v) => ({
    tool: v.tool,
    label: v.label,
    installed: !!v.installed,
    version: v.version ?? null,
    accounts: v.accounts ?? 0,
    error: v.error ?? null,
  }));
}

export function parseVendorAccounts(stdout: string): VendorAccount[] {
  const data = JSON.parse(stdout) as {
    schemaVersion?: number;
    error?: { message: string };
    accounts?: VendorAccount[];
  };
  checkSchema(data);
  return (data.accounts ?? []).map((a) => ({
    alias: a.alias,
    kind: a.kind,
    provider: a.provider ?? null,
    identity: a.identity ?? {},
    registered: a.registered !== false,
    active: typeof a.active === "boolean" ? a.active : null,
    note: a.note ?? "",
  }));
}

export function listVendors(bin: string): Promise<VendorSummary[]> {
  return run(bin, ["vendors", "--json"]).then(parseVendors);
}

export function listVendorAccounts(bin: string, tool: VendorTool): Promise<VendorAccount[]> {
  return run(bin, [tool, "list", "--json"]).then(parseVendorAccounts);
}

// Only OpenCode switches non-interactively and without a confirmation: Muse replaces the stored
// credential and Kiro needs a terminal login, so those stay on the command line (docs/vendor-compatibility.md).
export function canSwitchFromRaycast(tool: VendorTool, account: VendorAccount): boolean {
  return tool === "opencode" && !account.active && !!account.provider && account.active !== null;
}

export function vendorSwitchArgs(tool: VendorTool, account: VendorAccount): string[] {
  if (!canSwitchFromRaycast(tool, account)) throw new Error(`${tool} accounts cannot be switched from Raycast`);
  const selector = account.identity.id ?? account.identity.account;
  return [tool, "switch", account.provider as string, selector, "--json"];
}

export function switchHint(tool: VendorTool, account: VendorAccount): string {
  if (tool === "muse") return `Run: ccswap muse switch ${account.alias} --yes`;
  if (tool === "kiro") return `Run: ccswap kiro switch ${account.alias} --login`;
  return "Restart OpenCode sessions to use it";
}
