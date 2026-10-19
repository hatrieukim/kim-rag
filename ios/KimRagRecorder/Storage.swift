import Foundation
import UIKit

/// User settings, kept in UserDefaults.
enum AppSettings {
    /// The server set at build time (KIMRAG_SERVER in ios/Config/Local.xcconfig); empty when none was set.
    static let defaultServer = (Bundle.main.object(forInfoDictionaryKey: "KimRagServer") as? String ?? "")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    private static let d = UserDefaults.standard

    static var server: String {
        get { d.string(forKey: "server") ?? defaultServer }
        set { d.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "server") }
    }
    static var token: String {
        get { d.string(forKey: "token") ?? "" }
        set { d.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "token") }
    }
    /// Upload only on Wi-Fi. Off means cellular too (Tailscale works on both).
    static var wifiOnly: Bool {
        get { d.object(forKey: "wifiOnly") as? Bool ?? false }
        set { d.set(newValue, forKey: "wifiOnly") }
    }
    /// Show the quiet status strip on the lock screen.
    static var lockScreen: Bool {
        get { d.object(forKey: "lockScreen") as? Bool ?? true }
        set { d.set(newValue, forKey: "lockScreen") }
    }
    /// The user wants to be recording: resume after a relaunch.
    static var wantsRecording: Bool {
        get { d.bool(forKey: "wantsRecording") }
        set { d.set(newValue, forKey: "wantsRecording") }
    }
}

/// What the app knows about one chunk, sent to the server as the upload's metadata.
struct Sidecar: Codable {
    struct Place: Codable {
        var lat: Double
        var lon: Double
        var accuracy: Double
        var at: Date
    }

    var id: String
    var startedAt: Date
    var endedAt: Date?
    var endReason: String?          // rotate | close | stop | interruption | route_change | media_reset | update | app_killed | error
    var input: String?              // microphone in use
    var batteryStart: Float?
    var batteryEnd: Float?
    var charging: Bool?
    var location: Place?
    var motion: String?             // still | walking | running | cycling | automotive
    var call = false
    var playback: [[Double]]?       // [start, end] seconds while another app played sound on the iPhone speaker
    var appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    var device = UIDevice.current.model
    var uploadAttempts = 0
}

/// Chunks on disk: `<id>.rec.aac` while recording, `<id>.aac` + `<id>.json` once closed, deleted after the
/// server confirms. ADTS AAC stays readable even if the app is killed mid-write.
enum ChunkStore {
    static let dir: URL = {
        var url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("chunks", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true,
                                                 attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
        return url
    }()

    static func recording(_ id: String) -> URL { dir.appendingPathComponent("\(id).rec.aac") }
    static func audio(_ id: String) -> URL { dir.appendingPathComponent("\(id).aac") }
    static func sidecar(_ id: String) -> URL { dir.appendingPathComponent("\(id).json") }
    static func body(_ id: String) -> URL { dir.appendingPathComponent("\(id).body") }

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.dateEncodingStrategy = .custom { date, enc in
            var c = enc.singleValueContainer()
            try c.encode(iso.string(from: date))
        }
        return e
    }()
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        d.dateDecodingStrategy = .custom { dec in
            let text = try dec.singleValueContainer().decode(String.self)
            return iso.date(from: text) ?? Date()
        }
        return d
    }()
    static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func save(_ s: Sidecar) {
        try? encoder.encode(s).write(to: sidecar(s.id), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    static func load(_ id: String) -> Sidecar? {
        (try? Data(contentsOf: sidecar(id))).flatMap { try? decoder.decode(Sidecar.self, from: $0) }
    }

    /// Closed chunks waiting for upload, oldest first.
    static func ready() -> [Sidecar] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return names.filter { $0.hasSuffix(".aac") && !$0.hasSuffix(".rec.aac") }
            .compactMap { load(String($0.dropLast(4))) }
            .filter { $0.endedAt != nil }
            .sorted { $0.startedAt < $1.startedAt }
    }

    static func remove(_ id: String) {
        for url in [recording(id), audio(id), sidecar(id), body(id)] { try? FileManager.default.removeItem(at: url) }
    }

    /// Files left by a recording the app could not close (killed by the system): close them now.
    static func recoverLeftovers(except current: String?) -> Int {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        // Killed by a reinstall from the Mac (it announces itself first) rather than by the system.
        let updating = (UserDefaults.standard.object(forKey: "updatingAt") as? Date).map { Date().timeIntervalSince($0) < 1800 } ?? false
        UserDefaults.standard.removeObject(forKey: "updatingAt")
        var n = 0
        for name in names where name.hasSuffix(".rec.aac") {
            let id = String(name.dropLast(8))
            guard id != current else { continue }
            let url = recording(id)
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
            guard size > 1024, var s = load(id) else { remove(id); continue }
            if s.endedAt == nil {   // closed properly but not renamed yet keeps its own reason
                s.endedAt = (try? FileManager.default.attributesOfItem(atPath: url.path)[.modificationDate] as? Date) ?? Date()
                s.endReason = updating ? "update" : "app_killed"
            }
            save(s)
            try? FileManager.default.moveItem(at: url, to: audio(id))
            n += 1
        }
        return n
    }

    /// multipart/form-data body for `POST /chunks`, written to a file for a background upload.
    static func makeBody(for s: Sidecar, boundary: String) throws -> URL {
        var meta = try JSONSerialization.jsonObject(with: encoder.encode(s)) as? [String: Any] ?? [:]
        meta["client_id"] = "iphone:\(s.id)"
        meta["source"] = "iphone"
        // Recording stopped, or the user ended the conversation: the server closes it without waiting.
        meta["close_episode"] = s.endReason == "stop" || s.endReason == "close"
        meta.removeValue(forKey: "upload_attempts")
        let metaData = try JSONSerialization.data(withJSONObject: meta, options: [.sortedKeys])
        var body = Data()
        func line(_ text: String) { body.append(Data((text + "\r\n").utf8)) }
        line("--\(boundary)")
        line("Content-Disposition: form-data; name=\"metadata\"")
        line("Content-Type: application/json")
        line("")
        body.append(metaData)
        line("")
        line("--\(boundary)")
        line("Content-Disposition: form-data; name=\"audio\"; filename=\"\(s.id).aac\"")
        line("Content-Type: audio/aac")
        line("")
        body.append(try Data(contentsOf: audio(s.id)))
        line("")
        line("--\(boundary)--")
        try body.write(to: self.body(s.id), options: .atomic)
        return self.body(s.id)
    }

    static var pendingCount: Int { ready().count }
}
