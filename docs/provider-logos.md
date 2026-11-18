# Provider logos

The menu bar and its popover show each provider's logo next to its accounts, so you can tell at a glance whose numbers you're reading. The logos identify those products only. They remain the property of their owners, and ccswap isn't affiliated with or endorsed by Anthropic, OpenAI or Google.

The files live in `src/ccswap/menubar_web/logos/` and load from the package, never from the network. Each file is the company's own published artwork, unmodified. Two of them carry transparent padding, so the menu bar and the popover draw them larger to match the others' visual weight, without changing the files:

| Provider | Display scale |
| --- | --- |
| Codex | 1.15 |
| Antigravity | 1.38 |

## Sources

Checked 2026-11-18.

| File | Provider | Source | Changes |
| --- | --- | --- | --- |
| `claude.svg` | Claude Code | [claude.ai/favicon.svg](https://claude.ai/favicon.svg), Anthropic's site icon. Its one fill, `#D97757`, is the primary accent in [Anthropic's brand guidelines](https://github.com/anthropics/skills/blob/8a1541c4a3ffa5a20a5a91de0dcf3f0bab1d1ef4/skills/brand-guidelines/SKILL.md). | None |
| `codex.png` | Codex | The official Codex app, `Contents/Resources/app.asar`, `webview/assets/codex-app-ga-logo-3e5209898ca3.png`. Copied from [CodexBar](https://github.com/steipete/CodexBar/blob/main/docs/spend-chart-brand-icons.md), which extracted it from app version `26.928.31416`, signing team `2DC432GLL2`. The hash below matches the one CodexBar pinned. | None |
| `antigravity.png` | Antigravity | [antigravity.google/assets/image/antigravity-logo.png](https://antigravity.google/assets/image/antigravity-logo.png), downloaded directly. The hash below matches the one CodexBar pinned. A PNG because macOS's SVG renderer flattens this logo's gradient. | None |

| File | SHA-256 |
| --- | --- |
| `claude.svg` | `b150888bc7257af83e3b85d3c2be4294f88986026f8168f6c12fc1fde6697350` |
| `codex.png` | `8e82b26c98a10e45798ce48124515720657f7735fb8d0853b3f087eaa8a6b74e` |
| `antigravity.png` | `193ba1805de11c23cd0c7a1df92aa0a886708e57350f6f7766100afe5befed73` |

`tests/test_brand.py` checks these hashes. Replacing a file means updating its row here, or the test fails.
