import Foundation
import Network

/// Uploads closed chunks to `POST /chunks` with a background URLSession, so uploads continue while the app is
/// suspended. A chunk's files are deleted only after the server answers 200 or 201.
final class Uploader: NSObject, URLSessionTaskDelegate, URLSessionDataDelegate {
    static let shared = Uploader()
    static let sessionId = "com.kim.kimrag.recorder.upload"
    static let maxAttempts = 50

    var backgroundDone: (() -> Void)?
    var onChange: (() -> Void)?
    private(set) var lastSent: Date?
    private(set) var lastError: String?

    private var inFlight = Set<String>()
    private let monitor = NWPathMonitor()
    private lazy var session: URLSession = makeSession()
    private var sessionWifiOnly = AppSettings.wifiOnly

    private func makeSession() -> URLSession {
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionId)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        config.allowsCellularAccess = !AppSettings.wifiOnly
        config.allowsExpensiveNetworkAccess = !AppSettings.wifiOnly
        config.allowsConstrainedNetworkAccess = !AppSettings.wifiOnly
        config.timeoutIntervalForResource = 24 * 3600
        sessionWifiOnly = AppSettings.wifiOnly
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }

    private var started = false

    func start() {
        guard !started else { return }
        started = true
        _ = session
        session.getAllTasks { tasks in
            DispatchQueue.main.async {
                self.inFlight = Set(tasks.compactMap(\.taskDescription))
                self.flush()
            }
        }
        monitor.pathUpdateHandler = { [weak self] path in
            if path.status == .satisfied { DispatchQueue.main.async { self?.flush() } }
        }
        monitor.start(queue: .global(qos: .utility))
    }

    /// The Wi-Fi setting changed: a background session keeps its configuration, so make a new one.
    func settingsChanged() {
        guard sessionWifiOnly != AppSettings.wifiOnly else { return flush() }
        session.invalidateAndCancel()
        inFlight.removeAll()
        session = makeSession()
        flush()
    }

    /// Queue every closed chunk that is not already uploading.
    func flush() {
        guard let url = URL(string: AppSettings.server)?.appendingPathComponent("chunks") else {
            lastError = "Server address is not valid"
            onChange?()
            return
        }
        for s in ChunkStore.ready() where !inFlight.contains(s.id) {
            do {
                let boundary = "kimrag-\(UUID().uuidString)"
                let body = try ChunkStore.makeBody(for: s, boundary: boundary)
                var request = URLRequest(url: url)
                request.httpMethod = "POST"
                request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
                if !AppSettings.token.isEmpty {
                    request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
                }
                let task = session.uploadTask(with: request, fromFile: body)
                task.taskDescription = s.id
                inFlight.insert(s.id)
                task.resume()
            } catch {
                lastError = "Cannot prepare upload: \(error.localizedDescription)"
            }
        }
        onChange?()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let id = task.taskDescription else { return }
        inFlight.remove(id)
        let code = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        if error == nil, code == 200 || code == 201 {
            ChunkStore.remove(id)
            lastSent = Date()
            lastError = nil
        } else {
            try? FileManager.default.removeItem(at: ChunkStore.body(id))
            if var s = ChunkStore.load(id) {
                s.uploadAttempts += 1
                ChunkStore.save(s)
                // A chunk the server keeps refusing (400) is kept on the phone but no longer retried.
                if code == 400 && s.uploadAttempts >= Self.maxAttempts { inFlight.insert(id) }
            }
            lastError = error.map { _ in "Server unreachable" } ?? "Server answered \(code)"
        }
        onChange?()
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async {
            self.backgroundDone?()
            self.backgroundDone = nil
        }
    }

    /// Ask the server how much it received today.
    func today(done: @escaping (TodayReport?) -> Void) {
        guard var url = URL(string: AppSettings.server) else { return done(nil) }
        url.append(path: "recorder/day")
        var request = URLRequest(url: url, timeoutInterval: 10)
        if !AppSettings.token.isEmpty { request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization") }
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            let report = ok ? data.flatMap { try? ChunkStore.decoder.decode(TodayReport.self, from: $0) } : nil
            DispatchQueue.main.async { done(report) }
        }.resume()
    }
}

struct TodayReport: Decodable {
    var recordedS: Double
    var lostS: Double
    var chunks: Int
}
