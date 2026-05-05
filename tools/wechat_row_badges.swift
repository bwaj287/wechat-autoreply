#!/usr/bin/env swift

import AppKit
import Foundation

struct RowSpec: Codable {
    let index: Int
    let name: String
    let rowTop: Double
    let rowBottom: Double
    let nameLeft: Double?
}

struct RowResult: Codable {
    let index: Int
    let name: String
    let redPixelCount: Int
    let digitPixelCount: Int
    let numericBadge: Bool
    let unread: Bool
}

struct Component {
    let count: Int
    let minX: Int
    let maxX: Int
    let minY: Int
    let maxY: Int

    var width: Int { maxX - minX + 1 }
    var height: Int { maxY - minY + 1 }
    var centerX: Double { Double(minX + maxX) / 2.0 }
    var centerY: Double { Double(minY + maxY) / 2.0 }
    var fillRatio: Double { Double(count) / Double(width * height) }
}

func fail(_ message: String) -> Never {
    fputs(message + "\n", stderr)
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: wechat_row_badges.swift <image-path> <rows-json-path>")
}

let imagePath = CommandLine.arguments[1]
let rowsPath = CommandLine.arguments[2]

let rowsData: Data
do {
    rowsData = try Data(contentsOf: URL(fileURLWithPath: rowsPath))
} catch {
    fail("failed to read rows json: \(error)")
}

let rows: [RowSpec]
do {
    rows = try JSONDecoder().decode([RowSpec].self, from: rowsData)
} catch {
    fail("failed to decode rows json: \(error)")
}

guard let image = NSImage(contentsOfFile: imagePath) else {
    fail("failed to load image: \(imagePath)")
}

var rect = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fail("failed to create cgImage")
}

let bitmap = NSBitmapImageRep(cgImage: cgImage)
let width = bitmap.pixelsWide
let height = bitmap.pixelsHigh

func clamp(_ value: Int, min minValue: Int, max maxValue: Int) -> Int {
    Swift.max(minValue, Swift.min(maxValue, value))
}

func hueDegrees(_ r: Double, _ g: Double, _ b: Double) -> Double {
    let maxV = Swift.max(r, Swift.max(g, b))
    let minV = Swift.min(r, Swift.min(g, b))
    let delta = maxV - minV
    if delta <= 0.000_001 {
        return 0
    }
    var hue: Double
    if maxV == r {
        hue = ((g - b) / delta).truncatingRemainder(dividingBy: 6)
    } else if maxV == g {
        hue = ((b - r) / delta) + 2
    } else {
        hue = ((r - g) / delta) + 4
    }
    hue *= 60
    if hue < 0 {
        hue += 360
    }
    return hue
}

func isRedHue(_ hue: Double) -> Bool {
    // WeChat unread badges can drift towards orange-red under HDR / color profile changes.
    return hue <= 30 || hue >= 330
}

func isRedBadge(_ color: NSColor, relaxed: Bool = false) -> Bool {
    guard let c = color.usingColorSpace(.deviceRGB) else { return false }
    let r = Double(c.redComponent)
    let g = Double(c.greenComponent)
    let b = Double(c.blueComponent)
    let a = Double(c.alphaComponent)
    let maxV = Swift.max(r, Swift.max(g, b))
    let minV = Swift.min(r, Swift.min(g, b))
    let delta = maxV - minV
    let saturation = maxV > 0.000_001 ? (delta / maxV) : 0.0
    let hue = hueDegrees(r, g, b)
    let dominance = Swift.max(r - g, r - b)
    // Adaptive-ish strategy:
    // - strict pass: prefer cleaner reds;
    // - relaxed pass: tolerate dimmer/softer anti-aliased badges (HDR / scaling).
    if relaxed {
        return a > 0.18
            && maxV > 0.30
            && saturation > 0.14
            && dominance > 0.040
            && isRedHue(hue)
    }
    return a > 0.22
        && maxV > 0.36
        && saturation > 0.20
        && dominance > 0.055
        && isRedHue(hue)
}

