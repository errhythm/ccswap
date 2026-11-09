import { Action, ActionPanel, Color, getPreferenceValues, Icon, List, showToast, Toast } from "@raycast/api";
import { showFailureToast, useCachedPromise } from "@raycast/utils";
import { resolveBinary, run } from "./ccswap";
import {
  listUsage,
  resetLine,
  stateLabel,
  switchArgs,
  terminalCommand,
  TOOL_TITLES,
  ToolAccount,
  ToolId,
  ToolUsage,
  Usage,
  windowLine,
} from "./vendors";

function usageMarkdown(title: string, usage: Usage | null): string {
  if (!usage) return `## ${title}\n\nNo reading yet.`;
  const lines = [`## ${title}`];
  if (usage.plan) lines.push(`**Plan:** ${usage.plan}`);
  for (const w of usage.windows) {
    const reset = resetLine(w);
    lines.push(`- ${windowLine(w)}${reset ? ` — ${reset}` : ""}${w.scope !== "account" ? ` _(${w.scope})_` : ""}`);
  }
  const state = stateLabel(usage);
  if (state) lines.push(`\n_${state}_`);
  if (usage.hint) lines.push(`\n${usage.hint}`);
  if (usage.observedAt) lines.push(`\nUpdated ${new Date(usage.observedAt * 1000).toLocaleString()} · ${usage.source}`);
  return lines.join("\n");
}

function accountIcon(account: ToolAccount) {
  if (account.active === true) return { source: Icon.CheckCircle, tintColor: Color.Green };
  if (account.active === null) return Icon.QuestionMarkCircle;
  return Icon.Person;
}

export function ToolSection({ bin, tool, revalidate }: { bin: string; tool: ToolUsage; revalidate: () => void }) {
  async function switchTo(account: ToolAccount, args: string[]) {
    const toast = await showToast({ style: Toast.Style.Animated, title: "Switching…" });
    try {
      await run(bin, args);
    } catch (error) {
      await showFailureToast(error, { title: "Switch failed" });
      return;
    }
    revalidate();
    toast.style = Toast.Style.Success;
    toast.title = `${tool.label}: ${account.alias}`;
    toast.message = "Restart running sessions to use it";
  }

  const subtitle = tool.installed ? `${tool.version ?? ""} · ${tool.headline}` : "not installed";
  return (
    <List.Section title={tool.label} subtitle={subtitle}>
      {tool.usage && (
        <List.Item
          key={`${tool.tool}-current`}
          icon={Icon.Gauge}
          title={tool.tool === "opencode" ? "Local usage (all providers)" : "Current credential"}
          subtitle={tool.headline}
          detail={<List.Item.Detail markdown={usageMarkdown(tool.label, tool.usage)} />}
          actions={
            <ActionPanel>
              <Action title="Refresh" icon={Icon.ArrowClockwise} onAction={revalidate} />
            </ActionPanel>
          }
        />
      )}
      {tool.accounts.map((account) => {
        const args = switchArgs(tool.tool, account);
        return (
          <List.Item
            key={`${tool.tool}-${account.provider ?? ""}-${account.alias}`}
            icon={accountIcon(account)}
            title={account.alias}
            subtitle={account.headline}
            accessories={[{ tag: account.provider ?? account.kind }]}
            detail={<List.Item.Detail markdown={usageMarkdown(account.alias, account.usage)} />}
            actions={
              <ActionPanel>
                {args ? (
                  <Action title="Switch to Account" icon={Icon.Switch} onAction={() => switchTo(account, args)} />
                ) : (
                  <Action.CopyToClipboard title="Copy Switch Command" content={terminalCommand(tool.tool, account)} />
                )}
                <Action title="Refresh" icon={Icon.ArrowClockwise} onAction={revalidate} />
              </ActionPanel>
            }
          />
        );
      })}
      {tool.installed && tool.accounts.length === 0 && !tool.usage && (
        <List.Item key={`${tool.tool}-empty`} title="No accounts registered" subtitle={`ccswap ${tool.tool} add`} />
      )}
    </List.Section>
  );
}

export function ToolsList({ tools }: { tools: ToolId[] }) {
  const bin = resolveBinary(getPreferenceValues<Preferences>().ccswapPath || undefined);
  const { data, error, isLoading, mutate } = useCachedPromise(listUsage, [bin ?? "", tools], { execute: !!bin });
  // "Refresh" asks every tool again (`ccswap usage --refresh`), not just the short-lived cache.
  const revalidate = () => {
    if (bin) void mutate(listUsage(bin, tools, true));
  };
  if (!bin) {
    return (
      <List>
        <List.EmptyView icon={Icon.Terminal} title="ccswap Not Found" description="Install ccswap or set its path." />
      </List>
    );
  }
  if (error && !data) {
    return (
      <List isLoading={isLoading}>
        <List.EmptyView icon={Icon.Warning} title="Couldn't Load Usage" description={error.message} />
      </List>
    );
  }
  const title = tools.length === 1 ? TOOL_TITLES[tools[0]] : "All Tools";
  return (
    <List isLoading={isLoading} isShowingDetail navigationTitle={title} searchBarPlaceholder="Search accounts…">
      {(data ?? []).map((tool) => (
        <ToolSection key={tool.tool} bin={bin} tool={tool} revalidate={revalidate} />
      ))}
    </List>
  );
}
