# assets/ — Bundled binary assets (no code)

| Path | Does what | Connects to |
| ---- | --------- | ----------- |
| `cronus_icon.png` | App/tray icon, Discord Rich Presence art, `.ico` source for the exe. | `desktop/console_icon.py`, `services/discord_presence.py`, `cronus_launcher.spec` (`icon=`), `release.yml` (PIL → `.ico`). |
| `fonts/Kanit-*.ttf`, `fonts/Inter-*.ttf` | Bundled UI fonts, subset to the same glyph coverage as `ui/fonts/*.woff2` (Qt cannot read woff2, so the TTF fallback is what native widgets load). | `ui/styles/*`, `ui/fonts/*.woff2`, `desktop/webview_window.py`. |
| `fonts/OFL.txt` | Font license (SIL OFL). | Shipped as-is. |

Build note: `cronus_launcher.spec` bundles `assets/` → `sys._MEIPASS/assets`
resolved by `infra/app_paths.resource_path()`. Never load these via plain
relative paths or the frozen exe will miss them.

### Summary

Bundled binary assets only (icon, fonts, font license): packed by `cronus_launcher.spec` into `sys._MEIPASS` and resolved via `infra/app_paths.resource_path()`.
