#!/usr/bin/env swift

import AppKit
import Foundation

struct ItemSpec: Codable {
    let index: Int
    let x: Double
    let y: Double
    let w: Double
    let h: Double
}

struct ItemRole: Codable {
    let index: Int
    let role: String
    let greenPixels: Int
    let grayPixels: Int
}

func fail(_ message: String) -> Never {
    fputs(message + "\n", stderr)
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: wechat_bubble_roles.swift <image-path> <items-json-path>")
}

let imagePath = CommandLine.arguments[1]
let itemsPath = CommandLine.arguments[2]

let itemsData: Data
do {
    itemsData = try Data(contentsOf: URL(fileURLWithPath: itemsPath))
} catch {
    fail("failed to read items json: \(error)")
}

let items: [ItemSpec]
do {
    items = try JSONDecoder().decode([ItemSpec].self, from: itemsData)
} catch {
    fail("failed to decode items json: \(error)")
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

func bubbleRect(for item: ItemSpec) -> (x0: Int, x1: Int, y0: Int, y1: Int) {
    let px = Int(item.x * Double(width))
    let py = Int(item.y * Double(height))
    let pw = Int(item.w * Double(width))
    let ph = Int(item.h * Double(height))
    let minPadX = Swift.max(8, Int(Double(width) * 0.012))
    let minPadY = Swift.max(6, Int(Double(height) * 0.010))
    let padX = Swift.max(Int(Double(pw) * 0.32), minPadX)
    let padY = Swift.max(Int(Double(ph) * 0.60), minPadY)
    let x0 = clamp(px - padX, min: 0, max: width - 1)
    let x1 = clamp(px + pw + padX, min: x0 + 1, max: width)
    let y0 = clamp(py - padY, min: 0, max: height - 1)
    let y1 = clamp(py + ph + padY, min: y0 + 1, max: height)
    return (x0, x1, y0, y1)
}

func isGreenBubble(_ color: NSColor) -> Bool {
    guard let c = color.usingColorSpace(.deviceRGB) else { return false }
    let r = Double(c.redComponent)
    let g = Double(c.greenComponent)
    let b = Double(c.blueComponent)
    let a = Double(c.alphaComponent)
    return a > 0.35 && g > 0.30 && g > r + 0.06 && g > b + 0.03
}

func isGrayBubble(_ color: NSColor) -> Bool {
    guard let c = color.usingColorSpace(.deviceRGB) else { return false }
    let r = Double(c.redComponent)
    let g = Double(c.greenComponent)
    let b = Double(c.blueComponent)
    let a = Double(c.alphaComponent)
    let brightness = (r + g + b) / 3.0
    return a > 0.35
        && abs(r - g) < 0.08
        && abs(g - b) < 0.08
        && brightness >= 0.12
        && brightness <= 0.45
}

var roles: [ItemRole] = []
for item in items {
    let region = bubbleRect(for: item)
    let area = Swift.max(1, (region.x1 - region.x0) * (region.y1 - region.y0))
    let itemWidth = Swift.max(1, Int(item.w * Double(width)))
    let itemHeight = Swift.max(1, Int(item.h * Double(height)))
    let itemArea = Swift.max(1, itemWidth * itemHeight)
    var greenPixels = 0
    var grayPixels = 0

    for y in region.y0..<region.y1 {
        for x in region.x0..<region.x1 {
            guard let color = bitmap.colorAt(x: x, y: y) else { continue }
            if isGreenBubble(color) {
                greenPixels += 1
            } else if isGrayBubble(color) {
                grayPixels += 1
            }
        }
    }

    let greenRatio = Double(greenPixels) / Double(area)
    let grayRatio = Double(grayPixels) / Double(area)
    let minGreenPixels = Swift.max(24, Int(Double(itemArea) * 0.10), Int(Double(area) * 0.018))
    let minGrayPixels = Swift.max(42, Int(Double(itemArea) * 0.16), Int(Double(area) * 0.030))
    let role: String
    if greenPixels >= minGreenPixels && greenRatio >= 0.045 {
        role = "outbound"
    } else if grayPixels >= minGrayPixels && grayRatio >= 0.075 {
        role = "inbound"
    } else {
        role = "unknown"
    }

    roles.append(ItemRole(index: item.index, role: role, greenPixels: greenPixels, grayPixels: grayPixels))
}

do {
    let outData = try JSONEncoder().encode(["items": roles])
    guard let text = String(data: outData, encoding: .utf8) else {
        fail("failed to encode utf8 output")
    }
    print(text)
} catch {
    fail("failed to encode output: \(error)")
}
