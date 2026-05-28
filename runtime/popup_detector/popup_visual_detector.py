from __future__ import annotations

import math
from collections import deque
from typing import Any, Dict, List, Tuple

from app_paths import resource_path


VISUAL_TEMPLATE_BASE_SIZE = (679, 513)
VISUAL_TITLE_BOX = (222, 157, 459, 205)
VISUAL_RECONNECT_BOX = (372, 306, 550, 346)
STRUCTURAL_ANALYSIS_SIZE = (160, 120)
MODAL_ANALYSIS_SIZE = (320, 240)

_TEMPLATE_CACHE: Dict[str, Any] = {}


def _load_template(name: str):
    if name in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[name]
    try:
        from PIL import Image

        image = Image.open(resource_path("vision_templates", name)).convert("L")
        _TEMPLATE_CACHE[name] = image
        return image
    except Exception:
        _TEMPLATE_CACHE[name] = None
        return None


def _as_luma(screenshot: Any):
    try:
        if getattr(screenshot, "mode", "") == "L":
            return screenshot
        return screenshot.convert("L")
    except Exception:
        return screenshot


def _resize_luma(screenshot: Any, size: Tuple[int, int]):
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


def _scaled_box(box: Tuple[int, int, int, int], size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    base_w, base_h = VISUAL_TEMPLATE_BASE_SIZE
    width, height = size
    sx = float(width) / float(base_w)
    sy = float(height) / float(base_h)
    left, top, right, bottom = box
    return (
        max(0, int(round(left * sx))),
        max(0, int(round(top * sy))),
        min(width, int(round(right * sx))),
        min(height, int(round(bottom * sy))),
    )


def _rmsdiff(img_a: Any, img_b: Any) -> float:
    try:
        from PIL import ImageChops

        diff = ImageChops.difference(img_a, img_b)
        hist = diff.histogram()
        sq = sum((value * ((idx % 256) ** 2)) for idx, value in enumerate(hist))
        total = max(1, img_a.size[0] * img_a.size[1])
        return math.sqrt(float(sq) / float(total))
    except Exception:
        return 9999.0


def _binary_components(mask: List[List[bool]]) -> List[Dict[str, int]]:
    if not mask or not mask[0]:
        return []
    height = len(mask)
    width = len(mask[0])
    seen = [[False] * width for _ in range(height)]
    components: List[Dict[str, int]] = []
    for y in range(height):
        for x in range(width):
            if seen[y][x] or not mask[y][x]:
                continue
            q = deque([(x, y)])
            seen[y][x] = True
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while q:
                cx, cy = q.popleft()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    if seen[ny][nx] or not mask[ny][nx]:
                        continue
                    seen[ny][nx] = True
                    q.append((nx, ny))
            components.append({
                "x": min_x,
                "y": min_y,
                "width": max_x - min_x + 1,
                "height": max_y - min_y + 1,
                "area": area,
            })
    components.sort(key=lambda item: int(item["area"]), reverse=True)
    return components


def _overlay_features(screenshot: Any) -> Dict[str, Any]:
    try:
        small = _resize_luma(screenshot, STRUCTURAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        values: List[int] = []
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
        if not values:
            return {"matched": False, "score": 0.0, "reason": "no_background"}

        mean = sum(values) / float(len(values))
        variance = sum((value - mean) ** 2 for value in values) / float(len(values))
        stddev = math.sqrt(variance)
        edge = edge_total / float(max(1, edge_count))
        dark_ratio = sum(1 for value in values if value <= 125) / float(len(values))
        mid_ratio = sum(1 for value in values if 55 <= value <= 175) / float(len(values))
        dim_ratio = sum(1 for value in values if value <= 165) / float(len(values))

        breakdown: Dict[str, float] = {}
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


def _structural_popup_features(screenshot: Any) -> Dict[str, Any]:
    try:
        small = _resize_luma(screenshot, STRUCTURAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        min_modal_w = _scaled_min(width, 0.175, 12)
        min_modal_h = _scaled_min(height, 0.133, 8)
        min_button_w = _scaled_min(width, 0.088, 8)
        min_button_h = _scaled_min(height, 0.033, 3)

        dark_mask: List[List[bool]] = []
        bright_mask: List[List[bool]] = []
        for y in range(height):
            dark_row: List[bool] = []
            bright_row: List[bool] = []
            for x in range(width):
                value = int(pixels[x, y])
                dark_row.append(35 <= value <= 100)
                bright_row.append(value >= 215)
            dark_mask.append(dark_row)
            bright_mask.append(bright_row)

        modal_candidates = []
        for item in _binary_components(dark_mask):
            item_w = int(item["width"])
            item_h = int(item["height"])
            area = int(item["area"])
            center_x = int(item["x"]) + item_w / 2.0
            center_y = int(item["y"]) + item_h / 2.0
            if item_w < min_modal_w or item_h < min_modal_h:
                continue
            if not (width * 0.18 <= center_x <= width * 0.82 and height * 0.18 <= center_y <= height * 0.82):
                continue
            fill = area / float(max(1, item_w * item_h))
            if fill < 0.52:
                continue
            modal_candidates.append({**item, "fill": round(fill, 3)})
        modal = modal_candidates[0] if modal_candidates else {}

        button_candidates = []
        for item in _binary_components(bright_mask):
            item_w = int(item["width"])
            item_h = int(item["height"])
            area = int(item["area"])
            center_x = int(item["x"]) + item_w / 2.0
            center_y = int(item["y"]) + item_h / 2.0
            if item_w < min_button_w or item_h < min_button_h:
                continue
            if not (width * 0.10 <= center_x <= width * 0.90 and height * 0.45 <= center_y <= height * 0.95):
                continue
            fill = area / float(max(1, item_w * item_h))
            if fill < 0.42:
                continue
            button_candidates.append({**item, "fill": round(fill, 3)})
        button = button_candidates[0] if button_candidates else {}

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

        breakdown: Dict[str, float] = {}
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


def _center_modal_features(screenshot: Any) -> Dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        min_body_rows = _scaled_min(height, 0.075, 12)
        min_modal_cols = _scaled_min(width, 0.141, 24)
        min_button_rows = _scaled_min(height, 0.013, 2)
        x_min, x_max = int(width * 0.08), int(width * 0.92)
        y_min, y_max = int(height * 0.10), int(height * 0.90)

        row_spans: List[Tuple[int, int, float, int]] = []
        start = None
        values: List[float] = []
        for y in range(y_min, y_max):
            hits = sum(1 for x in range(x_min, x_max) if 35 <= int(pixels[x, y]) <= 105)
            ratio = hits / float(max(1, x_max - x_min))
            if ratio > 0.20:
                if start is None:
                    start = y
                    values = []
                values.append(ratio)
            elif start is not None:
                row_spans.append((start, y - 1, sum(values) / float(max(1, len(values))), len(values)))
                start = None
        if start is not None:
            row_spans.append((start, y_max - 1, sum(values) / float(max(1, len(values))), len(values)))

        body = max(row_spans, key=lambda item: item[2] * item[3], default=None)
        if not body:
            return {"matched": False, "score": 0.0, "reason": "no_center_body"}
        body_top, body_bottom, body_density, body_rows = body

        col_spans: List[Tuple[int, int, float, int]] = []
        start = None
        values = []
        for x in range(x_min, x_max):
            hits = sum(1 for y in range(body_top, body_bottom + 1) if 35 <= int(pixels[x, y]) <= 105)
            ratio = hits / float(max(1, body_bottom - body_top + 1))
            if ratio > 0.32:
                if start is None:
                    start = x
                    values = []
                values.append(ratio)
            elif start is not None:
                col_spans.append((start, x - 1, sum(values) / float(max(1, len(values))), len(values)))
                start = None
        if start is not None:
            col_spans.append((start, x_max - 1, sum(values) / float(max(1, len(values))), len(values)))

        col = max(col_spans, key=lambda item: item[2] * item[3], default=None)
        if not col:
            return {"matched": False, "score": 0.0, "reason": "no_center_columns"}
        modal_left, modal_right, col_density, modal_cols = col
        modal_width = modal_right - modal_left + 1
        body_height = body_bottom - body_top + 1
        modal_center_x = (modal_left + modal_right) / 2.0
        modal_center_y = (body_top + body_bottom) / 2.0

        separator_strength = 0.0
        for y in range(max(y_min, body_top - int(height * 0.18)), min(height, body_top + int(body_height * 0.32))):
            hits = sum(1 for x in range(modal_left, modal_right + 1) if 135 <= int(pixels[x, y]) <= 235)
            separator_strength = max(separator_strength, hits / float(max(1, modal_width)))

        button_rows = 0
        button_strength = 0.0
        search_top = int(body_top + body_height * 0.54)
        search_bottom = min(height, body_bottom + int(height * 0.10))
        for y in range(search_top, search_bottom):
            hits = sum(1 for x in range(modal_left, modal_right + 1) if int(pixels[x, y]) >= 205)
            ratio = hits / float(max(1, modal_width))
            button_strength = max(button_strength, ratio)
            if ratio >= 0.14:
                button_rows += 1

        width_frac = modal_width / float(width)
        height_frac = body_height / float(height)
        centered = (
            width * 0.25 <= modal_center_x <= width * 0.75
            and height * 0.24 <= modal_center_y <= height * 0.76
        )
        modal_shape = (
            0.16 <= width_frac <= 0.78
            and 0.10 <= height_frac <= 0.54
            and body_density >= 0.34
            and col_density >= 0.58
            and body_rows >= min_body_rows
            and modal_cols >= min_modal_cols
        )
        separator_matched = separator_strength >= 0.13
        button_matched = button_rows >= min_button_rows and button_strength >= 0.18
        controls = separator_matched and button_matched

        breakdown: Dict[str, float] = {}
        if centered:
            breakdown["centered_modal"] = 0.20
        if modal_shape:
            breakdown["modal_body"] = 0.36
        if separator_matched:
            breakdown["title_separator"] = 0.18
        if button_matched:
            breakdown["disconnect_button_bar"] = 0.34

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


def _button_layout_features(screenshot: Any, modal_rect: Dict[str, Any] | None = None) -> Dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        min_button_w = _scaled_min(width, 0.056, 10)
        min_button_h = _scaled_min(height, 0.017, 3)
        min_button_rows = _scaled_min(height, 0.013, 2)
        rect = dict(modal_rect or {})
        if rect:
            left = max(0, int(rect.get("x") or 0))
            top = max(0, int(rect.get("y") or 0))
            right = min(width, left + max(1, int(rect.get("width") or 0)))
            bottom = min(height, top + max(1, int(rect.get("height") or 0)))
        else:
            left, top, right, bottom = int(width * 0.12), int(height * 0.22), int(width * 0.88), int(height * 0.88)
        if right <= left or bottom <= top:
            return {"matched": False, "score": 0.0, "pattern": "none"}

        rect_height = max(1, bottom - top)
        search_top = int(top + rect_height * 0.52)
        search_bottom = min(height, int(bottom + rect_height * 0.45))
        mask: List[List[bool]] = []
        for y in range(search_top, search_bottom):
            row: List[bool] = []
            for x in range(left, right):
                row.append(int(pixels[x, y]) >= 205)
            mask.append(row)

        components = []
        for item in _binary_components(mask):
            item_w = int(item["width"])
            item_h = int(item["height"])
            area = int(item["area"])
            fill = area / float(max(1, item_w * item_h))
            if item_w < min_button_w or item_h < min_button_h:
                continue
            if fill < 0.36:
                continue
            components.append({
                "x": left + int(item["x"]),
                "y": search_top + int(item["y"]),
                "width": item_w,
                "height": item_h,
                "area": area,
                "fill": round(fill, 3),
            })

        row_strength = 0.0
        row_hits = 0
        for y in range(search_top, search_bottom):
            hits = sum(1 for x in range(left, right) if int(pixels[x, y]) >= 205)
            ratio = hits / float(max(1, right - left))
            row_strength = max(row_strength, ratio)
            if ratio >= 0.14:
                row_hits += 1

        count = len(components)
        pattern = "double" if count >= 2 else ("single" if count == 1 else ("bar" if row_hits >= min_button_rows else "none"))
        score = 0.0
        if pattern == "double":
            score = 0.62
        elif pattern == "single":
            score = 0.48
        elif pattern == "bar":
            score = 0.38
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


def _small_disconnect_panel_features(screenshot: Any) -> Dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        x_min, x_max = int(width * 0.03), int(width * 0.97)
        y_min, y_max = int(height * 0.10), int(height * 0.92)
        min_body_rows = _scaled_min(height, 0.18, 28)
        min_panel_width = _scaled_min(width, 0.55, 120)
        min_button_rows = _scaled_min(height, 0.04, 7)

        spans: List[Tuple[int, int, float, int]] = []
        start = None
        values: List[float] = []
        for y in range(y_min, y_max):
            hits = sum(1 for x in range(x_min, x_max) if 38 <= int(pixels[x, y]) <= 115)
            ratio = hits / float(max(1, x_max - x_min))
            if ratio >= 0.42:
                if start is None:
                    start = y
                    values = []
                values.append(ratio)
            elif start is not None:
                spans.append((start, y - 1, sum(values) / float(max(1, len(values))), len(values)))
                start = None
        if start is not None:
            spans.append((start, y_max - 1, sum(values) / float(max(1, len(values))), len(values)))

        body = max(spans, key=lambda item: item[2] * item[3], default=None)
        if not body:
            return {"matched": False, "score": 0.0, "source": "small_panel", "reason": "no_dark_panel"}
        body_top, body_bottom, body_density, body_rows = body

        col_spans: List[Tuple[int, int, float, int]] = []
        start = None
        values = []
        for x in range(x_min, x_max):
            hits = sum(1 for y in range(body_top, body_bottom + 1) if 38 <= int(pixels[x, y]) <= 115)
            ratio = hits / float(max(1, body_rows))
            if ratio >= 0.34:
                if start is None:
                    start = x
                    values = []
                values.append(ratio)
            elif start is not None:
                col_spans.append((start, x - 1, sum(values) / float(max(1, len(values))), len(values)))
                start = None
        if start is not None:
            col_spans.append((start, x_max - 1, sum(values) / float(max(1, len(values))), len(values)))
        col = max(col_spans, key=lambda item: item[2] * item[3], default=None)
        if not col:
            return {"matched": False, "score": 0.0, "source": "small_panel", "reason": "no_panel_columns"}
        panel_left, panel_right, col_density, panel_cols = col
        panel_width = panel_right - panel_left + 1

        text_rows = 0
        for y in range(body_top + _scaled_min(height, 0.05, 8), body_bottom - _scaled_min(height, 0.03, 4)):
            hits = sum(1 for x in range(panel_left, panel_right + 1) if 135 <= int(pixels[x, y]) <= 245)
            ratio = hits / float(max(1, panel_width))
            if 0.025 <= ratio <= 0.82:
                text_rows += 1

        button_top = max(y_min, int(body_top + body_rows * 0.52))
        button_bottom = min(height, body_bottom + _scaled_min(height, 0.20, 28))
        button_rows = 0
        button_strength = 0.0
        button_span = None
        start = None
        values = []
        for y in range(button_top, button_bottom):
            hits = sum(1 for x in range(panel_left, panel_right + 1) if int(pixels[x, y]) >= 205)
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
                if button_span is None or candidate[3] > button_span[3]:
                    button_span = candidate
                start = None
        if start is not None:
            candidate = (start, button_bottom - 1, sum(values) / float(max(1, len(values))), len(values))
            if button_span is None or candidate[3] > button_span[3]:
                button_span = candidate

        panel_shape = bool(
            body_rows >= min_body_rows
            and panel_width >= min_panel_width
            and body_density >= 0.50
            and col_density >= 0.58
        )
        button_matched = bool(button_span and button_rows >= min_button_rows and button_strength >= 0.48)
        text_matched = text_rows >= _scaled_min(height, 0.018, 3)
        breakdown: Dict[str, float] = {}
        if panel_shape:
            breakdown["wide_disconnect_panel"] = 0.42
        if text_matched:
            breakdown["panel_text_rows"] = 0.18
        if button_matched:
            breakdown["leave_button_bar"] = 0.38
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


def _captcha_challenge_features(screenshot: Any) -> Dict[str, Any]:
    try:
        small = _resize_luma(screenshot, MODAL_ANALYSIS_SIZE)
        pixels = small.load()
        width, height = small.size
        white_samples = 0
        white_hits = 0
        for y in range(0, height, 3):
            for x in range(0, width, 3):
                white_samples += 1
                if int(pixels[x, y]) >= 225:
                    white_hits += 1
        white_ratio = white_hits / float(max(1, white_samples))

        x_min, x_max = int(width * 0.24), int(width * 0.76)
        y_min, y_max = int(height * 0.26), int(height * 0.86)
        dark_rows = 0
        for y in range(int(height * 0.04), int(height * 0.62)):
            hits = sum(1 for x in range(int(width * 0.18), int(width * 0.82)) if int(pixels[x, y]) <= 90)
            if hits / float(max(1, int(width * 0.64))) >= 0.015:
                dark_rows += 1

        green_button_mask: List[List[bool]] = []
        for y in range(y_min, y_max):
            row: List[bool] = []
            for x in range(x_min, x_max):
                value = int(pixels[x, y])
                row.append(120 <= value <= 205)
            green_button_mask.append(row)

        button = {}
        min_button_w = _scaled_min(width, 0.10, 22)
        min_button_h = _scaled_min(height, 0.035, 6)
        for item in _binary_components(green_button_mask):
            item_w = int(item["width"])
            item_h = int(item["height"])
            area = int(item["area"])
            if item_w < min_button_w or item_h < min_button_h:
                continue
            fill = area / float(max(1, item_w * item_h))
            center_x = x_min + int(item["x"]) + item_w / 2.0
            center_y = y_min + int(item["y"]) + item_h / 2.0
            if fill >= 0.50 and width * 0.30 <= center_x <= width * 0.70 and height * 0.45 <= center_y <= height * 0.78:
                button = {**item, "x": x_min + int(item["x"]), "y": y_min + int(item["y"]), "fill": round(fill, 3)}
                break

        breakdown: Dict[str, float] = {}
        if white_ratio >= 0.70:
            breakdown["white_security_page"] = 0.35
        if dark_rows >= _scaled_min(height, 0.025, 5):
            breakdown["security_text_rows"] = 0.22
        if button:
            breakdown["start_puzzle_button"] = 0.48
        score = round(sum(breakdown.values()), 3)
        matched = bool(button and (white_ratio >= 0.82 or (white_ratio >= 0.70 and dark_rows >= _scaled_min(height, 0.018, 4))))
        return {
            "matched": matched,
            "score": score,
            "strength": "strong" if matched else ("weak" if button else "none"),
            "source": "captcha_challenge",
            "breakdown": breakdown,
            "white_ratio": round(white_ratio, 3),
            "dark_rows": dark_rows,
            "button": button,
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "source": "captcha_challenge", "reason": f"captcha:{exc}"}


def detect_visual_features(screenshot: Any) -> Dict[str, Any]:
    if screenshot is None:
        return {"matched": False, "score": 0.0, "reason": "no_screenshot"}

    screenshot = _as_luma(screenshot)
    overlay = _overlay_features(screenshot)
    center_modal = _center_modal_features(screenshot)
    button_layout = _button_layout_features(screenshot, dict(center_modal.get("rect") or {}))
    structural = _structural_popup_features(screenshot)
    small_panel = _small_disconnect_panel_features(screenshot)
    captcha_challenge = _captcha_challenge_features(screenshot)

    title_template = _load_template("disconnect_title.png")
    reconnect_template = _load_template("disconnect_reconnect_btn.png")
    template_score = 0.0
    title_rms = 9999.0
    reconnect_rms = 9999.0
    template_matched = False
    template_reason = ""
    if title_template is None or reconnect_template is None:
        template_reason = "missing_template"
    else:
        try:
            title_box = _scaled_box(VISUAL_TITLE_BOX, screenshot.size)
            reconnect_box = _scaled_box(VISUAL_RECONNECT_BOX, screenshot.size)
            title_patch = screenshot.crop(title_box).resize(title_template.size)
            reconnect_patch = screenshot.crop(reconnect_box).resize(reconnect_template.size)
            title_rms = _rmsdiff(title_patch, title_template)
            reconnect_rms = _rmsdiff(reconnect_patch, reconnect_template)
            title_score = max(0.0, min(0.55, (38.0 - title_rms) / 38.0 * 0.55))
            reconnect_score = max(0.0, min(0.55, (54.0 - reconnect_rms) / 54.0 * 0.55))
            template_score = round(title_score + reconnect_score, 3)
            template_matched = template_score >= 0.55
        except Exception as exc:
            template_reason = str(exc)

    modal_score = float(center_modal.get("score") or 0.0)
    button_score = float(button_layout.get("score") or 0.0)
    overlay_score = float(overlay.get("score") or 0.0)
    structural_score = float(structural.get("score") or 0.0)
    small_panel_score = float(small_panel.get("score") or 0.0)
    captcha_score = float(captcha_challenge.get("score") or 0.0)
    structural_separator = float(structural.get("separator_strength") or 0.0)
    structural_button = bool((structural.get("button") or {}).get("area"))
    modal_shape = bool(center_modal.get("centered") and center_modal.get("modal_shape"))
    separator = bool(center_modal.get("separator_matched"))
    button = bool(button_layout.get("matched") or center_modal.get("button_matched"))
    overlay_confirmed = bool(overlay.get("matched"))
    modal_button_confirmed = bool(modal_shape and button and (separator or overlay_confirmed))
    small_panel_confirmed = bool(small_panel.get("matched"))
    captcha_confirmed = bool(captcha_challenge.get("matched"))
    structural_button_confirmed = bool(
        overlay_confirmed
        and structural_score >= 0.96
        and structural_button
        and structural_separator >= 0.45
        and str(button_layout.get("pattern") or "none") in {"single", "double", "bar"}
    )

    pipeline_score = round(
        (0.18 if overlay_confirmed else 0.0)
        + (0.38 if modal_shape else 0.0)
        + (0.30 if button else 0.0)
        + (0.16 if separator else 0.0)
        + min(0.75, template_score),
        3,
    )
    score = round(max(pipeline_score, template_score, structural_score, small_panel_score, captcha_score), 3)
    strong = bool(template_matched or modal_button_confirmed or structural_button_confirmed or small_panel_confirmed or captcha_confirmed)
    weak = bool(structural.get("matched") or modal_shape or button)
    matched = strong or weak
    source = "captcha_challenge" if captcha_confirmed else ("template" if template_matched else ("visual_pipeline" if strong else ("structural" if weak else "none")))
    stage = (
        "captcha_challenge"
        if captcha_confirmed
        else (
            "template"
            if template_matched
            else (
                "modal_button"
                if modal_button_confirmed
                else ("small_panel" if small_panel_confirmed else ("structural_button" if structural_button_confirmed else ("structural_weak" if weak else "none")))
            )
        )
    )
    button_pattern = "bar" if small_panel_confirmed else str(button_layout.get("pattern") or "none")

    return {
        "matched": matched,
        "score": score,
        "strength": "strong" if strong else ("weak" if weak else "none"),
        "source": source,
        "visual_stage": stage,
        "button_pattern": button_pattern,
        "overlay_score": round(overlay_score, 3),
        "modal_score": round(modal_score, 3),
        "button_score": round(button_score, 3),
        "template_score": round(template_score, 3),
        "structural_score": round(structural_score, 3),
        "small_panel_score": round(small_panel_score, 3),
        "captcha_score": round(captcha_score, 3),
        "captcha_challenge": captcha_confirmed,
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
