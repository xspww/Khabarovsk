"""One-off retheme: Cronus blue-tinted dark -> Storm Launcher neutral charcoal.

Maps hardcoded colors in ui/styles/*.css and ui_dashboard.py.
Run once:  python ops/retheme_storm.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# old -> new (single-pass simultaneous replacement, case-insensitive on input)
MAP = {
    # surfaces / backgrounds
    "#0d0f18": "#0b0c10",
    "#0d0f19": "#0d0e12",
    "#0e1017": "#0d0e12",
    "#0e111d": "#0f1014",
    "#0e1728": "#12131a",
    "#10121a": "#0f1015",
    "#101320": "#101116",
    "#101322": "#111218",
    "#111622": "#131419",
    "#121420": "#131419",
    "#121524": "#14151a",
    "#131626": "#14151a",
    "#141726": "#15161b",
    "#141728": "#16171c",
    "#14172a": "#15161b",
    "#151828": "#16171c",
    "#161928": "#17181d",
    "#171828": "#18191e",
    "#171a2b": "#18191e",
    "#171b30": "#191a20",
    "#181b2e": "#191a20",
    "#181c30": "#1a1b21",
    "#181d33": "#1b1c22",
    "#191d30": "#1b1c22",
    "#1b1e32": "#1d1f25",
    "#1e2035": "#20222a",
    "#1e2238": "#212329",
    # borders
    "#1a1e2f": "#1d1f26",
    "#1c2033": "#1f2128",
    "#1f243b": "#22242b",
    "#20253b": "#232529",
    "#20253d": "#232529",
    "#20263f": "#232529",
    "#222842": "#25272e",
    "#252b40": "#282a31",
    "#252b44": "#282a31",
    "#252b47": "#282a31",
    "#282f4d": "#2b2d34",
    "#283050": "#2b2d34",
    "#2a3150": "#2d2f36",
    "#2b3252": "#2e3038",
    "#2b4a7a": "#34374a",
    "#2e3553": "#31333c",
    "#2f3657": "#32343d",
    "#2f3759": "#32343d",
    "#2f375c": "#32343d",
    "#312e52": "#34365c",
    "#3b4570": "#3e4152",
    "#464273": "#4a4c6e",
    # text (blue-gray -> neutral gray)
    "#f1f5f9": "#f2f3f5",
    "#e2e8f0": "#e4e6e9",
    "#dbe6ff": "#e3e5ee",
    "#d4def0": "#d6d8dd",
    "#cbd5e1": "#cdced4",
    "#c8d0e7": "#caccd2",
    "#c5cbdf": "#c7c9cf",
    "#a3aec9": "#a5a8b0",
    "#8b95c9": "#8d90a0",
    "#8b95af": "#8d9099",
    "#7e859b": "#7f838c",
    "#78829d": "#7a7e87",
    "#6b7794": "#6c7079",
    "#686f87": "#696d76",
    "#64748b": "#656972",
    "#64708c": "#656972",
    "#5e6987": "#5f626b",
    "#525d78": "#53565e",
    "#525b75": "#53565e",
    "#47516b": "#484b53",
    # rgba line tints
    "rgba(110,125,165,": "rgba(140,144,156,",
    "rgba(110, 125, 165,": "rgba(140, 144, 156,",
    "rgba(130,148,196,": "rgba(150,154,166,",
    "rgba(130, 148, 196,": "rgba(150, 154, 166,",
    "rgba(6,8,14,": "rgba(4,5,8,",
    "rgba(6, 8, 14,": "rgba(4, 5, 8,",
}

pattern = re.compile(
    "|".join(re.escape(key) for key in sorted(MAP, key=len, reverse=True)),
    re.IGNORECASE,
)


def sub(match: re.Match) -> str:
    return MAP[match.group(0).lower()]


TARGETS = sorted((ROOT / "ui" / "styles").glob("*.css")) + [ROOT / "ui_dashboard.py"]

for path in TARGETS:
    text = path.read_text(encoding="utf-8")
    new_text = pattern.sub(sub, text)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print(f"updated {path.relative_to(ROOT)}")
    else:
        print(f"no change {path.relative_to(ROOT)}")

# bump cache-buster versions
dash = ROOT / "ui" / "dashboard.css"
dash.write_text(dash.read_text(encoding="utf-8").replace("theme-toggle-compact", "storm-theme-1"), encoding="utf-8")
idx = ROOT / "ui" / "index.html"
idx.write_text(idx.read_text(encoding="utf-8").replace("dashboard.css?v=theme-toggle-compact", "dashboard.css?v=storm-theme-1"), encoding="utf-8")
print("bumped cache-buster to storm-theme-1")
