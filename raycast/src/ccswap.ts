import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Kept free of @raycast/api imports so raycast-tests/ccswap.test.ts runs it under plain Node.

export const SCHEMA_VERSION = 1;

export type Provider = string;

export type UsageWindow = { pct: number | null; countdown?: string };

export type AccountWindow = UsageWindow & { id?: string; label: string; scope?: object | null };

export type Account = {
  number: number;
  email: string;
  alias?: string;
  organizationName?: string;
  active: boolean;
  disabled?: boolean;
  usage: { fiveHour?: UsageWindow; sevenDay?: UsageWindow; weekly?: UsageWindow } | null;
  windows?: AccountWindow[];
};

export type ProviderData = {
  id: Provider;
  label: string;
  strategies: string[];
  restartAfterSwitch: boolean;
  accounts: Account[];
  error?: string;
};

type Row = Account & { lastGoodUsage?: Account["usage"] };
type Section = { schemaVersion?: number; accounts?: Row[] };
type Entry = Section & {
  id: string;
  label?: string;
  capabilities?: { strategies?: string[]; restartAfterSwitch?: boolean };
  error?: string | null;
};

// Older ccswap has no providers[]: these are what its two keys always meant.
const LEGACY: ProviderData[] = [
  { id: "claude", label: "Claude Code", strategies: ["best"], restartAfterSwitch: false, accounts: [] },
  { id: "codex", label: "Codex", strategies: ["best"], restartAfterSwitch: true, accounts: [] },
];

// Raycast does not load the login shell, so PATH lacks where uv, pipx and Homebrew install ccswap.
export function resolveBinary(preferred?: string): string | undefined {
  const candidates = preferred
    ? [preferred]
    : [join(homedir(), ".local/bin/ccswap"), "/opt/homebrew/bin/ccswap", "/usr/local/bin/ccswap"];
  return candidates.find((path) => existsSync(path));
}

function rows(section: Section): Account[] {
  if (section.schemaVersion !== undefined && section.schemaVersion !== SCHEMA_VERSION) {
    throw new Error(`ccswap JSON schema ${section.schemaVersion} is not supported. Update this extension.`);
  }
  // A failed refresh leaves usage null; the last good reading still beats a blank row.
  return (section.accounts ?? []).map(({ lastGoodUsage, ...a }) => ({ ...a, usage: a.usage ?? lastGoodUsage ?? null }));
}

export function parseList(stdout: string): ProviderData[] {
  const data = JSON.parse(stdout) as { error?: { message: string }; providers?: Entry[] } & Record<string, unknown>;
  if (data.error) throw new Error(data.error.message);
  if (data.providers) {
    return data.providers.map((e) => ({
      id: e.id,
      label: e.label ?? e.id,
      strategies: e.capabilities?.strategies ?? [],
      restartAfterSwitch: e.capabilities?.restartAfterSwitch ?? false,
      accounts: e.error ? [] : rows(e),
      error: e.error ?? undefined,
    }));
  }
  return LEGACY.map((p) => {
    const section = data[p.id] as Section | null | undefined;
    return { ...p, accounts: section ? rows(section) : [] };
  });
}

export function errorMessage(stdout: string, stderr: string): string | undefined {
  try {
    const message = (JSON.parse(stdout) as { error?: { message?: string } }).error?.message;
    if (message) return message;
  } catch {
    // not JSON, fall through to stderr
  }
  return stderr.trim() || undefined;
}

// `list --json` exits 1 when one provider fails but still prints the others.
export function partialList(stdout: string): boolean {
  try {
    return Array.isArray((JSON.parse(stdout) as { providers?: unknown }).providers);
  } catch {
    return false;
  }
}

export function run(bin: string, args: string[], usable?: (stdout: string) => boolean): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = execFile(bin, args, { timeout: 30_000 }, (error, stdout, stderr) => {
      if (error && !usable?.(stdout)) reject(new Error(errorMessage(stdout, stderr) ?? error.message));
      else resolve(stdout);
    });
    // A CLI prompt gets EOF and fails instead of hanging the command.
    child.stdin?.end();
  });
}

export function listAccounts(bin: string): Promise<ProviderData[]> {
  return run(bin, ["list", "--json"], partialList).then(parseList);
}

export function canBest(provider: Pick<ProviderData, "strategies">): boolean {
  return provider.strategies.includes("best");
}

// Claude keeps the bare form so the extension still works with older ccswap.
export function switchArgs(provider: Provider, number?: number): string[] {
  const target = number === undefined ? ["--strategy", "best"] : [String(number)];
  const base = provider === "claude" ? ["switch"] : [provider, "switch"];
  return [...base, ...target, "--json"];
}

// Windows from older ccswap come from the fixed usage keys.
export function windowsOf(account: Account): AccountWindow[] {
  if (account.windows) return account.windows;
  const u = account.usage;
  const old: [string, UsageWindow | undefined][] = [
    ["5h", u?.fiveHour],
    ["7d", u?.sevenDay ?? u?.weekly],
  ];
  return old.flatMap(([label, w]) => (w ? [{ label, ...w }] : []));
}

/** The row shows at most `max` windows: unscoped ones (5h, weekly) first, then the most used; the rest are returned for a tooltip. */
export function rowWindows(windows: AccountWindow[], max = 2): { shown: AccountWindow[]; rest: AccountWindow[] } {
  const unscoped = windows.filter((w) => !w.scope);
  const ranked = [...windows.filter((w) => w.scope)].sort((a, b) => (b.pct ?? -1) - (a.pct ?? -1));
  const ordered = [...unscoped, ...ranked];
  return { shown: ordered.slice(0, max), rest: ordered.slice(max) };
}

export function label(account: Account): string {
  return account.alias || account.email;
}
