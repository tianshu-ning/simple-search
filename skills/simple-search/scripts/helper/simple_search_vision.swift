// simple-search 的本机文字识别、图片分类和 PDF 文本提取组件。
// Protocol (serve mode): one JSON request per stdin line, one JSON reply per stdout line.
//   {"path": "...", "mode": "image" | "pdf", "labels": true, "ocr": true}
//   -> {"text": "...", "labels": ["cat", ...]}  or  {"error": "..."}
// Everything runs locally through Apple's Vision and PDFKit frameworks.
import AppKit
import Foundation
import ImageIO
import PDFKit
import Vision

struct Request: Decodable {
    let path: String
    let mode: String
    let labels: Bool?
    let ocr: Bool?
}

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: []),
       let line = String(data: data, encoding: .utf8) {
        FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
    } else {
        FileHandle.standardOutput.write("{\"error\":\"encode\"}\n".data(using: .utf8)!)
    }
}

func loadImage(_ url: URL) -> CGImage? {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: 4096,
    ]
    return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        ?? CGImageSourceCreateImageAtIndex(source, 0, nil)
}

func recognize(_ image: CGImage) -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    var wanted = ["zh-Hans", "zh-Hant", "en-US", "ja-JP", "ko-KR"]
    if let supported = try? request.supportedRecognitionLanguages() {
        wanted = wanted.filter { supported.contains($0) }
    }
    if !wanted.isEmpty { request.recognitionLanguages = wanted }
    if #available(macOS 13.0, *) { request.automaticallyDetectsLanguage = true }
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([request]) } catch { return "" }
    let observations = request.results ?? []
    // Keep reading order: top to bottom, then left to right.
    let lines = observations.sorted {
        let a = $0.boundingBox, b = $1.boundingBox
        if abs(a.midY - b.midY) > min(a.height, b.height) / 2 { return a.midY > b.midY }
        return a.minX < b.minX
    }.compactMap { $0.topCandidates(1).first?.string }
    return lines.joined(separator: "\n")
}

func classify(_ image: CGImage) -> [String] {
    let request = VNClassifyImageRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([request]) } catch { return [] }
    let results = request.results ?? []
    let good = results.filter { $0.confidence > 0.35 }
        .sorted { $0.confidence > $1.confidence }
        .prefix(10)
        .map { $0.identifier }
    return Array(good)
}

func renderPage(_ page: PDFPage) -> CGImage? {
    let bounds = page.bounds(for: .mediaBox)
    let scale: CGFloat = min(2.5, 2400 / max(bounds.width, bounds.height, 1))
    let size = NSSize(width: bounds.width * scale, height: bounds.height * scale)
    let image = page.thumbnail(of: size, for: .mediaBox)
    var rect = CGRect(origin: .zero, size: image.size)
    return image.cgImage(forProposedRect: &rect, context: nil, hints: nil)
}

func handle(_ req: Request) -> [String: Any] {
    let url = URL(fileURLWithPath: req.path)
    if req.mode == "pdf" {
        guard let doc = PDFDocument(url: url) else { return ["error": "pdf"] }
        var text = doc.string ?? ""
        let pages = doc.pageCount
        let sparse = text.trimmingCharacters(in: .whitespacesAndNewlines).count < max(40, pages * 20)
        if sparse && (req.ocr ?? true) {
            var parts: [String] = []
            for i in 0..<min(pages, 30) {
                autoreleasepool {
                    if let page = doc.page(at: i), let cg = renderPage(page) { parts.append(recognize(cg)) }
                }
            }
            text = (text + "\n" + parts.joined(separator: "\n")).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return ["text": text]
    }
    guard let image = loadImage(url) else { return ["error": "image"] }
    var out: [String: Any] = ["text": (req.ocr ?? true) ? recognize(image) : ""]
    if req.labels ?? false { out["labels"] = classify(image) }
    return out
}

let args = CommandLine.arguments
if args.count >= 3 && args[1] != "serve" {
    // 单次处理：simple-search-vision image|pdf <路径>
    let r = Request(path: args[2], mode: args[1], labels: true, ocr: true)
    emit(handle(r))
    exit(0)
}
while let line = readLine(strippingNewline: true) {
    if line.isEmpty { continue }
    autoreleasepool {
        guard let data = line.data(using: .utf8), let req = try? JSONDecoder().decode(Request.self, from: data) else {
            emit(["error": "request"]); return
        }
        emit(handle(req))
    }
}
