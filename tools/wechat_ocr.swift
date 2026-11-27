#!/usr/bin/env swift

import AppKit
import Foundation
import Vision

func fail(_ message: String, code: Int32 = 1) -> Never {
    fputs(message + "\n", stderr)
    exit(code)
}

guard CommandLine.arguments.count >= 2 else {
    fail("usage: wechat_ocr.swift <image> [custom-words-json]")
}

let imagePath = CommandLine.arguments[1]
let imageUrl = URL(fileURLWithPath: imagePath)
guard let image = NSImage(contentsOf: imageUrl) else {
    fail("failed to load image: \(imagePath)")
}

var rect = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fail("failed to build cgImage: \(imagePath)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
if CommandLine.arguments.count > 2 {
    guard let data = CommandLine.arguments[2].data(using: .utf8),
          let words = try? JSONDecoder().decode([String].self, from: data) else {
        fail("custom words must be a JSON array of strings")
    }
    request.customWords = words
}

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])

do {
    try handler.perform([request])
    let results = (request.results ?? []).compactMap { observation -> [String: Any]? in
        let candidates = observation.topCandidates(3)
        guard let candidate = candidates.first else { return nil }
        let bb = observation.boundingBox
        return [
            "text": candidate.string,
            "confidence": candidate.confidence,
            "candidates": candidates.map { ["text": $0.string, "confidence": $0.confidence] },
            "bbox": [
                "x": bb.origin.x,
                "y": bb.origin.y,
                "w": bb.size.width,
                "h": bb.size.height,
            ],
        ]
    }
    let payload: [String: Any] = [
        "image": imagePath,
        "results": results,
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
} catch {
    fail("ocr failed: \(error)")
}
