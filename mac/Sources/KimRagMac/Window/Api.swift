import Foundation
import KimRagCore

/// Talks to the kim-rag server for the main window, with the server address and token from `mac.json`.
struct Api {
    var config: () -> Config

    struct Failure: LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        d.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            guard let date = parseDate(text) else {
                throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "bad date \(text)"))
            }
            return date
        }
        return d
    }()

    /// ISO 8601 as Python writes it: with or without fractional seconds (up to microseconds), any offset.
    static func parseDate(_ text: String) -> Date? {
        var s = text
        if let dot = s.firstIndex(of: "."), let end = s[dot...].firstIndex(where: { $0 == "+" || $0 == "-" || $0 == "Z" }) {
            let fraction = s[s.index(after: dot)..<end]
            s.replaceSubrange(dot..<end, with: "." + fraction.prefix(3).padding(toLength: 3, withPad: "0", startingAt: 0))
        }
        let f = ISO8601DateFormatter()
        f.formatOptions = s.contains(".") ? [.withInternetDateTime, .withFractionalSeconds] : [.withInternetDateTime]
        return f.date(from: s)
    }

    func request(_ path: String, query: [String: String?] = [:], method: String = "GET",
                 body: Encodable? = nil, timeout: TimeInterval = 30) throws -> URLRequest {
        let c = config()
        guard var parts = URLComponents(string: c.server + path) else { throw Failure(message: "bad server address") }
        let items = query.compactMap { k, v in v.map { URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { parts.queryItems = items }
        guard let url = parts.url else { throw Failure(message: "bad server address") }
        var r = URLRequest(url: url, timeoutInterval: timeout)
        r.httpMethod = method
        if !c.token.isEmpty { r.setValue("Bearer \(c.token)", forHTTPHeaderField: "Authorization") }
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONEncoder().encode(body)
        }
        return r
    }

    func data(_ path: String, query: [String: String?] = [:], method: String = "GET", body: Encodable? = nil,
              timeout: TimeInterval = 30) async throws -> Data {
        let r = try request(path, query: query, method: method, body: body, timeout: timeout)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: r)
        } catch let error as URLError where error.code == .cannotConnectToHost || error.code == .networkConnectionLost {
            throw Failure(message: "Server kim-rag chưa chạy ở \(config().server)")
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["detail"]
            throw Failure(message: detail ?? "HTTP \(code)")
        }
        return data
    }

    func get<T: Decodable>(_ path: String, query: [String: String?] = [:], timeout: TimeInterval = 30) async throws -> T {
        try Self.decoder.decode(T.self, from: try await data(path, query: query, timeout: timeout))
    }

    func post(_ path: String, body: Encodable? = nil) async throws {
        _ = try await data(path, method: "POST", body: body)
    }
}

/// "2026-10-15" for a day in the Mac's time zone, the form the server takes.
func dayString(_ date: Date) -> String {
    let f = DateFormatter()
    f.calendar = Calendar(identifier: .gregorian)
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd"
    return f.string(from: date)
}

/// "15/10/2026", the way the journal writes days.
func dayLabel(_ date: Date) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "vi_VN")
    f.dateFormat = "dd/MM/yyyy"
    return f.string(from: date)
}

/// "Thứ năm" for a date.
func weekdayLabel(_ date: Date) -> String {
    ["Chủ nhật", "Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy"][Calendar.current.component(.weekday, from: date) - 1]
}

func clock(_ date: Date) -> String {
    date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
}
