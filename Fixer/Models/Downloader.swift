import Foundation

/// Downloads one file with progress, resuming nothing (files are fetched once and kept).
final class Downloader: NSObject, URLSessionDownloadDelegate {
    private var continuation: CheckedContinuation<URL, Error>?
    private var progress: ((Double) -> Void)?
    private var destination: URL!

    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 60 * 60 * 3
        config.waitsForConnectivity = true
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    /// Downloads `url` to `destination` (overwriting), reporting 0...1.
    static func fetch(_ url: URL, to destination: URL, progress: @escaping (Double) -> Void) async throws {
        let d = Downloader()
        _ = try await d.run(url, to: destination, progress: progress)
    }

    private func run(_ url: URL, to destination: URL, progress: @escaping (Double) -> Void) async throws -> URL {
        self.progress = progress
        self.destination = destination
        defer { session.finishTasksAndInvalidate() }
        // Stop must end the download too, not wait for the file to finish.
        let handle = Handle()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<URL, Error>) in
                self.continuation = cont
                let t = session.downloadTask(with: url)
                guard handle.set(t) else {                     // Stop came before the download started
                    self.continuation = nil
                    cont.resume(throwing: CancellationError())
                    return
                }
                t.resume()
            }
        } onCancel: {
            handle.cancel()
        }
    }

    /// The running download task, reachable from the cancellation handler (which runs on any thread).
    private final class Handle: @unchecked Sendable {
        private let lock = NSLock()
        private var task: URLSessionTask?
        private var cancelled = false

        /// Keeps the task; false when it was cancelled already (the task must not start).
        func set(_ t: URLSessionTask) -> Bool {
            lock.lock(); defer { lock.unlock() }
            if cancelled { return false }
            task = t
            return true
        }

        func cancel() {
            lock.lock()
            cancelled = true
            let t = task
            lock.unlock()
            t?.cancel()
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard totalBytesExpectedToWrite > 0 else { return }
        progress?(Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        do {
            if let http = downloadTask.response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                throw URLError(.badServerResponse, userInfo: [NSLocalizedDescriptionKey: "Download failed (HTTP \(http.statusCode))."])
            }
            let fm = FileManager.default
            try fm.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            if fm.fileExists(atPath: destination.path) { try fm.removeItem(at: destination) }
            try fm.moveItem(at: location, to: destination)
            continuation?.resume(returning: destination)
        } catch {
            continuation?.resume(throwing: error)
        }
        continuation = nil
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            // A cancelled download is Stop, not a failure to retry or report.
            continuation?.resume(throwing: (error as? URLError)?.code == .cancelled ? CancellationError() : error)
            continuation = nil
        }
    }
}
