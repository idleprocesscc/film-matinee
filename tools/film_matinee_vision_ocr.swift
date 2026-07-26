import AppKit
import Foundation
import Vision

struct OCRRow: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct OCRImageResult: Codable {
    let path: String
    let rows: [OCRRow]
    let error: String?
}

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: film-matinee-vision-ocr IMAGE...\n".utf8))
    exit(2)
}

var output: [OCRImageResult] = []
output.reserveCapacity(CommandLine.arguments.count - 1)

for path in CommandLine.arguments.dropFirst() {
    autoreleasepool {
        guard let image = NSImage(contentsOfFile: path),
              let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            output.append(OCRImageResult(path: path, rows: [], error: "could not decode image"))
            return
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
        request.minimumTextHeight = 0.018

        do {
            try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
            let rows = (request.results ?? []).compactMap { observation -> OCRRow? in
                guard let candidate = observation.topCandidates(1).first else { return nil }
                let box = observation.boundingBox
                return OCRRow(
                    text: candidate.string,
                    confidence: candidate.confidence,
                    x: box.origin.x,
                    y: box.origin.y,
                    width: box.size.width,
                    height: box.size.height
                )
            }
            output.append(OCRImageResult(path: path, rows: rows, error: nil))
        } catch {
            output.append(OCRImageResult(path: path, rows: [], error: String(describing: error)))
        }
    }
}

do {
    let data = try JSONEncoder().encode(output)
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("could not encode OCR result: \(error)\n".utf8))
    exit(1)
}
