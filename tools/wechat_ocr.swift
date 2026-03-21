#!/usr/bin/env swift

import AppKit
import Foundation
import Vision

func fail(_ message: String, code: Int32 = 1) -> Never {
    fputs(message + "\n", stderr)
    exit(code)
}

guard CommandLine.arguments.count >= 2 else {
    fail("usage: wechat_ocr.swift <image>")
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

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])

do {
    try handler.perform([request])
    let results = (request.results ?? []).compactMap { observation -> [String: Any]? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let bb = observation.boundingBox
        return [
            "text": candidate.string,
            "confidence": candidate.confidence,
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
