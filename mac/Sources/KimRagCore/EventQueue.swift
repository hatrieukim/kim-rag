import Foundation

/// Hàng đợi sự kiện chờ gửi về server, giữ cả trên đĩa để server tắt hay chương trình khởi động lại cũng không mất.
/// Mỗi dòng của file là một sự kiện JSON theo hợp đồng {uid, source, type, ts, payload}.
public final class EventQueue {
    public static let source = "mac"
    /// Server tắt quá lâu thì bỏ bớt sự kiện cũ nhất, để file không phình mãi.
    public static let maxItems = 200_000

    private let url: URL
    private var lines: [String] = []

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = .current
        return f
    }()

    public init(url: URL) {
        self.url = url
        if let text = try? String(contentsOf: url, encoding: .utf8) {
            lines = text.split(separator: "\n").map(String.init)
        }
    }

    public var count: Int { lines.count }

    public static func timestamp(_ date: Date) -> String { iso.string(from: date) }

    /// Thêm một sự kiện. Trả về uid của nó.
    /// `source` and `uid` default to a Mac activity event with a random id.
    @discardableResult
    public func add(type: String, payload: [String: Any] = [:], at date: Date = Date(),
                    source: String = EventQueue.source, uid: String? = nil) -> String {
        let uid = uid ?? "mac:\(UUID().uuidString.lowercased())"
        let event: [String: Any] = [
            "uid": uid, "source": source, "type": type, "ts": Self.timestamp(date), "payload": payload,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: event, options: [.sortedKeys]),
              let line = String(data: data, encoding: .utf8) else { return uid }
        lines.append(line)
        if lines.count > Self.maxItems {
            lines.removeFirst(lines.count - Self.maxItems)
            persist()
        } else {
            append(line)
        }
        return uid
    }

    /// Thân request cho tối đa `limit` sự kiện đầu hàng, kèm số sự kiện trong đó.
    public func batch(limit: Int = 500) -> (body: Data, count: Int)? {
        guard !lines.isEmpty else { return nil }
        let take = lines.prefix(limit)
        return (Data("[\(take.joined(separator: ","))]".utf8), take.count)
    }

    /// Bỏ `n` sự kiện đầu hàng sau khi server đã nhận.
    public func drop(_ n: Int) {
        lines.removeFirst(min(n, lines.count))
        persist()
    }

    /// Đọc lại toàn bộ sự kiện đang chờ, dùng cho kiểm thử.
    public func pending() -> [[String: Any]] {
        lines.compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
    }

    private func append(_ line: String) {
        let data = Data((line + "\n").utf8)
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        } else {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: url)
        }
    }

    private func persist() {
        let text = lines.isEmpty ? "" : lines.joined(separator: "\n") + "\n"
        try? Data(text.utf8).write(to: url, options: .atomic)
    }
}
