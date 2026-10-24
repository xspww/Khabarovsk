from __future__ import annotations

import math
from collections import deque
from typing import Any, NamedTuple

STRUCTURAL_ANALYSIS_SIZE = (160, 120)
MODAL_ANALYSIS_SIZE = (320, 240)


def _as_luma(screenshot: Any):
    try:
        if getattr(screenshot, "mode", "") == "L":
            return screenshot
        return screenshot.convert("L")
    except Exception:
        return screenshot


def _resize_luma(screenshot: Any, size: tuple[int, int]):
    image = _as_luma(screenshot)
    try:
        from PIL import Image

        resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR", 2)
        if getattr(image, "size", None) == size:
            return image
        return image.resize(size, resampling)
    except Exception:
        return image.resize(size)


def _scaled_min(total: int, ratio: float, floor: int = 1) -> int:
    return max(int(floor), int(round(max(1, int(total or 0)) * float(ratio))))


class _BandScan(NamedTuple):
    outer: range
    inner: range
    low: int
    high: int
    threshold: float
    inclusive: bool
    rows: bool


class _ComponentFilter(NamedTuple):
    min_width: int
    min_height: int
    min_fill: float
    x_lo: float
    x_hi: float
    y_lo: float
    y_hi: float


class _ModalBody(NamedTuple):
    top: int
    bottom: int
    density: float
    rows: int
    left: int
    right: int
    cols: int


class _ModalShapeInput(NamedTuple):
    width_frac: float
    height_frac: float
    body_density: float
    col_density: float
    body_rows: int
    modal_cols: int
    min_body_rows: int
    min_modal_cols: int


class _ButtonRegion(NamedTuple):
    left: int
    right: int
    top: int
    bottom: int


class _PanelRegion(NamedTuple):
    left: int
    right: int
    top: int
    bottom: int
    height: int


class _PanelShapeInput(NamedTuple):
    body_rows: int
    panel_width: int
    body_density: float
    col_density: float
    min_body_rows: int
    min_panel_width: int


class _VisualFlags(NamedTuple):
    overlay_score: float
    modal_score: float
    button_score: float
    structural_score: float
    small_panel_score: float
    captcha_score: float
    structural_separator: float
    structural_button: bool
    modal_shape: bool
    separator: bool
    button: bool
    overlay_confirmed: bool
    modal_button_confirmed: bool
    small_panel_confirmed: bool
    captcha_confirmed: bool
    structural_button_confirmed: bool


def _mask_rect(pixels: Any, x_range: range, y_range: range, low: int, high: int) -> list[list[bool]]:
    mask: list[list[bool]] = []
    for y in y_range:
        row: list[bool] = []
        for x in x_range:
            row.append(low <= int(pixels[x, y]) <= high)
        mask.append(row)
    return mask


def _band_spans(pixels: Any, scan: _BandScan) -> list[tuple[int, int, float, int]]:
    spans: list[tuple[int, int, float, int]] = []
    start: int | None = None
    values: list[float] = []
    inner_count = max(1, len(scan.inner))
    for index in scan.outer:
        if scan.rows:
            hits = sum(1 for other in scan.inner if scan.low <= int(pixels[other, index]) <= scan.high)
        else:
            hits = sum(1 for other in scan.inner if scan.low <= int(pixels[index, other]) <= scan.high)
        ratio = hits / float(inner_count)
        passed = ratio >= scan.threshold if scan.inclusive else ratio > scan.threshold
        if passed:
            if start is None:
                start = index
                values = []
            values.append(ratio)
        elif start is not None:
            spans.append((start, index - 1, sum(values) / float(max(1, len(values))), len(values)))
            start = None
    if start is not None:
        spans.append((start, scan.outer[-1], sum(values) / float(max(1, len(values))), len(values)))
    return spans


def _best_span(spans: list[tuple[int, int, float, int]]) -> tuple[int, int, float, int] | None:
    return max(spans, key=lambda item: item[2] * item[3], default=None)


