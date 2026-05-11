from __future__ import annotations

import math
from collections import deque
from typing import Any, Dict, List, Tuple

from app_paths import resource_path


VISUAL_TEMPLATE_BASE_SIZE = (679, 513)
VISUAL_TITLE_BOX = (222, 157, 459, 205)
VISUAL_RECONNECT_BOX = (372, 306, 550, 346)

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


def _structural_popup_features(screenshot: Any) -> Dict[str, Any]:
    try:
        small = screenshot.resize((160, 120))
        pixels = small.load()
        width, height = small.size

        dark_mask: List[List[bool]] = []
        bright_mask: List[List[bool]] = []
        for y in range(height):
            dark_row: List[bool] = []
            bright_row: List[bool] = []
            for x in range(width):
                value = int(pixels[x, y])
                dark_row.append(35 <= value <= 95)
                bright_row.append(value >= 220)
            dark_mask.append(dark_row)
            bright_mask.append(bright_row)

        modal_candidates = []
        for item in _binary_components(dark_mask):
            item_w = int(item["width"])
            item_h = int(item["height"])
            area = int(item["area"])
            center_x = int(item["x"]) + item_w / 2.0
            center_y = int(item["y"]) + item_h / 2.0
            if item_w < 55 or item_h < 30:
                continue
            if not (width * 0.25 <= center_x <= width * 0.75 and height * 0.25 <= center_y <= height * 0.75):
                continue
            fill = area / float(max(1, item_w * item_h))
            if fill < 0.55:
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
            if item_w < 24 or item_h < 5:
                continue
            if not (width * 0.15 <= center_x <= width * 0.85 and height * 0.50 <= center_y <= height * 0.92):
                continue
            fill = area / float(max(1, item_w * item_h))
            if fill < 0.50:
                continue
            button_candidates.append({**item, "fill": round(fill, 3)})
        button = button_candidates[0] if button_candidates else {}

        separator = False
        separator_strength = 0.0
        for y in range(int(height * 0.28), int(height * 0.46)):
            row_hits = 0
            for x in range(int(width * 0.22), int(width * 0.78)):
                value = int(pixels[x, y])
                if 145 <= value <= 230:
                    row_hits += 1
            strength = row_hits / float(max(1, int(width * 0.56)))
            if strength > separator_strength:
                separator_strength = strength
            if strength >= 0.18:
                separator = True
                break

        score = 0.0
        breakdown: Dict[str, float] = {}
        if modal:
            breakdown["modal"] = 0.35
        if button:
            breakdown["disconnect_button_shape"] = 0.45
        if separator:
            breakdown["separator_line"] = 0.25
        if modal and button:
            modal_bottom = int(modal["y"]) + int(modal["height"])
            button_y = int(button["y"])
            if button_y < modal_bottom and int(button["y"]) > int(modal["y"]):
                breakdown["button_inside_modal"] = 0.20
        score = round(sum(breakdown.values()), 3)
        return {
            "matched": score >= 0.70,
            "score": score,
            "strength": "weak" if score >= 0.70 else "none",
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
        small = screenshot.resize((320, 240))
        pixels = small.load()
        width, height = small.size
        x_min, x_max = int(width * 0.18), int(width * 0.82)
        y_min, y_max = int(height * 0.18), int(height * 0.82)

        row_spans: List[Tuple[int, int, float, int]] = []
        start = None
        values: List[float] = []
        for y in range(y_min, y_max):
            hits = sum(1 for x in range(x_min, x_max) if 35 <= int(pixels[x, y]) <= 95)
            ratio = hits / float(max(1, x_max - x_min))
            if ratio > 0.38:
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
            hits = sum(1 for y in range(body_top, body_bottom + 1) if 35 <= int(pixels[x, y]) <= 95)
            ratio = hits / float(max(1, body_bottom - body_top + 1))
            if ratio > 0.38:
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
        for y in range(max(y_min, body_top - int(height * 0.20)), body_top + 1):
            hits = sum(1 for x in range(modal_left, modal_right + 1) if 135 <= int(pixels[x, y]) <= 235)
            separator_strength = max(separator_strength, hits / float(max(1, modal_width)))

        button_rows = 0
        button_strength = 0.0
        search_top = int(body_top + body_height * 0.58)
        search_bottom = min(height, body_bottom + int(height * 0.12))
        for y in range(search_top, search_bottom):
            hits = sum(1 for x in range(modal_left, modal_right + 1) if int(pixels[x, y]) >= 210)
            ratio = hits / float(max(1, modal_width))
            button_strength = max(button_strength, ratio)
            if ratio >= 0.18:
                button_rows += 1

        width_frac = modal_width / float(width)
        height_frac = body_height / float(height)
        centered = (
            width * 0.32 <= modal_center_x <= width * 0.68
            and height * 0.30 <= modal_center_y <= height * 0.70
        )
        modal_shape = (
            0.36 <= width_frac <= 0.72
            and 0.14 <= height_frac <= 0.38
            and body_density >= 0.58
            and col_density >= 0.72
            and body_rows >= 28
            and modal_cols >= 105
        )
        controls = separator_strength >= 0.16 and button_rows >= 3 and button_strength >= 0.25

        breakdown: Dict[str, float] = {}
        if centered:
            breakdown["centered_modal"] = 0.25
        if modal_shape:
            breakdown["modal_body"] = 0.45
        if separator_strength >= 0.16:
            breakdown["title_separator"] = 0.25
        if button_rows >= 3 and button_strength >= 0.25:
            breakdown["disconnect_button_bar"] = 0.45

        score = round(sum(breakdown.values()), 3)
        matched = bool(centered and modal_shape and controls)
        return {
            "matched": matched,
            "score": score,
            "strength": "strong" if matched else "none",
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
            "separator_strength": round(separator_strength, 3),
            "button_rows": button_rows,
            "button_strength": round(button_strength, 3),
        }
    except Exception as exc:
        return {"matched": False, "score": 0.0, "reason": f"center_modal:{exc}"}


def detect_visual_features(screenshot: Any) -> Dict[str, Any]:
    if screenshot is None:
        return {"matched": False, "score": 0.0, "reason": "no_screenshot"}

    structural = _structural_popup_features(screenshot)
    center_modal = _center_modal_features(screenshot)
    structural_strength = str(structural.get("strength") or "none")
    center_strong = bool(center_modal.get("matched")) and str(center_modal.get("strength") or "") == "strong"
    if center_strong:
        structural = {
            **structural,
            "matched": True,
            "score": max(float(structural.get("score") or 0.0), float(center_modal.get("score") or 0.0)),
            "strength": "strong",
            "source": "center_modal",
            "center_modal": center_modal,
        }
    else:
        structural = {**structural, "center_modal": center_modal, "strength": structural_strength}
    title_template = _load_template("disconnect_title.png")
    reconnect_template = _load_template("disconnect_reconnect_btn.png")
    if title_template is None or reconnect_template is None:
        return {
            "matched": bool(structural.get("matched")),
            "score": float(structural.get("score") or 0.0),
            "strength": str(structural.get("strength") or ("weak" if bool(structural.get("matched")) else "none")),
            "source": str(structural.get("source") or "structural"),
            "reason": "missing_template",
            "structural": structural,
        }

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
        structural_score = float(structural.get("score") or 0.0)
        score = round(max(template_score, structural_score), 3)
        template_matched = template_score >= 0.55
        structural_matched = bool(structural.get("matched"))
        structural_is_strong = str(structural.get("strength") or "") == "strong"
        return {
            "matched": template_matched or structural_matched,
            "score": score,
            "strength": "strong" if template_matched or structural_is_strong else ("weak" if structural_matched else "none"),
            "source": "template" if template_matched else (str(structural.get("source") or "structural") if structural_matched else "none"),
            "template_score": template_score,
            "structural_score": structural_score,
            "title_rms": round(title_rms, 2),
            "reconnect_rms": round(reconnect_rms, 2),
            "structural": structural,
        }
    except Exception as exc:
        return {
            "matched": bool(structural.get("matched")),
            "score": float(structural.get("score") or 0.0),
            "strength": str(structural.get("strength") or ("weak" if bool(structural.get("matched")) else "none")),
            "source": str(structural.get("source") or "structural"),
            "reason": str(exc),
            "structural": structural,
        }