func isWhiteDigitPixel(_ color: NSColor) -> Bool {
    guard let c = color.usingColorSpace(.deviceRGB) else { return false }
    let r = Double(c.redComponent)
    let g = Double(c.greenComponent)
    let b = Double(c.blueComponent)
    let a = Double(c.alphaComponent)
    let maxV = Swift.max(r, Swift.max(g, b))
    let minV = Swift.min(r, Swift.min(g, b))
    let delta = maxV - minV
    let saturation = maxV > 0.000_001 ? (delta / maxV) : 0.0
    let luminance = (r + g + b) / 3.0
    return a > 0.22
        && luminance > 0.72
        && saturation < 0.28
}

func isDarkDigitPixel(_ color: NSColor) -> Bool {
    guard let c = color.usingColorSpace(.deviceRGB) else { return false }
    let r = Double(c.redComponent)
    let g = Double(c.greenComponent)
    let b = Double(c.blueComponent)
    let a = Double(c.alphaComponent)
    let maxV = Swift.max(r, Swift.max(g, b))
    let minV = Swift.min(r, Swift.min(g, b))
    let delta = maxV - minV
    let saturation = maxV > 0.000_001 ? (delta / maxV) : 0.0
    let luminance = (r + g + b) / 3.0
    return a > 0.18
        && luminance < 0.34
        && saturation < 0.35
}

func transformedColor(_ color: NSColor, invert: Bool, grayscale: Bool = false) -> NSColor? {
    guard let c = color.usingColorSpace(.deviceRGB) else { return nil }
    var r = c.redComponent
    var g = c.greenComponent
    var b = c.blueComponent
    if grayscale {
        let y = 0.299 * r + 0.587 * g + 0.114 * b
        r = y
        g = y
        b = y
    }
    if !invert {
        return NSColor(
            deviceRed: r,
            green: g,
            blue: b,
            alpha: c.alphaComponent
        )
    }
    return NSColor(
        deviceRed: 1.0 - r,
        green: 1.0 - g,
        blue: 1.0 - b,
        alpha: c.alphaComponent
    )
}

func digitPixelCount(
    bitmap: NSBitmapImageRep,
    component: Component,
    rowTop: Int,
    rowBottom: Int,
    centerOnly: Bool = false
) -> Int {
    let rowHeight = Swift.max(1, rowBottom - rowTop)
    let padX = centerOnly ? Swift.max(1, Int(Double(component.width) * 0.20)) : Swift.max(1, Int(Double(component.width) * 0.10))
    let padY = centerOnly ? Swift.max(1, Int(Double(component.height) * 0.20)) : Swift.max(1, Int(Double(component.height) * 0.10))
    let x0 = clamp(component.minX + padX, min: 0, max: bitmap.pixelsWide - 1)
    let x1 = clamp(component.maxX - padX, min: x0 + 1, max: bitmap.pixelsWide)
    let y0Raw = component.minY + padY
    let y1Raw = component.maxY - padY
    let yMin = centerOnly ? Swift.max(rowTop, y0Raw) : y0Raw
    let yMax = centerOnly ? Swift.min(rowBottom, y1Raw) : y1Raw
    let y0 = clamp(yMin, min: 0, max: bitmap.pixelsHigh - 1)
    let y1 = clamp(yMax, min: y0 + 1, max: bitmap.pixelsHigh)
    if x1 <= x0 || y1 <= y0 {
        return 0
    }
    var white = 0
    for py in y0..<y1 {
        for px in x0..<x1 {
            guard let color = bitmap.colorAt(x: px, y: py) else { continue }
            if isWhiteDigitPixel(color) {
                white += 1
            }
        }
    }
    // Prevent unrelated large bright regions from passing.
    let maxAllowed = Swift.max(18, Int(Double(component.count) * 0.55), Int(Double(rowHeight) * 0.65))
    return Swift.min(white, maxAllowed)
}

