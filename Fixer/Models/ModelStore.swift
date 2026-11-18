import CoreML
import Foundation
import ZIPFoundation

/// The Core ML models Fixer downloads on first use. They're kept in Application Support (excluded from
/// iCloud backup) and can be deleted from the Models screen. The brain (MLX) downloads separately in Brain.swift.
enum CoreModel: String, CaseIterable, Identifiable {
    case remover, upscaler, generator
    var id: String { rawValue }

    var title: String {
        switch self {
        case .remover: "Remove tool"
        case .upscaler: "Upscaler"
        case .generator: "Image model"
        }
    }

    var approxMB: Int {
        switch self {
        case .remover: 196
        case .upscaler: 62
        case .generator: 960
        }
    }
}

@MainActor
final class ModelStore: ObservableObject {
    static let shared = ModelStore()

    @Published private(set) var progress: [CoreModel: Double] = [:]
    @Published private(set) var installed: Set<CoreModel> = []

    nonisolated static var root: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("Models", isDirectory: true)
    }

    nonisolated static func folder(_ m: CoreModel) -> URL { root.appendingPathComponent(m.rawValue, isDirectory: true) }
    /// Compiled model for single-model assets.
    nonisolated static func compiled(_ m: CoreModel) -> URL { folder(m).appendingPathComponent("model.mlmodelc", isDirectory: true) }
    private nonisolated static func doneMarker(_ m: CoreModel) -> URL { folder(m).appendingPathComponent(".done") }

    private init() { refresh() }

    func refresh() {
        installed = Set(CoreModel.allCases.filter { FileManager.default.fileExists(atPath: Self.doneMarker($0).path) })
    }

    func isInstalled(_ m: CoreModel) -> Bool { installed.contains(m) }

    func delete(_ m: CoreModel) {
        try? FileManager.default.removeItem(at: Self.folder(m))
        refresh()
    }

    /// Downloads and prepares a model if it isn't there yet. Safe to call repeatedly.
    func ensure(_ m: CoreModel) async throws {
        if isInstalled(m) { return }
        if progress[m] != nil {           // another caller is already installing it: wait for that
            while progress[m] != nil { try await Task.sleep(nanoseconds: 300_000_000) }
            if isInstalled(m) { return }
        }
        progress[m] = 0
        defer { progress[m] = nil; refresh() }
        let folder = Self.folder(m)
        try? FileManager.default.removeItem(at: folder)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var url = folder
        try? url.setResourceValues({ var v = URLResourceValues(); v.isExcludedFromBackup = true; return v }())

        let report: @Sendable (Double) -> Void = { p in Task { @MainActor in ModelStore.shared.progress[m] = p } }
        switch m {
        case .remover:
            try await Self.installZippedPackage(URL(string: "https://huggingface.co/mlboydaisuke/coreml-zoo/resolve/main/lama/LaMa.mlpackage.zip")!,
                                                into: m, progress: report)
        case .upscaler:
            try await Self.installZippedPackage(URL(string: "https://huggingface.co/mlboydaisuke/coreml-zoo/resolve/main/realesrgan/RealESRGAN_x4.mlpackage.zip")!,
                                                into: m, progress: report)
        case .generator:
            try await Self.installGenerator(progress: report)
        }
        FileManager.default.createFile(atPath: Self.doneMarker(m).path, contents: Data())
    }

    // MARK: installers

    private nonisolated static func installZippedPackage(_ remote: URL, into m: CoreModel,
                                                         progress: @escaping @Sendable (Double) -> Void) async throws {
        let folder = folder(m)
        let zip = folder.appendingPathComponent("download.zip")
        try await Downloader.fetch(remote, to: zip) { progress($0 * 0.9) }
        let unpacked = folder.appendingPathComponent("unpacked", isDirectory: true)
        try FileManager.default.unzipItem(at: zip, to: unpacked)
        try? FileManager.default.removeItem(at: zip)
        guard let package = findFirst(in: unpacked, suffix: ".mlpackage") ?? findFirst(in: unpacked, suffix: ".mlmodel") else {
            throw CocoaError(.fileReadCorruptFile, userInfo: [NSLocalizedDescriptionKey: "The downloaded model was empty."])
        }
        let temp = try await MLModel.compileModel(at: package)
        let dest = compiled(m)
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.moveItem(at: temp, to: dest)
        try? FileManager.default.removeItem(at: unpacked)
        progress(1)
    }

    /// Default generator: Stable Diffusion 1.5, Apple's 6-bit palettized split-einsum build.
    nonisolated static let defaultGenerator = "apple/coreml-stable-diffusion-v1-5-palettized"

    /// Any Core ML Stable Diffusion repo on Hugging Face that holds compiled resources (TextEncoder, Unet,
    /// VAEDecoder[, VAEEncoder].mlmodelc + vocab.json + merges.txt), either as loose folders or as a zip.
    /// Files are fetched one by one; any bundled yes/no SafetyChecker is skipped (Fixer uses its own
    /// graded Detector instead).
    private nonisolated static func installGenerator(progress: @escaping @Sendable (Double) -> Void) async throws {
        let repo = await MainActor.run { Rules.shared.generatorID }
        let listURL = URL(string: "https://huggingface.co/api/models/\(repo)/tree/main?recursive=true")!
        let (data, response) = try await URLSession.shared.data(from: listURL)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw CocoaError(.fileNoSuchFile, userInfo: [NSLocalizedDescriptionKey: "Hugging Face has no model called \(repo)."])
        }
        struct Entry: Decodable { let type: String; let path: String; let size: Int? }
        let all = try JSONDecoder().decode([Entry].self, from: data).filter { $0.type == "file" }

        // Prefer a loose compiled folder (split_einsum first: it runs on the Neural Engine).
        let unetPaths = all.map(\.path).filter { $0.hasSuffix("Unet.mlmodelc/weights/weight.bin") && !$0.contains("Controlled") }
        let pick = unetPaths.first { $0.contains("split_einsum") } ?? unetPaths.first
        if let pick {
            let prefix = String(pick.dropLast("Unet.mlmodelc/weights/weight.bin".count))
            let files = all.filter { $0.path.hasPrefix(prefix) && !$0.path.contains("SafetyChecker") }
            try await fetchFiles(repo: repo, files: files.map { ($0.path, $0.size) }, prefix: prefix, progress: progress)
            return
        }
        // Otherwise a zip of compiled resources (common in coreml-community repos).
        let zips = all.filter { $0.path.hasSuffix(".zip") }
        guard let zip = zips.first(where: { $0.path.lowercased().contains("split") || $0.path.lowercased().contains("_se") })
                ?? zips.first(where: { $0.path.lowercased().contains("compiled") }) ?? zips.first else {
            throw CocoaError(.fileReadCorruptFile, userInfo: [NSLocalizedDescriptionKey:
                "\(repo) has no compiled Core ML Stable Diffusion files. Look for repos under coreml-community or apple."])
        }
        let dest = folder(.generator)
        let local = dest.appendingPathComponent("download.zip")
        try await Downloader.fetch(URL(string: "https://huggingface.co/\(repo)/resolve/main/\(zip.path)")!, to: local) { progress($0 * 0.95) }
        let unpacked = dest.appendingPathComponent("unpacked", isDirectory: true)
        try FileManager.default.unzipItem(at: local, to: unpacked)
        try? FileManager.default.removeItem(at: local)
        guard let unet = findFirst(in: unpacked, suffix: "Unet.mlmodelc") else {
            throw CocoaError(.fileReadCorruptFile, userInfo: [NSLocalizedDescriptionKey: "This model isn't in a format Fixer can run."])
        }
        let resources = unet.deletingLastPathComponent()
        for item in try FileManager.default.contentsOfDirectory(at: resources, includingPropertiesForKeys: nil)
        where !item.lastPathComponent.contains("SafetyChecker") {
            try FileManager.default.moveItem(at: item, to: dest.appendingPathComponent(item.lastPathComponent))
        }
        try? FileManager.default.removeItem(at: unpacked)
        progress(1)
    }

    private nonisolated static func fetchFiles(repo: String, files: [(String, Int?)], prefix: String,
                                               progress: @escaping @Sendable (Double) -> Void) async throws {
        guard !files.isEmpty else { throw URLError(.cannotParseResponse) }
        let total = Double(files.reduce(0) { $0 + ($1.1 ?? 1) })
        var done = 0.0
        let dest = folder(.generator)
        for (path, bytes) in files {
            let rel = String(path.dropFirst(prefix.count))
            guard let remote = URL(string: "https://huggingface.co/\(repo)/resolve/main/"
                                   + (path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? path)) else { continue }
            let size = Double(bytes ?? 1)
            let base = done
            try await Downloader.fetch(remote, to: dest.appendingPathComponent(rel)) { p in
                progress((base + p * size) / total)
            }
            done += size
        }
        progress(1)
    }

    nonisolated static func findFirst(in dir: URL, suffix: String) -> URL? {
        guard let e = FileManager.default.enumerator(at: dir, includingPropertiesForKeys: nil) else { return nil }
        for case let url as URL in e where url.lastPathComponent.hasSuffix(suffix) && !url.lastPathComponent.hasPrefix("._") {
            return url
        }
        return nil
    }
}
