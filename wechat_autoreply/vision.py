from __future__ import annotations

import re
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

from AppKit import NSBitmapImageRep, NSImage
from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly

from .ocr import ocr_image


DIGIT_RE = re.compile(r"\d+")
BADGE_ROI_X_RATIO = 0.58
BADGE_ROI_Y_RATIO = 0.12
BADGE_ROI_H_RATIO = 0.76
OCR_UPSCALE_FACTOR = 3
ADAPTIVE_THRESHOLD_OFFSET = 8


def _control_center_items() -> list[dict[str, Any]]:
    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    items: list[dict[str, Any]] = []
    for window in windows:
        if window.get("kCGWindowOwnerName") != "Control Center":
            continue
        if int(window.get("kCGWindowLayer", -1) or -1) != 25:
            continue
        bounds = window.get("kCGWindowBounds") or {}
        width = int(bounds.get("Width", 0) or 0)
        height = int(bounds.get("Height", 0) or 0)
        if width < 20 or height < 20:
            continue
        items.append(
            {
                "name": str(window.get("kCGWindowName") or ""),
                "x": int(bounds.get("X", 0) or 0),
                "y": int(bounds.get("Y", 0) or 0),
                "width": width,
                "height": height,
            }
        )
    items.sort(key=lambda item: item["x"])
    return items