func digitPixelRatioUpscaled(
    bitmap: NSBitmapImageRep,
    component: Component,
    rowTop: Int,
    rowBottom: Int,
    scale: Int,
    centerOnly: Bool = false,
    invert: Bool = false,
    grayscale: Bool = false
) -> Double {
    let factor = Swift.max(1, scale)
    let padX = centerOnly ? Swift.max(1, Int(Double(component.width) * 0.20)) : Swift.max(1, Int(Double(component.width) * 0.10))
    let padY = centerOnly ? Swift.max(1, Int(Double(component.height) * 0.20)) : Swift.max(1, Int(Double(component.height) * 0.10))
    let x0 = clamp(component.minX + padX, min: 0, max: bitmap.pixelsWide - 1)
    let x1 = clamp(component.maxX - padX, min: x0 + 1, max: bitmap.pixelsWide)
    let y0Raw = component.minY + padY
    let y1Raw = component.maxY - padY
    let yMin = centerOnly ? Swift.max(rowTop, y0Raw) : y0Raw
    let yMax = centerOnly ? Swift.min(rowBottom, y1Raw) : y1Raw
    let y0 = clamp(yMin, min: 0, max: bitmap.pixelsHigh - 1)
    let y1 = clamp(yMax, min: y0 + 1, max: bitmap.pixelsHigh)
    if x1 <= x0 || y1 <= y0 {
        return 0.0
    }
    let srcW = Swift.max(1, x1 - x0)
    let srcH = Swift.max(1, y1 - y0)
    let scaledW = srcW * factor
    let scaledH = srcH * factor
    if scaledW <= 0 || scaledH <= 0 {
        return 0.0
    }
    var hits = 0
    for sy in 0..<scaledH {
        let sourceY = Double(y0) + (Double(sy) + 0.5) / Double(factor)
        let py = clamp(Int(sourceY.rounded(.down)), min: y0, max: y1 - 1)
        for sx in 0..<scaledW {
            let sourceX = Double(x0) + (Double(sx) + 0.5) / Double(factor)
            let px = clamp(Int(sourceX.rounded(.down)), min: x0, max: x1 - 1)
            guard let raw = bitmap.colorAt(x: px, y: py), let color = transformedColor(raw, invert: invert, grayscale: grayscale) else {
                continue
            }
            if invert {
                if isDarkDigitPixel(color) {
                    hits += 1
                }
            } else if isWhiteDigitPixel(color) {
                hits += 1
            }
        }
    }
    let total = Swift.max(1, scaledW * scaledH)
    // Keep ratio bounded; caller can convert to equivalent pixels when needed.
    let ratio = Double(hits) / Double(total)
    let maxRatioCap = centerOnly ? 0.55 : 0.62
    return Swift.min(maxRatioCap, ratio)
}

