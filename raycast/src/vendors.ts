import { run } from "./ccswap.ts";

// Kept free of @raycast/api imports so `npm test` runs it under plain Node.
// Every number shown comes from `ccswap usage --json`: ccswap's one usage model, shared with the
// CLI, TUI and menu bar. Nothing here parses vendor output.

export const USAGE_SCHEMA_VERSION = 1;

export type ToolId = "claude" | "codex" | "muse" | "kiro" | "opencode" | "agy";
export const TOOL_IDS: ToolId[] = ["claude", "codex", "muse", "kiro", "opencode", "agy"];
export const TOOL_TITLES: Record<ToolId, string> = {
  claude: "Claude Code",
  codex: "Codex",
  muse: "Muse Code",
  kiro: "Kiro CLI",
  opencode: "OpenCode",
  agy: "Antigravity",
};

export type UsageWindow = {
  key: string;
  label: string;
  unit: string;
  used: number | null;
  limit: number | null;
  remaining: number | null;
  usedPercent: number | null;
  resetsAt: number | null;
  resetsOn: string | null;
  scope: string;
  note: string;
};

export type Usage = {
  state: "available" | "unavailable" | "unsupported" | "unauthenticated" | "stale" | "error";
  reason: string;
  hint: string;
  plan: string | null;
  source: string;
  authoritative: boolean;
  attributedTo: string | null;
  observedAt: number | null;
  error: string | null;
  windows: UsageWindow[];
};

export type ToolAccount = {
  alias: string;
  kind: string;
  provider: string | null;
  identity: Record<string, string>;
  registered: boolean;
  // null means the tool cannot say; never rendered as "inactive".
  active: boolean | null;
  note: string;
  headline: string;
  usage: Usage;
};

export type ToolUsage = {
  tool: string;
  label: string;
  installed: boolean;
  version: string | null;
  headline: string;
  error: string | null;
  refreshedAt: number | null;
  usage: Usage | null;
  accounts: ToolAccount[];
};

type Envelope = { schemaVersion?: number; error?: { message: string }; tools?: ToolUsage[] };

export function parseUsage(stdout: string): ToolUsage[] {
  const data = JSON.parse(stdout) as Envelope;
  if (data.error) throw new Error(data.error.message);
  if (data.schemaVersion !== USAGE_SCHEMA_VERSION) {
    throw new Error(`ccswap usage schema ${data.schemaVersion} is not supported. Update this extension.`);
  }
  return (data.tools ?? []).map((tool) => ({
    ...tool,
    accounts: (tool.accounts ?? []).map((a) => ({
      ...a,
      active: typeof a.active === "boolean" ? a.active : null,
      registered: a.registered !== false,
    })),
  }));
}

export function usageArgs(tools: ToolId[] = [], refresh = false): string[] {
  return ["usage", ...tools, "--json", ...(refresh ? ["--refresh"] : [])];
}

export function listUsage(bin: string, tools: ToolId[] = [], refresh = false): Promise<ToolUsage[]> {
  // Several CLIs are asked in parallel by ccswap; allow for the slowest one.
  return run(bin, usageArgs(tools, refresh), 120_000).then(parseUsage);
}

// Switching from Raycast is offered only where it is non-interactive and needs no extra
// confirmation: Claude Code and Codex slots (ccswap's own switch), and OpenCode provider
// accounts (native `auth switch`). Muse replaces a stored credential and Kiro needs a terminal
// login, so those return a command to run instead.
export function switchArgs(tool: string, account: ToolAccount): string[] | null {
  if (account.active === true) return null;
  if ((tool === "claude" || tool === "codex") && account.identity.number) {
    return [...(tool === "codex" ? ["codex"] : []), "switch", account.identity.number, "--json"];
  }
  if (tool === "opencode" && account.provider && account.active === false) {
    const selector = account.identity.id ?? account.identity.account;
    return selector ? ["opencode", "switch", account.provider, selector, "--json"] : null;
  }
  return null;
}

export function terminalCommand(tool: string, account: ToolAccount): string {
  if (tool === "muse") return `ccswap muse switch ${account.alias} --yes`;
  if (tool === "kiro") {
    return account.kind === "api-key"
      ? `ccswap kiro run ${account.alias}`
      : `ccswap kiro switch ${account.alias} --login --yes`;
  }
  if (tool === "agy") return `ccswap agy switch ${account.identity.number ?? account.alias}`;
  return `ccswap ${tool} switch ${account.alias}`;
}

export function windowLine(w: UsageWindow): string {
  if (w.used !== null && w.limit !== null) {
    const pct = w.usedPercent !== null ? ` (${w.usedPercent}%)` : "";
    return `${w.label}: ${fmt(w.used)} of ${fmt(w.limit)} ${w.unit === "percent" ? "" : w.unit} used${pct}`.replace(
      "  ",
      " ",
    );
  }
  if (w.usedPercent !== null) return `${w.label}: ${w.usedPercent}% used`;
  return `${w.label}: not reported`;
}

export function resetLine(w: UsageWindow, now = Date.now() / 1000): string | null {
  if (w.resetsAt !== null) {
    const delta = w.resetsAt - now;
    if (delta <= 0) return "reset due";
    const h = Math.floor(delta / 3600);
    return h >= 24
      ? `resets in ${Math.floor(h / 24)}d ${h % 24}h`
      : `resets in ${h}h ${Math.floor((delta % 3600) / 60)}m`;
  }
  return w.resetsOn ? `resets on ${w.resetsOn}` : null;
}

function fmt(n: number): string {
  return Math.abs(n) >= 100 || Number.isInteger(n) ? Math.round(n).toLocaleString("en-US") : n.toFixed(2);
}

export function stateLabel(usage: Usage | null): string | null {
  if (!usage) return null;
  if (usage.state === "stale") return `Stale${usage.error ? `: ${usage.error}` : ""}`;
  if (usage.state === "available") return null;
  return usage.reason || usage.state;
}
