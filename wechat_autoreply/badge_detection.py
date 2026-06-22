from __future__ import annotations

from typing import Any

from PIL import Image, ImageOps


def _is_red_badge_pixel(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    max_v = max(r, g, b)
    min_v = min(r, g, b)
    if max_v <= 92:
        return False
    saturation = (max_v - min_v) / max_v if max_v else 0.0
    return saturation > 0.20 and r > g + 28 and r > b + 28


def _is_white_digit_pixel(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    max_v = max(r, g, b)
    min_v = min(r, g, b)
    luminance = (r + g + b) / 3.0
    saturation = (max_v - min_v) / max_v if max_v else 0.0
    return luminance > 205 and saturation < 0.34


def _count_light_pixels(image: Image.Image, threshold: int) -> int:
    pixels = image.load()
    width, height = image.size
    return sum(
        1
        for y in range(height)
        for x in range(width)
        if int(pixels[x, y]) >= threshold
    )


def _count_dark_pixels(image: Image.Image, threshold: int) -> int:
    pixels = image.load()
    width, height = image.size
    return sum(
        1
        for y in range(height)
        for x in range(width)
        if int(pixels[x, y]) <= threshold
    )


def _enhanced_digit_equivalent(crop: Image.Image) -> int:
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(crop), cutoff=2)
    inverted = ImageOps.invert(grayscale)
    scale = 8
    gray_up = grayscale.resize(
        (grayscale.width * scale, grayscale.height * scale),
        Image.Resampling.BICUBIC,
    )
    inverted_up = inverted.resize(
        (inverted.width * scale, inverted.height * scale),
        Image.Resampling.BICUBIC,
    )
    light_equivalent = _count_light_pixels(gray_up, 220) // (scale * scale)
    dark_equivalent = _count_dark_pixels(inverted_up, 35) // (scale * scale)
    return min(light_equivalent, dark_equivalent)


def _connected_components(mask: list[list[bool]]) -> list[dict[str, int]]:
    if not mask or not mask[0]:
        return []
    height = len(mask)
    width = len(mask[0])
    visited = [[False] * width for _ in range(height)]
    components: list[dict[str, int]] = []
    for y in range(height):
        for x in range(width):
            if visited[y][x] or not mask[y][x]:
                continue
            stack = [(x, y)]
            visited[y][x] = True
            count = 0
            min_x = max_x = x
            min_y = max_y = y
            while stack:
                cx, cy = stack.pop()
                count += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx = cx + dx
                    ny = cy + dy
                    if 0 <= nx < width and 0 <= ny < height and not visited[ny][nx] and mask[ny][nx]:
                        visited[ny][nx] = True
                        stack.append((nx, ny))
            components.append(
                {
                    "count": count,
                    "minX": min_x,
                    "maxX": max_x,
                    "minY": min_y,
                    "maxY": max_y,
                    "width": max_x - min_x + 1,
                    "height": max_y - min_y + 1,
                }
            )
    return components


def _embedded_numeric_badge_detection(
    image: Image.Image,
    *,
    x0: int,
    x1: int,
    y0: int,
    y1: int,
    red_components: list[dict[str, int]],
    name_x: int,
) -> dict[str, int] | None:
    oversized = [
        component
        for component in red_components
        if 80 <= int(component["count"]) <= 1200
        and 21 <= int(component["width"]) <= 48
        and 15 <= int(component["height"]) <= 48
    ]
    if not oversized:
        return None

    white_mask = [
        [_is_white_digit_pixel(image.getpixel((px, py))) for px in range(x0, x1)]
        for py in range(y0, y1)
    ]
    for digit in sorted(_connected_components(white_mask), key=lambda item: int(item["count"]), reverse=True):
        digit_count = int(digit["count"])
        digit_width = int(digit["width"])
        digit_height = int(digit["height"])
        if digit_count < 6 or digit_count > 40:
            continue
        if digit_width < 1 or digit_width > 10 or digit_height < 3 or digit_height > 18:
            continue

        center_x = (int(digit["minX"]) + int(digit["maxX"])) / 2.0
        center_y = (int(digit["minY"]) + int(digit["maxY"])) / 2.0
        absolute_center_x = x0 + center_x
        if absolute_center_x > name_x + 4:
            continue

        host = None
        for component in oversized:
            relative_x = (center_x - int(component["minX"])) / max(1, int(component["width"]) - 1)
            relative_y = (center_y - int(component["minY"])) / max(1, int(component["height"]) - 1)
            if (
                int(component["minX"]) <= center_x <= int(component["maxX"])
                and int(component["minY"]) <= center_y <= int(component["maxY"])
                and relative_x >= 0.48
                and relative_y <= 0.60
            ):
                host = component
                break
        if host is None:
            continue

        cx = int(round(absolute_center_x))
        cy = int(round(y0 + center_y))
        window_x0 = max(x0, cx - 8)
        window_x1 = min(x1, cx + 9)
        window_y0 = max(y0, cy - 8)
        window_y1 = min(y1, cy + 9)
        area = max(1, (window_x1 - window_x0) * (window_y1 - window_y0))
        red_count = 0
        left_red = 0
        right_red = 0
        left_area = 0
        right_area = 0
        for py in range(window_y0, window_y1):
            for px in range(window_x0, window_x1):
                is_red = _is_red_badge_pixel(image.getpixel((px, py)))
                red_count += int(is_red)
                if px < cx:
                    left_area += 1
                    left_red += int(is_red)
                elif px > cx:
                    right_area += 1
                    right_red += int(is_red)
        red_ratio = red_count / area
        if red_ratio < 0.40 or red_ratio > 0.88:
            continue
        if left_red / max(1, left_area) < 0.25 or right_red / max(1, right_area) < 0.25:
            continue
        return {"redPixelCount": red_count, "digitPixelCount": digit_count}
    return None


def fallback_row_badge_detection(
    image: Image.Image,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    width, height = image.size
    row_top = float(row.get("rowTop", 0.0) or 0.0)
    row_bottom = float(row.get("rowBottom", 0.0) or 0.0)
    name_left = float(row.get("nameLeft", 0.15) or 0.15)
    row_span = max(0.02, row_bottom - row_top)
    scan_pad_left = max(0.018, min(0.022, row_span * 0.16))
    scan_pad_right = max(0.018, min(0.024, row_span * 0.18))
    x0 = max(0, int(round((name_left - scan_pad_left) * width)))
    x1 = min(width, int(round((name_left + scan_pad_right) * width)))
    y0 = max(0, int(round(max(0.0, row_top - 0.012) * height)))
    y1 = min(height, int(round(min(1.0, row_top + max(0.085, min(0.135, row_span * 0.78))) * height)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None

    region_h = max(1, y1 - y0)
    mask = [
        [_is_red_badge_pixel(image.getpixel((px, py))) for px in range(x0, x1)]
        for py in range(y0, y1)
    ]
    components = _connected_components(mask)
    best: dict[str, int] | None = None
    for component in components:
        comp_w = int(component["width"])
        comp_h = int(component["height"])
        count = int(component["count"])
        if count < 20 or count > 320:
            continue
        if comp_w < 9 or comp_w > 20 or comp_h < 9 or comp_h > 28:
            continue
        fill_ratio = count / max(1, comp_w * comp_h)
        aspect = comp_w / max(1, comp_h)
        center_y = ((component["minY"] + component["maxY"]) / 2.0) / region_h
        if fill_ratio < 0.16 or fill_ratio > 0.86:
            continue
        if aspect < 0.45 or aspect > 1.70:
            continue
        if center_y < 0.08 or center_y > 0.92:
            continue
        if best is None or count > int(best["count"]):
            best = component

    if best is None:
        embedded = _embedded_numeric_badge_detection(
            image,
            x0=x0,
            x1=x1,
            y0=y0,
            y1=y1,
            red_components=components,
            name_x=int(round(name_left * width)),
        )
        if embedded is None:
            return None
        return {**embedded, "numericBadge": True, "unread": True}

    pad_x = max(1, int(best["width"] * 0.16))
    pad_y = max(1, int(best["height"] * 0.16))
    ix0 = x0 + int(best["minX"]) + pad_x
    ix1 = x0 + int(best["maxX"]) - pad_x + 1
    iy0 = y0 + int(best["minY"]) + pad_y
    iy1 = y0 + int(best["maxY"]) - pad_y + 1
    if ix1 <= ix0 or iy1 <= iy0:
        return None

    digit_pixels = sum(
        1
        for py in range(iy0, iy1)
        for px in range(ix0, ix1)
        if _is_white_digit_pixel(image.getpixel((px, py)))
    )
    if digit_pixels < 10:
        crop = image.crop((ix0, iy0, ix1, iy1))
        enhanced_equivalent = _enhanced_digit_equivalent(crop)
        if digit_pixels >= 8 and 3 <= enhanced_equivalent <= 10:
            digit_pixels = max(digit_pixels, enhanced_equivalent)
        else:
            return None
    return {
        "redPixelCount": int(best["count"]),
        "digitPixelCount": int(digit_pixels),
        "numericBadge": True,
        "unread": True,
    }
