import {
  Action,
  ActionPanel,
  Color,
  getPreferenceValues,
  Icon,
  Keyboard,
  List,
  openExtensionPreferences,
  showToast,
  Toast,
} from "@raycast/api";
import { showFailureToast, useCachedPromise } from "@raycast/utils";
import {
  Account,
  AccountWindow,
  canBest,
  label,
  listAccounts,
  ProviderData,
  resolveBinary,
  run,
  switchArgs,
  windowsOf,
} from "./ccswap";

function windowTag(window: AccountWindow): List.Item.Accessory {
  const name = window.label;
  // null: the API reported the window without a utilization figure.
  if (window.pct === null) return { tag: `${name} ?`, tooltip: window.countdown && `Resets in ${window.countdown}` };
  const pct = Math.round(window.pct);
  const color = pct >= 90 ? Color.Red : pct >= 70 ? Color.Orange : undefined;
  return { tag: { value: `${name} ${pct}%`, color }, tooltip: window.countdown && `Resets in ${window.countdown}` };
}

function accessories(account: Account): List.Item.Accessory[] {
  return [
    ...(account.disabled ? [{ tag: "Disabled" }] : []),
    ...windowsOf(account).map(windowTag),
    ...(account.active ? [{ icon: { source: Icon.CheckCircle, tintColor: Color.Green }, tooltip: "Active" }] : []),
  ];
}

export default function Command() {
  const bin = resolveBinary(getPreferenceValues<Preferences>().ccswapPath || undefined);
  const { data, error, isLoading, revalidate } = useCachedPromise(listAccounts, [bin ?? ""], { execute: !!bin });

  async function switchTo(provider: ProviderData, number?: number) {
    const toast = await showToast({ style: Toast.Style.Animated, title: "Switching…" });
    try {
      await run(bin!, switchArgs(provider.id, number));
    } catch (error) {
      await showFailureToast(error, { title: "Switch failed" });
      return;
    }
    // The switch already happened, so a failed lookup only loses the account name.
    const active = (await listAccounts(bin!).catch(() => undefined))
      ?.find((p) => p.id === provider.id)
      ?.accounts.find((a) => a.active);
    revalidate();
    toast.style = Toast.Style.Success;
    toast.title = `${provider.label}: ${active ? label(active) : "switched"}`;
    toast.message = provider.restartAfterSwitch
      ? "Restart any open session to use it"
      : "Open sessions pick it up within about 30 seconds";
  }

  if (!bin) {
    return (
      <List>
        <List.EmptyView
          icon={Icon.Terminal}
          title="ccswap Not Found"
          description="Install it with `uv tool install ccswap`, or set its path in the extension preferences."
          actions={
            <ActionPanel>
              <Action title="Open Extension Preferences" icon={Icon.Gear} onAction={openExtensionPreferences} />
            </ActionPanel>
          }
        />
      </List>
    );
  }

  if (error && !data) {
    return (
      <List isLoading={isLoading}>
        <List.EmptyView
          icon={Icon.Warning}
          title="Couldn't Load Accounts"
          description={error.message}
          actions={
            <ActionPanel>
              <Action title="Retry" icon={Icon.ArrowClockwise} onAction={revalidate} />
            </ActionPanel>
          }
        />
      </List>
    );
  }

  const bestAction = (provider: ProviderData, shortcut?: Keyboard.Shortcut) => (
    <Action title="Switch to Best Account" icon={Icon.Stars} shortcut={shortcut} onAction={() => switchTo(provider)} />
  );

  return (
    <List isLoading={isLoading} searchBarPlaceholder="Search accounts…">
      {data && !data.some((p) => p.accounts.length) && (
        <List.EmptyView
          icon={Icon.PersonCircle}
          title="No Accounts Yet"
          description="Log in to a provider, then run `ccswap add` in a terminal to save the account."
          actions={
            <ActionPanel>
              <Action.CopyToClipboard title="Copy Command" content="ccswap add" />
              <Action title="Refresh" icon={Icon.ArrowClockwise} onAction={revalidate} />
            </ActionPanel>
          }
        />
      )}
      {(data ?? []).map((provider) => (
        <List.Section key={provider.id} title={provider.label}>
          {provider.error && (
            <List.Item
              icon={{ source: Icon.Warning, tintColor: Color.Red }}
              title="Couldn't load accounts"
              subtitle={provider.error}
              actions={
                <ActionPanel>
                  <Action.CopyToClipboard title="Copy Error" content={provider.error} />
                  <Action title="Refresh" icon={Icon.ArrowClockwise} onAction={revalidate} />
                </ActionPanel>
              }
            />
          )}
          {provider.accounts.map((account) => (
            <List.Item
              key={`${provider.id}-${account.number}`}
              icon={account.active ? Icon.PersonCircle : Icon.Person}
              title={label(account)}
              subtitle={account.alias ? account.email : account.organizationName}
              keywords={[account.email, String(account.number)]}
              accessories={accessories(account)}
              actions={
                <ActionPanel>
                  {/* Raycast hides shortcuts on the first two actions, so ⌘B sits third. */}
                  {account.active && canBest(provider) ? (
                    bestAction(provider)
                  ) : account.active ? null : (
                    <Action
                      title="Switch to Account"
                      icon={Icon.Switch}
                      onAction={() => switchTo(provider, account.number)}
                    />
                  )}
                  <Action.CopyToClipboard title="Copy Email" content={account.email} />
                  {!account.active && canBest(provider) && bestAction(provider, { modifiers: ["cmd"], key: "b" })}
                  <Action
                    title="Refresh"
                    icon={Icon.ArrowClockwise}
                    shortcut={Keyboard.Shortcut.Common.Refresh}
                    onAction={revalidate}
                  />
                </ActionPanel>
              }
            />
          ))}
        </List.Section>
      ))}
    </List>
  );
}