func hasNumericBadgeEvidence(
    bitmap: NSBitmapImageRep,
    component: Component,
    rowTop: Int,
    rowBottom: Int,
    relaxed: Bool
) -> (Bool, Int) {
    func hasStrongDigitEvidence(_ digitPixels: Int) -> Bool {
        if digitPixels >= 10 {
            return true
        }
        let ratio = Double(digitPixels) / Double(Swift.max(component.count, 1))
        if digitPixels >= 8 {
            return ratio >= 0.045
        }
        if digitPixels >= 6 && component.count <= 80 {
            return ratio >= 0.11
        }
        return false
    }

    let full = digitPixelCount(bitmap: bitmap, component: component, rowTop: rowTop, rowBottom: rowBottom, centerOnly: false)
    let center = digitPixelCount(bitmap: bitmap, component: component, rowTop: rowTop, rowBottom: rowBottom, centerOnly: true)
    let minFull = relaxed ? Swift.max(4, Int(Double(component.count) * 0.014)) : Swift.max(5, Int(Double(component.count) * 0.018))
    let minCenter = relaxed ? Swift.max(2, Int(Double(component.count) * 0.006)) : Swift.max(3, Int(Double(component.count) * 0.008))
    if full >= minFull && center >= minCenter && hasStrongDigitEvidence(full) {
        return (true, full)
    }

    // Enhanced fallback for small/faint badges:
    // 1) upscale (including 5x / 10x) to amplify tiny digit strokes;
    // 2) grayscale and invert probes to rescue low-contrast anti-aliasing.
    let minSide = Swift.min(component.width, component.height)
    let scales: [Int]
    if minSide <= 8 {
        scales = [10, 5, 3]
    } else if minSide <= 14 {
        scales = [10, 5]
    } else if minSide <= 22 {
        scales = [5, 3]
    } else {
        scales = [5]
    }
    let probeModes: [(invert: Bool, grayscale: Bool)] = [
        (false, false),
        (false, true),
        (true, false),
        (true, true),
    ]
    var fullRatio = 0.0
    var centerRatio = 0.0
    for scale in scales {
        for mode in probeModes {
            fullRatio = Swift.max(
                fullRatio,
                digitPixelRatioUpscaled(
                    bitmap: bitmap,
                    component: component,
                    rowTop: rowTop,
                    rowBottom: rowBottom,
                    scale: scale,
                    centerOnly: false,
                    invert: mode.invert,
                    grayscale: mode.grayscale
                )
            )
            centerRatio = Swift.max(
                centerRatio,
                digitPixelRatioUpscaled(
                    bitmap: bitmap,
                    component: component,
                    rowTop: rowTop,
                    rowBottom: rowBottom,
                    scale: scale,
                    centerOnly: true,
                    invert: mode.invert,
                    grayscale: mode.grayscale
                )
            )
        }
    }
    let minFullRatio: Double
    let minCenterRatio: Double
    if minSide <= 8 {
        minFullRatio = relaxed ? 0.008 : 0.010
        minCenterRatio = relaxed ? 0.003 : 0.004
    } else if minSide <= 14 {
        minFullRatio = relaxed ? 0.010 : 0.013
        minCenterRatio = relaxed ? 0.0035 : 0.005
    } else {
        minFullRatio = relaxed ? 0.013 : 0.016
        minCenterRatio = relaxed ? 0.005 : 0.007
    }
    if fullRatio >= minFullRatio && centerRatio >= minCenterRatio && centerRatio >= fullRatio * 0.18 {
        let area = Swift.max(1, component.width * component.height)
        let equivalent = Int((fullRatio * Double(area)).rounded())
        let minEquivalent = relaxed ? 3 : 4
        if equivalent < minEquivalent {
            return (false, full)
        }
        if !hasStrongDigitEvidence(equivalent) {
            return (false, Swift.max(full, equivalent))
        }
        return (true, Swift.max(full, equivalent))
    }

    return (false, full)
}

func findComponents(
    bitmap: NSBitmapImageRep,
    x0: Int,
    x1: Int,
    yTop: Int,
    yBottom: Int,
    relaxed: Bool = false
) -> [Component] {
    let regionWidth = x1 - x0
    let regionHeight = yBottom - yTop
    if regionWidth <= 0 || regionHeight <= 0 {
        return []
    }

    func regionIndex(_ x: Int, _ y: Int) -> Int {
        y * regionWidth + x
    }

    var mask = Array(repeating: false, count: regionWidth * regionHeight)
    for py in yTop..<yBottom {
        for x in x0..<x1 {
            if let color = bitmap.colorAt(x: x, y: py), isRedBadge(color, relaxed: relaxed) {
                mask[regionIndex(x - x0, py - yTop)] = true
            }
        }
    }

    let directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    var visited = Array(repeating: false, count: mask.count)
    var components: [Component] = []

    for y in 0..<regionHeight {
        for x in 0..<regionWidth {
            let start = regionIndex(x, y)
            if visited[start] || !mask[start] {
                continue
            }

            visited[start] = true
            var stack = [(x, y)]
            var count = 0
            var minX = x
            var maxX = x
            var minY = y
            var maxY = y

            while let (cx, cy) = stack.popLast() {
                count += 1
                minX = Swift.min(minX, cx)
                maxX = Swift.max(maxX, cx)
                minY = Swift.min(minY, cy)
                maxY = Swift.max(maxY, cy)

                for (dx, dy) in directions {
                    let nx = cx + dx
                    let ny = cy + dy
                    if nx < 0 || ny < 0 || nx >= regionWidth || ny >= regionHeight {
                        continue
                    }
                    let next = regionIndex(nx, ny)
                    if visited[next] || !mask[next] {
                        continue
                    }
                    visited[next] = true
                    stack.append((nx, ny))
                }
            }

            components.append(
                Component(
                    count: count,
                    minX: minX + x0,
                    maxX: maxX + x0,
                    minY: minY + yTop,
                    maxY: maxY + yTop
                )
            )
        }
    }

    return components
}

