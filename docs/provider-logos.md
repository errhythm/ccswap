# Provider logos

The menu bar and its popover show each provider's logo next to its accounts, so you can tell at a glance whose numbers you're reading. The logos identify those products only. They remain the property of their owners, and ccswap isn't affiliated with or endorsed by Anthropic, OpenAI or Google.

The files live in `src/ccswap/menubar_web/logos/` and load from the package, never from the network. Each file is the company's own published artwork, unmodified. The popover is a web page and uses SVG where the company publishes one. The menu bar is drawn by macOS, whose SVG renderer flattens Antigravity's blurred gradient, so it uses the PNG from the same press kit. Two logos carry transparent padding, so both places draw them larger to match the others' visual weight, without changing the files:

| Provider | Display scale |
| --- | --- |
| Codex | 1.15 |
| Antigravity | 1.32 |

## Sources

Checked 2026-11-18.

| File | Provider | Source | Changes |
| --- | --- | --- | --- |
| `claude.svg` | Claude Code | [claude.ai/favicon.svg](https://claude.ai/favicon.svg), Anthropic's site icon. Its one fill, `#D97757`, is the primary accent in [Anthropic's brand guidelines](https://github.com/anthropics/skills/blob/8a1541c4a3ffa5a20a5a91de0dcf3f0bab1d1ef4/skills/brand-guidelines/SKILL.md). | None |
| `codex.png` | Codex | The official Codex app, `Contents/Resources/app.asar`, `webview/assets/codex-app-ga-logo-3e5209898ca3.png`. Copied from [CodexBar](https://github.com/steipete/CodexBar/blob/main/docs/spend-chart-brand-icons.md), which extracted it from app version `26.928.31416`, signing team `2DC432GLL2`. The hash below matches the one CodexBar pinned. | None |
| `antigravity.svg` | Antigravity (popover) | [Antigravity press assets](https://antigravity.google/press/), "Icon - Full Color", SVG: `antigravity.google/assets/image/brand/antigravity-icon__full-color.svg`. Identical to the copy in the press kit zip. | None |
| `antigravity.png` | Antigravity (menu bar) | The same press assets, "Icon - Full Color", PNG (540×540): `antigravity.google/assets/image/brand/antigravity-icon__full-color.png`. Identical to the copy in the press kit zip. | None |

| File | SHA-256 |
| --- | --- |
| `claude.svg` | `b150888bc7257af83e3b85d3c2be4294f88986026f8168f6c12fc1fde6697350` |
| `codex.png` | `8e82b26c98a10e45798ce48124515720657f7735fb8d0853b3f087eaa8a6b74e` |
| `antigravity.svg` | `d775965ea1f8060538c15f758a3e22487317211fcb6051333b5e023f35aa726f` |
| `antigravity.png` | `e0cd08ccd10cd8d08ccf0ba449823ee88495825c0841619618100d3ab089f51e` |

`tests/test_brand.py` checks every file in the folder against these hashes. Adding or replacing a file means updating its rows here, or the test fails.