def _capture_region(info: dict[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    handle.close()
    path = Path(handle.name)
    subprocess.run(
        [
            "screencapture",
            "-R",
            f"{info['x']},{info['y']},{info['width']},{info['height']}",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def _bitmap_rep(path: Path) -> NSBitmapImageRep:
    image = NSImage.alloc().initWithContentsOfFile_(str(path))
    if image is None:
        raise RuntimeError(f"failed to load image: {path}")
    rep = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation())
    if rep is None:
        raise RuntimeError(f"failed to decode image: {path}")
    return rep


def _white_components(path: Path) -> tuple[int, int, list[dict[str, int]]]:
    rep = _bitmap_rep(path)
    width = int(rep.pixelsWide())
    height = int(rep.pixelsHigh())
    mask = [[False] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            color = rep.colorAtX_y_(x, y)
            red = int(color.redComponent() * 255)
            green = int(color.greenComponent() * 255)
            blue = int(color.blueComponent() * 255)
            if red < 170 or green < 170 or blue < 170:
                continue
            if max(red, green, blue) - min(red, green, blue) > 35:
                continue
            mask[y][x] = True

    seen = [[False] * width for _ in range(height)]
    components: list[dict[str, int]] = []
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue
            queue = deque([(x, y)])
            seen[y][x] = True
            points: list[tuple[int, int]] = []
            while queue:
                cx, cy = queue.popleft()
                points.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if nx < 0 or nx >= width or ny < 0 or ny >= height:
                        continue
                    if seen[ny][nx] or not mask[ny][nx]:
                        continue
                    seen[ny][nx] = True
                    queue.append((nx, ny))
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            x0 = min(xs)
            x1 = max(xs)
            y0 = min(ys)
            y1 = max(ys)
            components.append(
                {
                    "area": len(points),
                    "x0": x0,
                    "x1": x1,
                    "y0": y0,
                    "y1": y1,
                    "width": x1 - x0 + 1,
                    "height": y1 - y0 + 1,
                    "cx2": x0 + x1,
                    "cy2": y0 + y1,
                }
            )
    components.sort(key=lambda item: item["area"], reverse=True)
    return width, height, components


def _wechat_icon_score(path: Path) -> int:
    width, height, components = _white_components(path)
    large = [
        item
        for item in components
        if item["area"] >= 170
        and 16 <= item["width"] <= min(36, width - 4)
        and 16 <= item["height"] <= min(32, height - 4)
    ]
    best_score = 0
    for first in large:
        for second in large:
            if first is second:
                continue
            if first["cx2"] >= second["cx2"]:
                continue
            if first["cx2"] >= int(width * 1.25):
                continue
            if second["cx2"] >= int(width * 1.45):
                continue
            if first["cy2"] >= second["cy2"]:
                continue
            overlap_x = min(first["x1"], second["x1"]) - max(first["x0"], second["x0"]) + 1
            overlap_y = min(first["y1"], second["y1"]) - max(first["y0"], second["y0"]) + 1
            if overlap_x < 6 or overlap_y < 6:
                continue
            dx2 = second["cx2"] - first["cx2"]
            dy2 = second["cy2"] - first["cy2"]
            if dx2 < 12 or dx2 > 44:
                continue
            if dy2 < 8 or dy2 > 28:
                continue
            score = first["area"] + second["area"]
            if second["x1"] < int(width * 0.45):
                score -= 60
            if second["x1"] > int(width * 0.80):
                score -= 40
            best_score = max(best_score, score)
    return best_score


def _choose_wechat_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    annotated: list[dict[str, Any]] = []
    try:
        for item in items:
            capture_path = _capture_region(item)
            score = _wechat_icon_score(capture_path)
            annotated.append({**item, "capture_path": capture_path, "wechat_icon_score": score})
        if not annotated:
            return None
        best = max(annotated, key=lambda item: item.get("wechat_icon_score", 0))
        if int(best.get("wechat_icon_score", 0) or 0) < 500:
            return None
        return best
    finally:
        for item in annotated:
            Path(item["capture_path"]).unlink(missing_ok=True)


def _digit_like_component_exists(path: Path) -> bool:
    rep = _bitmap_rep(path)
    width = int(rep.pixelsWide())
    height = int(rep.pixelsHigh())
    x_start = int(width * 0.58)
    x_end = width
    y_start = int(height * 0.12)
    y_end = int(height * 0.88)
    mask = [[False] * width for _ in range(height)]

    for y in range(y_start, y_end):
        for x in range(x_start, x_end):
            color = rep.colorAtX_y_(x, y)
            red = int(color.redComponent() * 255)
            green = int(color.greenComponent() * 255)
            blue = int(color.blueComponent() * 255)
            if red < 175 or green < 175 or blue < 175:
                continue
            if max(red, green, blue) - min(red, green, blue) > 35:
                continue
            mask[y][x] = True

    seen = [[False] * width for _ in range(height)]
    for y in range(y_start, y_end):
        for x in range(x_start, x_end):
            if not mask[y][x] or seen[y][x]:
                continue
            queue = deque([(x, y)])
            seen[y][x] = True
            points: list[tuple[int, int]] = []
            while queue:
                cx, cy = queue.popleft()
                points.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if nx < x_start or nx >= x_end or ny < y_start or ny >= y_end:
                        continue
                    if seen[ny][nx] or not mask[ny][nx]:
                        continue
                    seen[ny][nx] = True
                    queue.append((nx, ny))
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            component_width = max(xs) - min(xs) + 1
            component_height = max(ys) - min(ys) + 1
            area = len(points)
            if area < 18 or area > 240:
                continue
            if component_width < 2 or component_width > max(18, int(width * 0.24)):
                continue
            if component_height < 10 or component_height > int(height * 0.7):
                continue
            if max(xs) < int(width * 0.68):
                continue
            return True
    return False


def _read_badge_digits(path: Path) -> str:
    variants = _prepare_badge_ocr_variants(path)
    try:
        for variant in variants:
            digits = _extract_badge_digits(ocr_image(variant), min_x=0.0)
            if digits:
                return digits
        return _extract_badge_digits(ocr_image(path), min_x=0.5)
    except Exception:
        return ""
    finally:
        for variant in variants:
            variant.unlink(missing_ok=True)


def _extract_badge_digits(observations: list[dict[str, Any]], min_x: float = 0.0) -> str:
    try:
        if not isinstance(observations, list):
            return ""
    except Exception:
        return ""
    best = ""
    best_x = -1.0
    for obs in observations:
        text = str(obs.get("text") or "").strip()
        match = DIGIT_RE.search(text)
        if not match:
            continue
        bbox = obs.get("bbox") or {}
        x = float(bbox.get("x", 0.0) or 0.0)
        if x < 0.5:
            continue
        digits = match.group(0)
        if x > best_x:
            best = digits
            best_x = x
    return best


def _badge_roi_bounds(width: int, height: int) -> tuple[int, int, int, int]:
    x0 = max(0, min(width - 2, int(width * BADGE_ROI_X_RATIO)))
    y0 = max(0, min(height - 2, int(height * BADGE_ROI_Y_RATIO)))
    roi_h = max(2, int(height * BADGE_ROI_H_RATIO))
    y1 = min(height, y0 + roi_h)
    if y1 - y0 < 2:
        y0 = max(0, height - 2)
        y1 = height
    x1 = width
    return x0, y0, x1, y1


def _prepare_badge_ocr_variants(path: Path) -> list[Path]:
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return []

    source = Image.open(path).convert("RGB")
    width, height = source.size
    x0, y0, x1, y1 = _badge_roi_bounds(width, height)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return []

    roi = source.crop((x0, y0, x1, y1))
    upscaled = roi.resize(
        (max(2, roi.width * OCR_UPSCALE_FACTOR), max(2, roi.height * OCR_UPSCALE_FACTOR)),
        resample=Image.Resampling.BICUBIC,
    )
    grayscale = ImageOps.grayscale(upscaled)

    local_mean = grayscale.filter(ImageFilter.BoxBlur(2))
    src_px = grayscale.load()
    mean_px = local_mean.load()
    bw = Image.new("L", grayscale.size, 255)
    bw_px = bw.load()
    black_pixels = 0
    total_pixels = max(1, grayscale.width * grayscale.height)
    for y in range(grayscale.height):
        for x in range(grayscale.width):
            if int(src_px[x, y]) >= int(mean_px[x, y]) + ADAPTIVE_THRESHOLD_OFFSET:
                bw_px[x, y] = 0
                black_pixels += 1
            else:
                bw_px[x, y] = 255

    variants: list[Path] = []
    images = [bw, ImageOps.invert(bw)]
    black_ratio = black_pixels / float(total_pixels)
    if black_ratio < 0.01 or black_ratio > 0.60:
        images.insert(0, grayscale)

    for image in images:
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        handle.close()
        variant_path = Path(handle.name)
        image.save(variant_path, format="PNG")
        variants.append(variant_path)
    return variants


def unread_signal() -> str:
    item = _choose_wechat_item(_control_center_items())
    if not item:
        return ""

    path = _capture_region(item)
    capture_path = Path(path)
    try:
        if not _digit_like_component_exists(capture_path):
            return ""
        digits = _read_badge_digits(capture_path)
        if digits:
            return digits
        # OCR occasionally misses tiny menubar digits (e.g. "1") even when badge glyphs are present.
        # Fall back to a conservative actionable signal so claim flow still runs.
        return "1"
    finally:
        capture_path.unlink(missing_ok=True)


def check_unread_dot() -> bool:
    return bool(unread_signal())