var results: [RowResult] = []
for row in rows {
    let nameLeft = row.nameLeft ?? 0.15
    let rowTopNorm = row.rowTop
    let rowBottomNorm = row.rowBottom
    let rowSpanNorm = Swift.max(0.02, rowBottomNorm - rowTopNorm)
    let avatarGap = Swift.max(0.014, Swift.min(0.040, rowSpanNorm * 0.30))
    let estimatedAvatarRight = nameLeft - avatarGap
    let estimatedAvatarSize = Swift.max(0.040, Swift.min(0.095, rowSpanNorm * 1.10))
    let estimatedAvatarLeft = Swift.max(0.0, estimatedAvatarRight - estimatedAvatarSize)
    // Use a tight box anchored to the name start. In practice the unread badge
    // sits immediately to the left of the contact name, while scanning the full
    // avatar neighborhood lets red-heavy avatars merge into one giant component.
    let scanPadLeft = Swift.max(0.022, Swift.min(0.032, rowSpanNorm * 0.25))
    let scanPadRight = Swift.max(0.014, Swift.min(0.026, rowSpanNorm * 0.23))
    let minScanX = Swift.max(0.05, nameLeft - scanPadLeft)
    let maxScanX = Swift.min(0.24, nameLeft + scanPadRight)
    let x0 = clamp(Int(Double(width) * minScanX), min: 0, max: width - 1)
    let x1 = clamp(Int(Double(width) * maxScanX), min: x0 + 1, max: width)
    let topExpand = row.index == 0 ? 0.035 : 0.018
    let bottomExpand = row.index == 0 ? 0.020 : 0.014
    let yTop = clamp(Int((rowTopNorm - topExpand) * Double(height)), min: 0, max: height - 1)
    let yBottom = clamp(Int((rowBottomNorm + bottomExpand) * Double(height)), min: yTop + 1, max: height)
    let rowHeight = Swift.max(1, yBottom - yTop)
    // First visible row can include search/header bleed in OCR-derived row bounds,
    // which inflates rowHeight and makes badge-size thresholds too strict.
    // Clamp only for size-threshold derivation to keep center/position checks intact.
    let rowHeightForSize: Int
    if row.index == 0 {
        rowHeightForSize = Swift.min(rowHeight, 88)
    } else {
        rowHeightForSize = rowHeight
    }
    let smallBadgeMode = rowHeightForSize <= 90
    let minBadgeHeight = Swift.max(
        smallBadgeMode ? 6 : 8,
        Int(Double(rowHeightForSize) * (smallBadgeMode ? 0.13 : 0.18))
    )
    let maxBadgeHeight = Swift.max(minBadgeHeight + 3, Int(Double(rowHeight) * (smallBadgeMode ? 0.36 : 0.34)))
    let minBadgeWidth = Swift.max(6, Int(Double(minBadgeHeight) * (smallBadgeMode ? 0.82 : 1.0)))
    let maxBadgeWidth = Swift.max(maxBadgeHeight, Int(Double(rowHeightForSize) * (smallBadgeMode ? 0.98 : 0.92)))
    let minPixels = Swift.max(
        smallBadgeMode ? 8 : 14,
        Int(Double(minBadgeHeight * minBadgeHeight) * (smallBadgeMode ? 0.14 : 0.28))
    )
    let maxPixels = Swift.max(
        minPixels + 120,
        Int(Double(maxBadgeWidth * maxBadgeHeight) * (smallBadgeMode ? 1.28 : 1.20))
    )
    // Guardrail: unread numeric badge is a small corner glyph.
    // Large solid-red avatar regions should never pass as a badge.
    let maxLikelyBadgePixels = Swift.max(260, Int(Double(rowHeightForSize) * 3.4))
    // Badge must be spatially coupled with avatar corner, not the nav rail.
    let badgeXMin = Swift.max(0.0, estimatedAvatarLeft - Swift.max(0.006, rowSpanNorm * 0.08))
    let badgeXMax = Swift.min(0.30, estimatedAvatarRight + Swift.max(0.025, rowSpanNorm * 0.20))
    let badgeCenterMinX = Swift.max(0.004, estimatedAvatarRight - Swift.max(0.035, rowSpanNorm * 0.75))
    let badgeCenterMaxX = Swift.min(0.30, estimatedAvatarRight + Swift.max(0.022, rowSpanNorm * 0.45))
    let componentsStrict = findComponents(bitmap: bitmap, x0: x0, x1: x1, yTop: yTop, yBottom: yBottom, relaxed: false)
    var usingRelaxed = false
    var candidates = componentsStrict.filter { component in
        let centerXNorm = component.centerX / Double(width)
        let centerYNorm = (component.centerY - Double(yTop)) / Double(max(rowHeight, 1))
        let topYNorm = (Double(component.minY - yTop)) / Double(max(rowHeight, 1))
        let minXNorm = Double(component.minX) / Double(width)
        let maxXNorm = Double(component.maxX) / Double(width)
        let aspect = Double(component.width) / Double(max(component.height, 1))
        let minCenterX = Swift.max(0.008, Swift.max(nameLeft - (smallBadgeMode ? 0.16 : 0.14), badgeXMin + 0.015))
        let maxCenterX = Swift.min(0.33, nameLeft + (smallBadgeMode ? 0.10 : 0.08))
        let maxCenterY = row.index == 0 ? (smallBadgeMode ? 0.55 : 0.52) : (smallBadgeMode ? 0.46 : 0.42)
        let minCenterY = row.index == 0 ? 0.01 : 0.05
        return component.count >= minPixels
            && component.count <= maxPixels
            && component.count <= maxLikelyBadgePixels
            && component.width >= minBadgeWidth
            && component.width <= maxBadgeWidth
            && component.height >= minBadgeHeight
            && component.height <= maxBadgeHeight
            && component.fillRatio >= (smallBadgeMode ? 0.16 : 0.26)
            && component.fillRatio <= 0.86
            && aspect >= (smallBadgeMode ? 0.42 : 0.58)
            && aspect <= (smallBadgeMode ? 2.35 : 1.95)
            && centerXNorm >= minCenterX
            && centerXNorm <= maxCenterX
            && centerXNorm >= badgeCenterMinX
            && centerXNorm <= badgeCenterMaxX
            && maxXNorm >= badgeXMin
            && minXNorm <= badgeXMax
            && centerYNorm >= minCenterY
            && centerYNorm <= maxCenterY
            && topYNorm <= (row.index == 0 ? (smallBadgeMode ? 0.46 : 0.42) : (smallBadgeMode ? 0.42 : 0.34))
    }
    if candidates.isEmpty {
        usingRelaxed = true
        // Relaxed pass for HDR / scaling drift.
        let relaxedMinPixels = Swift.max(smallBadgeMode ? 6 : 10, Int(Double(minPixels) * (smallBadgeMode ? 0.55 : 0.65)))
        let relaxedMaxPixels = Swift.max(relaxedMinPixels + 120, Int(Double(maxPixels) * (smallBadgeMode ? 1.60 : 1.35)))
        let componentsRelaxed = findComponents(bitmap: bitmap, x0: x0, x1: x1, yTop: yTop, yBottom: yBottom, relaxed: true)
        candidates = componentsRelaxed.filter { component in
            let centerXNorm = component.centerX / Double(width)
            let centerYNorm = (component.centerY - Double(yTop)) / Double(max(rowHeight, 1))
            let topYNorm = (Double(component.minY - yTop)) / Double(max(rowHeight, 1))
            let minXNorm = Double(component.minX) / Double(width)
            let maxXNorm = Double(component.maxX) / Double(width)
            let aspect = Double(component.width) / Double(max(component.height, 1))
            let minCenterX = Swift.max(0.008, Swift.max(nameLeft - 0.15, badgeXMin + 0.012))
            let maxCenterX = Swift.min(0.35, nameLeft + (smallBadgeMode ? 0.13 : 0.10))
            let maxCenterY = row.index == 0 ? (smallBadgeMode ? 0.60 : 0.56) : (smallBadgeMode ? 0.56 : 0.48)
            let minCenterY = row.index == 0 ? 0.0 : 0.03
            return component.count >= relaxedMinPixels
                && component.count <= relaxedMaxPixels
                && component.count <= Int(Double(maxLikelyBadgePixels) * 1.12)
                && component.width >= max(smallBadgeMode ? 5 : 6, Int(Double(minBadgeWidth) * (smallBadgeMode ? 0.62 : 0.75)))
                && component.width <= maxBadgeWidth + max(6, Int(Double(maxBadgeWidth) * 0.35))
                && component.height >= max(smallBadgeMode ? 5 : 6, Int(Double(minBadgeHeight) * (smallBadgeMode ? 0.62 : 0.70)))
                && component.height <= maxBadgeHeight + max(6, Int(Double(maxBadgeHeight) * 0.35))
                && component.fillRatio >= (smallBadgeMode ? 0.12 : 0.22)
                && component.fillRatio <= 0.88
                && aspect >= (smallBadgeMode ? 0.36 : 0.50)
                && aspect <= (smallBadgeMode ? 2.55 : 2.10)
                && centerXNorm >= minCenterX
                && centerXNorm <= maxCenterX
                && centerXNorm >= badgeCenterMinX
                && centerXNorm <= badgeCenterMaxX
                && maxXNorm >= badgeXMin
                && minXNorm <= badgeXMax
                && centerYNorm >= minCenterY
                && centerYNorm <= maxCenterY
                && topYNorm <= (row.index == 0 ? (smallBadgeMode ? 0.56 : 0.50) : (smallBadgeMode ? 0.50 : 0.42))
        }
    }
    let ranked = candidates.sorted { lhs, rhs in
        if lhs.count == rhs.count {
            return lhs.fillRatio > rhs.fillRatio
        }
        return lhs.count > rhs.count
    }
    var best: Component? = nil
    var bestRedPixels = 0
    var bestDigitPixels = 0
    var bestNumeric = false
    for component in ranked {
        let (numeric, digitPixels) = hasNumericBadgeEvidence(
            bitmap: bitmap,
            component: component,
            rowTop: yTop,
            rowBottom: yBottom,
            relaxed: usingRelaxed
        )
        if numeric {
            best = component
            bestRedPixels = component.count
            bestDigitPixels = digitPixels
            bestNumeric = true
            break
        }
        if best == nil {
            best = component
            bestRedPixels = component.count
            bestDigitPixels = digitPixels
        }
    }
    let redPixels = bestRedPixels
    results.append(
        RowResult(
            index: row.index,
            name: row.name,
            redPixelCount: redPixels,
            digitPixelCount: bestDigitPixels,
            numericBadge: bestNumeric,
            unread: bestNumeric
        )
    )
}

do {
    let outData = try JSONEncoder().encode(["rows": results])
    guard let text = String(data: outData, encoding: .utf8) else {
        fail("failed to encode utf8 output")
    }
    print(text)
} catch {
    fail("failed to encode output: \(error)")
}
