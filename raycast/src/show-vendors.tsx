import { Action, ActionPanel, Color, getPreferenceValues, Icon, List, showToast, Toast } from "@raycast/api";
import { showFailureToast, useCachedPromise } from "@raycast/utils";
import { resolveBinary, run } from "./ccswap";
import {
  canSwitchFromRaycast,
  listVendorAccounts,
  listVendors,
  switchHint,
  VendorAccount,
  vendorSwitchArgs,
  VendorSummary,
  VendorTool,
} from "./vendors";

function accountAccessories(account: VendorAccount): List.Item.Accessory[] {
  if (account.active === true) return [{ icon: { source: Icon.CheckCircle, tintColor: Color.Green }, tooltip: "Active" }];
  if (account.active === null) return [{ tag: "Unknown", tooltip: "This tool cannot confirm which account is active" }];
  return [];
}

function VendorAccounts({ bin, vendor }: { bin: string; vendor: VendorSummary }) {
  const { data, isLoading, revalidate } = useCachedPromise(listVendorAccounts, [bin, vendor.tool as VendorTool]);

  async function switchTo(account: VendorAccount) {
    const toast = await showToast({ style: Toast.Style.Animated, title: "Switching…" });
    try {
      await run(bin, vendorSwitchArgs(vendor.tool, account));
    } catch (error) {
      await showFailureToast(error, { title: "Switch failed" });
      return;
    }
    revalidate();
    toast.style = Toast.Style.Success;
    toast.title = `${vendor.label}: ${account.alias}`;
    toast.message = switchHint(vendor.tool, account);
  }

  return (
    <List isLoading={isLoading} navigationTitle={vendor.label}>
      <List.EmptyView title="No accounts" description={`Run \`ccswap ${vendor.tool} add\` in a terminal.`} />
      {(data ?? []).map((account) => (
        <List.Item
          key={`${account.provider ?? ""}/${account.alias}`}
          icon={account.active ? Icon.PersonCircle : Icon.Person}
          title={account.alias}
          subtitle={[account.provider, account.kind].filter(Boolean).join(" · ")}
          accessories={accountAccessories(account)}
          actions={
            <ActionPanel>
              {canSwitchFromRaycast(vendor.tool, account) ? (
                <Action title="Switch to Account" icon={Icon.Switch} onAction={() => switchTo(account)} />
              ) : (
                <Action.CopyToClipboard title="Copy Switch Command" content={switchHint(vendor.tool, account)} />
              )}
              <Action title="Refresh" icon={Icon.ArrowClockwise} onAction={revalidate} />
            </ActionPanel>
          }
        />
      ))}
    </List>
  );
}

export default function Command() {
  const bin = resolveBinary(getPreferenceValues<Preferences>().ccswapPath || undefined);
  const { data, isLoading, revalidate } = useCachedPromise(listVendors, [bin ?? ""], { execute: !!bin });

  if (!bin) {
    return (
      <List>
        <List.EmptyView icon={Icon.Terminal} title="ccswap Not Found" description="Install it with `uv tool install ccswap`." />
      </List>
    );
  }

  return (
    <List isLoading={isLoading} searchBarPlaceholder="Search vendors…">
      {(data ?? []).map((vendor) => (
        <List.Item
          key={vendor.tool}
          icon={vendor.installed ? Icon.CheckCircle : Icon.XMarkCircle}
          title={vendor.label}
          subtitle={vendor.installed ? `${vendor.version ?? "unknown version"} · ${vendor.accounts} registered` : "Not installed"}
          accessories={vendor.error ? [{ tag: { value: "Error", color: Color.Red }, tooltip: vendor.error }] : []}
          actions={
            <ActionPanel>
              {vendor.installed && (
                <Action.Push title="Show Accounts" icon={Icon.List} target={<VendorAccounts bin={bin} vendor={vendor} />} />
              )}
              <Action title="Refresh" icon={Icon.ArrowClockwise} onAction={revalidate} />
            </ActionPanel>
          }
        />
      ))}
    </List>
  );
}
