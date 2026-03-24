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

func hasNumericBadgeEvidence(
    bitmap: NSBitmapImageRep,
    component: Component,
    rowTop: Int,
    rowBottom: Int,
    relaxed: Bool
) -> (Bool, Int) {
    let full = digitPixelCount(bitmap: bitmap, component: component, rowTop: rowTop, rowBottom: rowBottom, centerOnly: false)
    let center = digitPixelCount(bitmap: bitmap, component: component, rowTop: rowTop, rowBottom: rowBottom, centerOnly: true)
    let minFull = relaxed ? Swift.max(4, Int(Double(component.count) * 0.014)) : Swift.max(5, Int(Double(component.count) * 0.018))
    let minCenter = relaxed ? Swift.max(2, Int(Double(component.count) * 0.006)) : Swift.max(3, Int(Double(component.count) * 0.008))
    let ok = full >= minFull && center >= minCenter
    return (ok, full)
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
    // Scan the whole avatar badge neighborhood (left+right of avatar top edge).
    // Some WeChat UI variants render unread badges near avatar top-left instead
    // of top-right, especially for the first visible row.
    let rowTopNorm = row.rowTop
    let rowBottomNorm = row.rowBottom
    let rowSpanNorm = Swift.max(0.02, rowBottomNorm - rowTopNorm)
    let scanPadLeft = Swift.min(0.20, Swift.max(0.11, rowSpanNorm * 1.9))
    let scanPadRight = Swift.min(0.11, Swift.max(0.040, rowSpanNorm * 0.85))
    let minScanX = Swift.max(0.008, nameLeft - scanPadLeft)
    let maxScanX = Swift.min(0.32, nameLeft + scanPadRight)
    let x0 = clamp(Int(Double(width) * minScanX), min: 0, max: width - 1)
    let x1 = clamp(Int(Double(width) * maxScanX), min: x0 + 1, max: width)
    let topExpand = row.index == 0 ? 0.035 : 0.018
    let bottomExpand = row.index == 0 ? 0.020 : 0.014
    let yTop = clamp(Int((rowTopNorm - topExpand) * Double(height)), min: 0, max: height - 1)
    let yBottom = clamp(Int((rowBottomNorm + bottomExpand) * Double(height)), min: yTop + 1, max: height)
    let rowHeight = Swift.max(1, yBottom - yTop)
    let minBadgeHeight = Swift.max(8, Int(Double(rowHeight) * 0.18))
    let maxBadgeHeight = Swift.max(minBadgeHeight + 3, Int(Double(rowHeight) * 0.40))
    let minBadgeWidth = minBadgeHeight
    let maxBadgeWidth = Swift.max(maxBadgeHeight, Int(Double(rowHeight) * 1.05))
    let minPixels = Swift.max(14, Int(Double(minBadgeHeight * minBadgeHeight) * 0.28))
    let maxPixels = Swift.max(minPixels + 120, Int(Double(maxBadgeWidth * maxBadgeHeight) * 1.40))
    let componentsStrict = findComponents(bitmap: bitmap, x0: x0, x1: x1, yTop: yTop, yBottom: yBottom, relaxed: false)
    var usingRelaxed = false
    var candidates = componentsStrict.filter { component in
        let centerXNorm = component.centerX / Double(width)
        let centerYNorm = (component.centerY - Double(yTop)) / Double(max(rowHeight, 1))
        let topYNorm = (Double(component.minY - yTop)) / Double(max(rowHeight, 1))
        let aspect = Double(component.width) / Double(max(component.height, 1))
        let minCenterX = Swift.max(0.01, nameLeft - 0.16)
        let maxCenterX = Swift.min(0.31, nameLeft + 0.08)
        let maxCenterY = row.index == 0 ? 0.72 : 0.46
        let minCenterY = row.index == 0 ? 0.01 : 0.05
        return component.count >= minPixels
            && component.count <= maxPixels
            && component.width >= minBadgeWidth
            && component.width <= maxBadgeWidth
            && component.height >= minBadgeHeight
            && component.height <= maxBadgeHeight
            && component.fillRatio >= 0.26
            && aspect >= 0.58
            && aspect <= 1.95
            && centerXNorm >= minCenterX
            && centerXNorm <= maxCenterX
            && centerYNorm >= minCenterY
            && centerYNorm <= maxCenterY
            && topYNorm <= (row.index == 0 ? 0.50 : 0.38)
    }
    if candidates.isEmpty {
        usingRelaxed = true
        // Relaxed pass for HDR / scaling drift.
        let relaxedMinPixels = Swift.max(10, Int(Double(minPixels) * 0.65))
        let relaxedMaxPixels = Swift.max(relaxedMinPixels + 120, Int(Double(maxPixels) * 1.35))
        let componentsRelaxed = findComponents(bitmap: bitmap, x0: x0, x1: x1, yTop: yTop, yBottom: yBottom, relaxed: true)
        candidates = componentsRelaxed.filter { component in
            let centerXNorm = component.centerX / Double(width)
            let centerYNorm = (component.centerY - Double(yTop)) / Double(max(rowHeight, 1))
            let topYNorm = (Double(component.minY - yTop)) / Double(max(rowHeight, 1))
            let aspect = Double(component.width) / Double(max(component.height, 1))
            let minCenterX = Swift.max(0.008, nameLeft - 0.17)
            let maxCenterX = Swift.min(0.33, nameLeft + 0.10)
            let maxCenterY = row.index == 0 ? 0.76 : 0.54
            let minCenterY = row.index == 0 ? 0.0 : 0.03
            return component.count >= relaxedMinPixels
                && component.count <= relaxedMaxPixels
                && component.width >= max(6, Int(Double(minBadgeWidth) * 0.75))
                && component.width <= maxBadgeWidth + max(6, Int(Double(maxBadgeWidth) * 0.35))
                && component.height >= max(6, Int(Double(minBadgeHeight) * 0.70))
                && component.height <= maxBadgeHeight + max(6, Int(Double(maxBadgeHeight) * 0.35))
                && component.fillRatio >= 0.22
                && aspect >= 0.50
                && aspect <= 2.10
                && centerXNorm >= minCenterX
                && centerXNorm <= maxCenterX
                && centerYNorm >= minCenterY
                && centerYNorm <= maxCenterY
                && topYNorm <= (row.index == 0 ? 0.56 : 0.46)
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