def _component_candidates(
    components: list[dict[str, int]],
    width: int,
    height: int,
    spec: _ComponentFilter,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in components:
        item_w = int(item["width"])
        item_h = int(item["height"])
        area = int(item["area"])
        center_x = int(item["x"]) + item_w / 2.0
        center_y = int(item["y"]) + item_h / 2.0
        if item_w < spec.min_width or item_h < spec.min_height:
            continue
        if not (width * spec.x_lo <= center_x <= width * spec.x_hi and height * spec.y_lo <= center_y <= height * spec.y_hi):
            continue
        fill = area / float(max(1, item_w * item_h))
        if fill < spec.min_fill:
            continue
        candidates.append({**item, "fill": round(fill, 3)})
    return candidates


def _score_of(result: dict[str, Any]) -> float:
    return float(result.get("score") or 0.0)


def _neighbors(cx: int, cy: int, width: int, height: int):
    """Yield the in-bounds 4-neighbour coordinates of one mask cell."""
    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
        if 0 <= nx < width and 0 <= ny < height:
            yield nx, ny


def _grow_component(
    mask: list[list[bool]], seen: list[list[bool]], start_x: int, start_y: int
) -> dict[str, int]:
    """Flood-fill one connected component and return its bounding box."""
    height = len(mask)
    width = len(mask[0])
    q = deque([(start_x, start_y)])
    seen[start_y][start_x] = True
    min_x = max_x = start_x
    min_y = max_y = start_y
    area = 0
    while q:
        cx, cy = q.popleft()
        area += 1
        min_x = min(min_x, cx)
        max_x = max(max_x, cx)
        min_y = min(min_y, cy)
        max_y = max(max_y, cy)
        for nx, ny in _neighbors(cx, cy, width, height):
            if seen[ny][nx] or not mask[ny][nx]:
                continue
            seen[ny][nx] = True
            q.append((nx, ny))
    return {
        "x": min_x,
        "y": min_y,
        "width": max_x - min_x + 1,
        "height": max_y - min_y + 1,
        "area": area,
    }


def _binary_components(mask: list[list[bool]]) -> list[dict[str, int]]:
    if not mask or not mask[0]:
        return []
    height = len(mask)
    width = len(mask[0])
    seen = [[False] * width for _ in range(height)]
    components: list[dict[str, int]] = []
    for y in range(height):
        for x in range(width):
            if seen[y][x] or not mask[y][x]:
                continue
            components.append(_grow_component(mask, seen, x, y))
    components.sort(key=lambda item: int(item["area"]), reverse=True)
    return components


def _overlay_scan(pixels: Any, width: int, height: int) -> tuple[list[int], int, int]:
    values: list[int] = []
    edge_total = 0
    edge_count = 0
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            if width * 0.24 <= x <= width * 0.76 and height * 0.20 <= y <= height * 0.80:
                continue
            value = int(pixels[x, y])
            values.append(value)
            if x + 2 < width:
                edge_total += abs(value - int(pixels[x + 2, y]))
                edge_count += 1
            if y + 2 < height:
                edge_total += abs(value - int(pixels[x, y + 2]))
                edge_count += 1
    return values, edge_total, edge_count


def _overlay_breakdown(
    dim_ratio: float,
    dark_ratio: float,
    mid_ratio: float,
    stddev: float,
    edge: float,
) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    if dim_ratio >= 0.52:
        breakdown["dim_background"] = 0.18
    if dark_ratio >= 0.25:
        breakdown["dark_overlay"] = 0.18
    if mid_ratio >= 0.45:
        breakdown["muted_luminance"] = 0.12
    if stddev <= 58.0:
        breakdown["low_contrast"] = 0.16
    if edge <= 18.0:
        breakdown["blurred_background"] = 0.12
    return breakdown


def _overlay_features(screenshot: Any) -> dict[str, Any]:
    try:
        small = _resize_luma(screenshot, STRUCTURAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        values, edge_total, edge_count = _overlay_scan(pixels, width, height)
        if not values:
            return {"matched": False, "score": 0.0, "reason": "no_background"}

        mean = sum(values) / float(len(values))
        variance = sum((value - mean) ** 2 for value in values) / float(len(values))
        stddev = math.sqrt(variance)
        edge = edge_total / float(max(1, edge_count))
        dark_ratio = sum(1 for value in values if value <= 125) / float(len(values))
        mid_ratio = sum(1 for value in values if 55 <= value <= 175) / float(len(values))
        dim_ratio = sum(1 for value in values if value <= 165) / float(len(values))

        breakdown = _overlay_breakdown(dim_ratio, dark_ratio, mid_ratio, stddev, edge)
        score = round(sum(breakdown.values()), 3)
        return {
            "matched": score >= 0.28,
            "score": score,
            "breakdown": breakdown,
            "mean": round(mean, 2),
            "stddev": round(stddev, 2),
            "edge": round(edge, 2),
            "dark_ratio": round(dark_ratio, 3),
            "mid_ratio": round(mid_ratio, 3),
            "dim_ratio": round(dim_ratio, 3),
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "reason": f"overlay:{exc}"}


def _separator_scan(pixels: Any, width: int, height: int) -> tuple[bool, float]:
    separator = False
    separator_strength = 0.0
    for y in range(int(height * 0.18), int(height * 0.50)):
        row_hits = 0
        for x in range(int(width * 0.14), int(width * 0.86)):
            value = int(pixels[x, y])
            if 135 <= value <= 235:
                row_hits += 1
        strength = row_hits / float(max(1, int(width * 0.72)))
        separator_strength = max(separator_strength, strength)
        if strength >= 0.14:
            separator = True
            break
    return separator, separator_strength


def _structural_breakdown(
    modal: dict[str, Any],
    button: dict[str, Any],
    separator: bool,
) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    if modal:
        breakdown["modal"] = 0.30
    if button:
        breakdown["disconnect_button_shape"] = 0.34
    if separator:
        breakdown["separator_line"] = 0.20
    if modal and button:
        modal_bottom = int(modal["y"]) + int(modal["height"])
        button_y = int(button["y"])
        if int(modal["y"]) < button_y < modal_bottom:
            breakdown["button_inside_modal"] = 0.18
    return breakdown


def _structural_popup_features(screenshot: Any) -> dict[str, Any]:
    try:
        small = _resize_luma(screenshot, STRUCTURAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        min_modal_w = _scaled_min(width, 0.175, 12)
        min_modal_h = _scaled_min(height, 0.133, 8)
        min_button_w = _scaled_min(width, 0.088, 8)
        min_button_h = _scaled_min(height, 0.033, 3)

        dark_mask = _mask_rect(pixels, range(width), range(height), 35, 100)
        bright_mask = _mask_rect(pixels, range(width), range(height), 215, 255)

        modal_candidates = _component_candidates(
            _binary_components(dark_mask),
            width,
            height,
            _ComponentFilter(min_modal_w, min_modal_h, 0.52, 0.18, 0.82, 0.18, 0.82),
        )
        modal = modal_candidates[0] if modal_candidates else {}

        button_candidates = _component_candidates(
            _binary_components(bright_mask),
            width,
            height,
            _ComponentFilter(min_button_w, min_button_h, 0.42, 0.10, 0.90, 0.45, 0.95),
        )
        button = button_candidates[0] if button_candidates else {}

        separator, separator_strength = _separator_scan(pixels, width, height)
        breakdown = _structural_breakdown(modal, button, separator)
        score = round(sum(breakdown.values()), 3)
        return {
            "matched": score >= 0.66,
            "score": score,
            "strength": "weak" if score >= 0.66 else "none",
            "source": "structural",
            "breakdown": breakdown,
            "modal": modal,
            "button": button,
            "separator_strength": round(separator_strength, 3),
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "reason": f"structural:{exc}"}


def _center_separator(pixels: Any, height: int, y_min: int, body: _ModalBody) -> float:
    separator_strength = 0.0
    width = body.right - body.left + 1
    for y in range(max(y_min, body.top - int(height * 0.18)), min(height, body.top + int((body.bottom - body.top + 1) * 0.32))):
        hits = sum(1 for x in range(body.left, body.right + 1) if 135 <= int(pixels[x, y]) <= 235)
        separator_strength = max(separator_strength, hits / float(max(1, width)))
    return separator_strength


def _center_buttons(pixels: Any, height: int, body: _ModalBody) -> tuple[int, float]:
    button_rows = 0
    button_strength = 0.0
    width = body.right - body.left + 1
    search_top = int(body.top + (body.bottom - body.top + 1) * 0.54)
    search_bottom = min(height, body.bottom + int(height * 0.10))
    for y in range(search_top, search_bottom):
        hits = sum(1 for x in range(body.left, body.right + 1) if int(pixels[x, y]) >= 205)
        ratio = hits / float(max(1, width))
        button_strength = max(button_strength, ratio)
        if ratio >= 0.14:
            button_rows += 1
    return button_rows, button_strength


def _centered_ok(width: int, height: int, center_x: float, center_y: float) -> bool:
    return bool(width * 0.25 <= center_x <= width * 0.75 and height * 0.24 <= center_y <= height * 0.76)


def _modal_shape_ok(values: _ModalShapeInput) -> bool:
    return bool(
        0.16 <= values.width_frac <= 0.78
        and 0.10 <= values.height_frac <= 0.54
        and values.body_density >= 0.34
        and values.col_density >= 0.58
        and values.body_rows >= values.min_body_rows
        and values.modal_cols >= values.min_modal_cols
    )


def _center_breakdown(
    centered: bool,
    modal_shape: bool,
    separator_matched: bool,
    button_matched: bool,
) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    if centered:
        breakdown["centered_modal"] = 0.20
    if modal_shape:
        breakdown["modal_body"] = 0.36
    if separator_matched:
        breakdown["title_separator"] = 0.18
    if button_matched:
        breakdown["disconnect_button_bar"] = 0.34
    return breakdown


def _center_modal_features(screenshot: Any) -> dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        min_body_rows = _scaled_min(height, 0.075, 12)
        min_modal_cols = _scaled_min(width, 0.141, 24)
        min_button_rows = _scaled_min(height, 0.013, 2)
        x_min, x_max = int(width * 0.08), int(width * 0.92)
        y_min, y_max = int(height * 0.10), int(height * 0.90)

        row_spans = _band_spans(
            pixels,
            _BandScan(range(y_min, y_max), range(x_min, x_max), 35, 105, 0.20, False, True),
        )
        body_span = _best_span(row_spans)
        if not body_span:
            return {"matched": False, "score": 0.0, "reason": "no_center_body"}
        body_top, body_bottom, body_density, body_rows = body_span

        col_spans = _band_spans(
            pixels,
            _BandScan(range(x_min, x_max), range(body_top, body_bottom + 1), 35, 105, 0.32, False, False),
        )
        col = _best_span(col_spans)
        if not col:
            return {"matched": False, "score": 0.0, "reason": "no_center_columns"}
        modal_left, modal_right, col_density, modal_cols = col
        modal_width = modal_right - modal_left + 1
        body_height = body_bottom - body_top + 1
        modal_center_x = (modal_left + modal_right) / 2.0
        modal_center_y = (body_top + body_bottom) / 2.0
        body = _ModalBody(body_top, body_bottom, body_density, body_rows, modal_left, modal_right, modal_cols)

        separator_strength = _center_separator(pixels, height, y_min, body)
        button_rows, button_strength = _center_buttons(pixels, height, body)

        width_frac = modal_width / float(width)
        height_frac = body_height / float(height)
        centered = _centered_ok(width, height, modal_center_x, modal_center_y)
        modal_shape = _modal_shape_ok(
            _ModalShapeInput(
                width_frac,
                height_frac,
                body_density,
                col_density,
                body_rows,
                modal_cols,
                min_body_rows,
                min_modal_cols,
            )
        )
        separator_matched = separator_strength >= 0.13
        button_matched = button_rows >= min_button_rows and button_strength >= 0.18
        controls = separator_matched and button_matched

        breakdown = _center_breakdown(centered, modal_shape, separator_matched, button_matched)

        score = round(sum(breakdown.values()), 3)
        matched = bool(centered and modal_shape and controls)
        return {
            "matched": matched,
            "score": score,
            "strength": "strong" if matched else ("weak" if centered and modal_shape else "none"),
            "source": "center_modal",
            "breakdown": breakdown,
            "rect": {
                "x": modal_left,
                "y": body_top,
                "width": modal_width,
                "height": body_height,
                "body_density": round(body_density, 3),
                "col_density": round(col_density, 3),
                "width_frac": round(width_frac, 3),
                "height_frac": round(height_frac, 3),
            },
            "centered": centered,
            "modal_shape": modal_shape,
            "separator_matched": separator_matched,
            "button_matched": button_matched,
            "controls": controls,
            "separator_strength": round(separator_strength, 3),
            "button_rows": button_rows,
            "button_strength": round(button_strength, 3),
            "analysis_size": f"{width}x{height}",
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "reason": f"center_modal:{exc}"}


def _button_search_rect(
    width: int,
    height: int,
    modal_rect: dict[str, Any] | None,
) -> tuple[int, int, int, int] | None:
    rect = dict(modal_rect or {})
    if rect:
        left = max(0, int(rect.get("x") or 0))
        top = max(0, int(rect.get("y") or 0))
        right = min(width, left + max(1, int(rect.get("width") or 0)))
        bottom = min(height, top + max(1, int(rect.get("height") or 0)))
    else:
        left, top, right, bottom = int(width * 0.12), int(height * 0.22), int(width * 0.88), int(height * 0.88)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bright_button_components(
    pixels: Any,
    region: _ButtonRegion,
    min_width: int,
    min_height: int,
) -> list[dict[str, Any]]:
    mask = _mask_rect(pixels, range(region.left, region.right), range(region.top, region.bottom), 205, 255)
    components: list[dict[str, Any]] = []
    for item in _binary_components(mask):
        item_w = int(item["width"])
        item_h = int(item["height"])
        area = int(item["area"])
        fill = area / float(max(1, item_w * item_h))
        if item_w < min_width or item_h < min_height:
            continue
        if fill < 0.36:
            continue
        components.append({
            "x": region.left + int(item["x"]),
            "y": region.top + int(item["y"]),
            "width": item_w,
            "height": item_h,
            "area": area,
            "fill": round(fill, 3),
        })
    return components


def _button_row_stats(pixels: Any, region: _ButtonRegion) -> tuple[int, float]:
    row_strength = 0.0
    row_hits = 0
    for y in range(region.top, region.bottom):
        hits = sum(1 for x in range(region.left, region.right) if int(pixels[x, y]) >= 205)
        ratio = hits / float(max(1, region.right - region.left))
        row_strength = max(row_strength, ratio)
        if ratio >= 0.14:
            row_hits += 1
    return row_hits, row_strength


def _button_pattern(count: int, row_hits: int, min_button_rows: int) -> tuple[str, float]:
    if count >= 2:
        return "double", 0.62
    if count == 1:
        return "single", 0.48
    if row_hits >= min_button_rows:
        return "bar", 0.38
    return "none", 0.0


def _button_layout_features(screenshot: Any, modal_rect: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        min_button_w = _scaled_min(width, 0.056, 10)
        min_button_h = _scaled_min(height, 0.017, 3)
        min_button_rows = _scaled_min(height, 0.013, 2)
        rect = _button_search_rect(width, height, modal_rect)
        if rect is None:
            return {"matched": False, "score": 0.0, "pattern": "none"}
        left, top, right, bottom = rect
        rect_height = max(1, bottom - top)
        search_top = int(top + rect_height * 0.52)
        search_bottom = min(height, int(bottom + rect_height * 0.45))
        region = _ButtonRegion(left=left, right=right, top=search_top, bottom=search_bottom)

        components = _bright_button_components(pixels, region, min_button_w, min_button_h)
        row_hits, row_strength = _button_row_stats(pixels, region)

        count = len(components)
        pattern, score = _button_pattern(count, row_hits, min_button_rows)
        matched = pattern != "none"
        return {
            "matched": matched,
            "score": score,
            "pattern": pattern,
            "components": components[:3],
            "component_count": count,
            "row_hits": row_hits,
            "row_strength": round(row_strength, 3),
            "analysis_size": f"{width}x{height}",
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "pattern": "none", "reason": f"button:{exc}"}


def _panel_text_rows(pixels: Any, region: _PanelRegion) -> int:
    text_rows = 0
    scan_top = region.top + _scaled_min(region.height, 0.05, 8)
    scan_bottom = region.bottom - _scaled_min(region.height, 0.03, 4)
    panel_width = region.right - region.left + 1
    for y in range(scan_top, scan_bottom):
        hits = sum(1 for x in range(region.left, region.right + 1) if 135 <= int(pixels[x, y]) <= 245)
        ratio = hits / float(max(1, panel_width))
        if 0.025 <= ratio <= 0.82:
            text_rows += 1
    return text_rows


def _better_span(
    current: tuple[int, int, float, int] | None,
    candidate: tuple[int, int, float, int],
) -> tuple[int, int, float, int]:
    if current is None or candidate[3] > current[3]:
        return candidate
    return current


def _panel_button_scan(
    pixels: Any,
    region: _ButtonRegion,
) -> tuple[int, float, tuple[int, int, float, int] | None]:
    button_rows = 0
    button_strength = 0.0
    button_span: tuple[int, int, float, int] | None = None
    start: int | None = None
    values: list[float] = []
    panel_width = region.right - region.left + 1
    for y in range(region.top, region.bottom):
        hits = sum(1 for x in range(region.left, region.right + 1) if int(pixels[x, y]) >= 205)
        ratio = hits / float(max(1, panel_width))
        button_strength = max(button_strength, ratio)
        if ratio >= 0.42:
            button_rows += 1
            if start is None:
                start = y
                values = []
            values.append(ratio)
        elif start is not None:
            candidate = (start, y - 1, sum(values) / float(max(1, len(values))), len(values))
            button_span = _better_span(button_span, candidate)
            start = None
    if start is not None:
        candidate = (start, region.bottom - 1, sum(values) / float(max(1, len(values))), len(values))
        button_span = _better_span(button_span, candidate)
    return button_rows, button_strength, button_span


def _panel_shape_ok(values: _PanelShapeInput) -> bool:
    return bool(
        values.body_rows >= values.min_body_rows
        and values.panel_width >= values.min_panel_width
        and values.body_density >= 0.50
        and values.col_density >= 0.58
    )


def _panel_breakdown(panel_shape: bool, text_matched: bool, button_matched: bool) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    if panel_shape:
        breakdown["wide_disconnect_panel"] = 0.42
    if text_matched:
        breakdown["panel_text_rows"] = 0.18
    if button_matched:
        breakdown["leave_button_bar"] = 0.38
    return breakdown


def _small_disconnect_panel_features(screenshot: Any) -> dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        x_min, x_max = int(width * 0.03), int(width * 0.97)
        y_min, y_max = int(height * 0.10), int(height * 0.92)
        min_body_rows = _scaled_min(height, 0.18, 28)
        min_panel_width = _scaled_min(width, 0.55, 120)
        min_button_rows = _scaled_min(height, 0.04, 7)

        spans = _band_spans(
            pixels,
            _BandScan(range(y_min, y_max), range(x_min, x_max), 38, 115, 0.42, True, True),
        )
        body = _best_span(spans)
        if not body:
            return {"matched": False, "score": 0.0, "source": "small_panel", "reason": "no_dark_panel"}
        body_top, body_bottom, body_density, body_rows = body

        col_spans = _band_spans(
            pixels,
            _BandScan(range(x_min, x_max), range(body_top, body_bottom + 1), 38, 115, 0.34, True, False),
        )
        col = _best_span(col_spans)
        if not col:
            return {"matched": False, "score": 0.0, "source": "small_panel", "reason": "no_panel_columns"}
        panel_left, panel_right, col_density, _panel_cols = col
        panel_width = panel_right - panel_left + 1

        text_rows = _panel_text_rows(
            pixels,
            _PanelRegion(left=panel_left, right=panel_right, top=body_top, bottom=body_bottom, height=height),
        )

        button_top = max(y_min, int(body_top + body_rows * 0.52))
        button_bottom = min(height, body_bottom + _scaled_min(height, 0.20, 28))
        button_rows, button_strength, button_span = _panel_button_scan(
            pixels,
            _ButtonRegion(left=panel_left, right=panel_right, top=button_top, bottom=button_bottom),
        )

        panel_shape = _panel_shape_ok(
            _PanelShapeInput(body_rows, panel_width, body_density, col_density, min_body_rows, min_panel_width)
        )
        button_matched = bool(button_span and button_rows >= min_button_rows and button_strength >= 0.48)
        text_matched = text_rows >= _scaled_min(height, 0.018, 3)
        breakdown = _panel_breakdown(panel_shape, text_matched, button_matched)
        score = round(sum(breakdown.values()), 3)
        matched = bool(panel_shape and text_matched and button_matched)
        return {
            "matched": matched,
            "score": score,
            "strength": "strong" if matched else ("weak" if panel_shape or button_matched else "none"),
            "source": "small_panel",
            "breakdown": breakdown,
            "rect": {
                "x": panel_left,
                "y": body_top,
                "width": panel_width,
                "height": body_rows,
                "body_density": round(body_density, 3),
                "col_density": round(col_density, 3),
            },
            "button": {
                "rows": button_rows,
                "strength": round(button_strength, 3),
                "span": button_span,
            },
            "text_rows": text_rows,
            "panel_shape": panel_shape,
            "button_matched": button_matched,
            "text_matched": text_matched,
            "analysis_size": f"{width}x{height}",
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "source": "small_panel", "reason": f"small_panel:{exc}"}


def _puzzle_frame_features(screenshot: Any) -> dict[str, Any]:
    """Detect a large centered dark puzzle frame, the signature of an
    Arkose/FunCaptcha challenge. Roblox's auto-passing "Security Verification"
    page is just text + a button on a white page and must NOT match."""
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        dark_mask: list[list[bool]] = []
        for y in range(height):
            row: list[bool] = []
            for x in range(width):
                row.append(int(pixels[x, y]) <= 100)
            dark_mask.append(row)
        min_w = _scaled_min(width, 0.20, 44)
        min_h = _scaled_min(height, 0.13, 26)
        for item in _binary_components(dark_mask):
            item_w = int(item["width"])
            item_h = int(item["height"])
            area = int(item["area"])
            if item_w < min_w or item_h < min_h:
                continue
            center_x = int(item["x"]) + item_w / 2.0
            center_y = int(item["y"]) + item_h / 2.0
            if not (width * 0.22 <= center_x <= width * 0.78 and height * 0.22 <= center_y <= height * 0.78):
                continue
            fill = area / float(max(1, item_w * item_h))
            # solid puzzle placeholder box or hollow frame both qualify; thin
            # text strips do not. Dark-page modals are excluded by the caller's
            # white_ratio requirement, so a solid box is fine here.
            if not (0.10 <= fill <= 1.0):
                continue
            return {
                "matched": True,
                "x": int(item["x"]),
                "y": int(item["y"]),
                "width": item_w,
                "height": item_h,
                "fill": round(fill, 3),
            }
        return {"matched": False}
    except Exception as exc:
        return {"matched": False, "reason": f"puzzle_frame:{exc}"}


def _white_ratio(pixels: Any, width: int, height: int) -> float:
    white_samples = 0
    white_hits = 0
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            white_samples += 1
            if int(pixels[x, y]) >= 225:
                white_hits += 1
    return white_hits / float(max(1, white_samples))


def _captcha_button(pixels: Any, region: _ButtonRegion, width: int, height: int) -> dict[str, Any]:
    mask = _mask_rect(pixels, range(region.left, region.right), range(region.top, region.bottom), 100, 215)
    min_button_w = _scaled_min(width, 0.10, 22)
    min_button_h = _scaled_min(height, 0.035, 6)
    for item in _binary_components(mask):
        item_w = int(item["width"])
        item_h = int(item["height"])
        area = int(item["area"])
        if item_w < min_button_w or item_h < min_button_h:
            continue
        fill = area / float(max(1, item_w * item_h))
        center_x = region.left + int(item["x"]) + item_w / 2.0
        center_y = region.top + int(item["y"]) + item_h / 2.0
        if fill >= 0.50 and width * 0.30 <= center_x <= width * 0.70 and height * 0.45 <= center_y <= height * 0.78:
            return {
                **item,
                "x": region.left + int(item["x"]),
                "y": region.top + int(item["y"]),
                "fill": round(fill, 3),
            }
    return {}


def _captcha_breakdown(white_ratio: float, frame_matched: bool, button: dict[str, Any]) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    if white_ratio >= 0.70:
        breakdown["white_security_page"] = 0.30
    if frame_matched:
        breakdown["puzzle_frame"] = 0.40
    if button:
        breakdown["start_puzzle_button"] = 0.48
    return breakdown


def _captcha_challenge_features(screenshot: Any) -> dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        white_ratio = _white_ratio(pixels, width, height)

        region = _ButtonRegion(
            left=int(width * 0.24),
            right=int(width * 0.76),
            top=int(height * 0.26),
            bottom=int(height * 0.86),
        )
        button = _captcha_button(pixels, region, width, height)

        frame = _puzzle_frame_features(screenshot)
        frame_matched = bool(frame.get("matched"))
        breakdown = _captcha_breakdown(white_ratio, frame_matched, button)
        score = round(sum(breakdown.values()), 3)
        matched = bool(button and white_ratio >= 0.78 and frame_matched)
        return {
            "matched": matched,
            "score": score,
            "strength": "strong" if matched else ("weak" if button else "none"),
            "source": "captcha_challenge",
            "breakdown": breakdown,
            "white_ratio": round(white_ratio, 3),
            "frame": frame,
            "button": button,
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "source": "captcha_challenge", "reason": f"captcha:{exc}"}


def _modal_button_confirmed(modal_shape: bool, button: bool, separator: bool, overlay_confirmed: bool) -> bool:
    return bool(modal_shape and button and (separator or overlay_confirmed))


def _structural_confirmed(
    overlay_confirmed: bool,
    structural_score: float,
    structural_button: bool,
    structural_separator: float,
    button_layout: dict[str, Any],
) -> bool:
    return bool(
        overlay_confirmed
        and structural_score >= 0.96
        and structural_button
        and structural_separator >= 0.45
        and str(button_layout.get("pattern") or "none") in {"single", "double", "bar"}
    )


def _visual_flags(
    overlay: dict[str, Any],
    center_modal: dict[str, Any],
    button_layout: dict[str, Any],
    structural: dict[str, Any],
    small_panel: dict[str, Any],
    captcha_challenge: dict[str, Any],
) -> _VisualFlags:
    structural_score = _score_of(structural)
    structural_separator = float(structural.get("separator_strength") or 0.0)
    structural_button = bool((structural.get("button") or {}).get("area"))
    modal_shape = bool(center_modal.get("centered") and center_modal.get("modal_shape"))
    separator = bool(center_modal.get("separator_matched"))
    button = bool(button_layout.get("matched") or center_modal.get("button_matched"))
    overlay_confirmed = bool(overlay.get("matched"))
    return _VisualFlags(
        overlay_score=_score_of(overlay),
        modal_score=_score_of(center_modal),
        button_score=_score_of(button_layout),
        structural_score=structural_score,
        small_panel_score=_score_of(small_panel),
        captcha_score=_score_of(captcha_challenge),
        structural_separator=structural_separator,
        structural_button=structural_button,
        modal_shape=modal_shape,
        separator=separator,
        button=button,
        overlay_confirmed=overlay_confirmed,
        modal_button_confirmed=_modal_button_confirmed(modal_shape, button, separator, overlay_confirmed),
        small_panel_confirmed=bool(small_panel.get("matched")),
        captcha_confirmed=bool(captcha_challenge.get("matched")),
        structural_button_confirmed=_structural_confirmed(
            overlay_confirmed,
            structural_score,
            structural_button,
            structural_separator,
            button_layout,
        ),
    )


def _pipeline_score(flags: _VisualFlags, template_score: float) -> float:
    return round(
        (0.18 if flags.overlay_confirmed else 0.0)
        + (0.38 if flags.modal_shape else 0.0)
        + (0.30 if flags.button else 0.0)
        + (0.16 if flags.separator else 0.0)
        + min(0.75, template_score),
        3,
    )


def _is_strong(flags: _VisualFlags, template_matched: bool) -> bool:
    return bool(
        template_matched
        or flags.modal_button_confirmed
        or flags.structural_button_confirmed
        or flags.small_panel_confirmed
        or flags.captcha_confirmed
    )


def _visual_source(flags: _VisualFlags, template_matched: bool, strong: bool, weak: bool) -> str:
    if flags.captcha_confirmed:
        return "captcha_challenge"
    if template_matched:
        return "template"
    if strong:
        return "visual_pipeline"
    if weak:
        return "structural"
    return "none"


def _visual_stage(flags: _VisualFlags, template_matched: bool, weak: bool) -> str:
    if flags.captcha_confirmed:
        return "captcha_challenge"
    if template_matched:
        return "template"
    if flags.modal_button_confirmed:
        return "modal_button"
    if flags.small_panel_confirmed:
        return "small_panel"
    if flags.structural_button_confirmed:
        return "structural_button"
    if weak:
        return "structural_weak"
    return "none"


def detect_visual_features(screenshot: Any) -> dict[str, Any]:
    if screenshot is None:
        return {"matched": False, "score": 0.0, "reason": "no_screenshot"}

    screenshot = _as_luma(screenshot)
    overlay = _overlay_features(screenshot)
    center_modal = _center_modal_features(screenshot)
    button_layout = _button_layout_features(screenshot, dict(center_modal.get("rect") or {}))
    structural = _structural_popup_features(screenshot)
    small_panel = _small_disconnect_panel_features(screenshot)
    captcha_challenge = _captcha_challenge_features(screenshot)

    template_score = 0.0
    title_rms = 9999.0
    reconnect_rms = 9999.0
    template_matched = False
    template_reason = ""

    flags = _visual_flags(overlay, center_modal, button_layout, structural, small_panel, captcha_challenge)
    pipeline_score = _pipeline_score(flags, template_score)
    score = round(max(pipeline_score, template_score, flags.structural_score, flags.small_panel_score, flags.captcha_score), 3)
    strong = _is_strong(flags, template_matched)
    weak = bool(structural.get("matched") or flags.modal_shape or flags.button)
    matched = strong or weak
    source = _visual_source(flags, template_matched, strong, weak)
    stage = _visual_stage(flags, template_matched, weak)
    button_pattern = "bar" if flags.small_panel_confirmed else str(button_layout.get("pattern") or "none")

    return {
        "matched": matched,
        "score": score,
        "strength": "strong" if strong else ("weak" if weak else "none"),
        "source": source,
        "visual_stage": stage,
        "button_pattern": button_pattern,
        "overlay_score": round(flags.overlay_score, 3),
        "modal_score": round(flags.modal_score, 3),
        "button_score": round(flags.button_score, 3),
        "template_score": round(template_score, 3),
        "structural_score": round(flags.structural_score, 3),
        "small_panel_score": round(flags.small_panel_score, 3),
        "captcha_score": round(flags.captcha_score, 3),
        "captcha_challenge": flags.captcha_confirmed,
        "title_rms": round(title_rms, 2),
        "reconnect_rms": round(reconnect_rms, 2),
        "overlay": overlay,
        "center_modal": center_modal,
        "button_layout": button_layout,
        "structural": structural,
        "small_panel": small_panel,
        "captcha": captcha_challenge,
        "reason": template_reason,
    }
