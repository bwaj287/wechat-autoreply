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

func isRedBadge(_ color: NSColor) -> Bool {
    guard let c = color.usingColorSpace(.deviceRGB) else { return false }
    let r = Double(c.redComponent)
    let g = Double(c.greenComponent)
    let b = Double(c.blueComponent)
    let a = Double(c.alphaComponent)
    return a > 0.25 && r > 0.35 && g < 0.60 && b < 0.60 && (r - g) > 0.06 && (r - b) > 0.06
}

func findComponents(
    bitmap: NSBitmapImageRep,
    x0: Int,
    x1: Int,
    yTop: Int,
    yBottom: Int
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
            if let color = bitmap.colorAt(x: x, y: py), isRedBadge(color) {
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
    // Only scan the avatar top-right badge lane to avoid false positives
    // from colorful avatars or preview snippets.
    let minScanX = Swift.max(0.03, nameLeft - 0.08)
    let maxScanX = Swift.min(0.20, nameLeft - 0.005)
    let x0 = clamp(Int(Double(width) * minScanX), min: 0, max: width - 1)
    let x1 = clamp(Int(Double(width) * maxScanX), min: x0 + 1, max: width)
    let yTop = clamp(Int(row.rowTop * Double(height)), min: 0, max: height - 1)
    let yBottom = clamp(Int(row.rowBottom * Double(height)), min: yTop + 1, max: height)
    let rowHeight = Swift.max(1, yBottom - yTop)
    let minBadgeHeight = Swift.max(6, Int(Double(rowHeight) * 0.14))
    let maxBadgeHeight = Swift.max(minBadgeHeight + 3, Int(Double(rowHeight) * 0.38))
    let minBadgeWidth = minBadgeHeight
    let maxBadgeWidth = Swift.max(maxBadgeHeight, Int(Double(rowHeight) * 1.05))
    let minPixels = Swift.max(14, Int(Double(minBadgeHeight * minBadgeHeight) * 0.28))
    let maxPixels = Swift.max(minPixels + 120, Int(Double(maxBadgeWidth * maxBadgeHeight) * 1.40))
    let components = findComponents(bitmap: bitmap, x0: x0, x1: x1, yTop: yTop, yBottom: yBottom)
    let candidates = components.filter { component in
        let centerXNorm = component.centerX / Double(width)
        let centerYNorm = (component.centerY - Double(yTop)) / Double(max(rowHeight, 1))
        let aspect = Double(component.width) / Double(max(component.height, 1))
        let minCenterX = Swift.max(0.07, nameLeft - 0.06)
        let maxCenterX = Swift.min(0.28, nameLeft + 0.05)
        return component.count >= minPixels
            && component.count <= maxPixels
            && component.width >= minBadgeWidth
            && component.width <= maxBadgeWidth
            && component.height >= minBadgeHeight
            && component.height <= maxBadgeHeight
            && component.fillRatio >= 0.20
            && aspect >= 0.45
            && aspect <= 2.30
            && centerXNorm >= minCenterX
            && centerXNorm <= maxCenterX
            && centerYNorm >= 0.08
            && centerYNorm <= 0.52
    }
    let best = candidates.max(by: { lhs, rhs in
        if lhs.count == rhs.count {
            return lhs.fillRatio < rhs.fillRatio
        }
        return lhs.count < rhs.count
    })
    let redPixels = best?.count ?? 0

    results.append(
        RowResult(
            index: row.index,
            name: row.name,
            redPixelCount: redPixels,
            unread: best != nil
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
