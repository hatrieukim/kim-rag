import Foundation

/// Cấu hình của chương trình ghi, đọc từ `~/Library/Application Support/kim-rag/mac.json`.
/// Khóa nào thiếu trong file thì lấy giá trị mặc định ở đây.
public struct Config: Codable, Equatable {
    /// Địa chỉ server kim-rag nhận `POST /events`.
    public var server = "http://127.0.0.1:8000"
    /// Để trống khi server không đòi token (API_TOKEN trống trong .env của server).
    public var token = ""
    /// Bundle id của các app không ghi gì: không tên app, không tiêu đề, không clipboard.
    public var ignoreApps = [
        "com.bitwarden.desktop",
        "com.1password.1password",
        "com.agilebits.onepassword7",
        "com.apple.keychainaccess",
        "com.apple.Passwords",
    ]
    /// Regex trên tiêu đề cửa sổ. Khớp thì bỏ tiêu đề và clipboard của cửa sổ đó, vẫn tính thời gian theo app.
    public var ignoreTitles = [
        "(?i)incognito|ẩn danh|private browsing",
        "(?i)internet banking|ngân hàng|ipay|ebanking|vcb digibank",
    ]
    /// Không chạm chuột, bàn phím quá ngần này giây thì coi là rời máy.
    public var idleSeconds: Double = 300
    public var clipboardMaxChars = 2000
    public var sendEverySeconds: Double = 15
    /// Read the macOS calendar (titles, times, attendees; never notes or links) to name speakers and for the journal.
    public var calendar = true
    /// Regex on calendar names. Matching calendars are not read.
    public var ignoreCalendars = ["(?i)birthday|sinh nhật|holiday|ngày lễ|siri|found in"]
    /// Record online meetings (product 8): the mic the meeting app uses and the sound of that app.
    public var meeting = true
    /// Bundle ids of meeting apps. An entry also covers its helper processes ("com.google.Chrome" covers
    /// "com.google.Chrome.helper"). A meeting starts when one of them uses the microphone.
    public var meetingApps = [
        "com.microsoft.teams2", "com.microsoft.teams", "us.zoom.xos", "com.google.Chrome",
        "com.microsoft.edgemac", "company.thebrowser.Browser", "com.tinyspeck.slackmacgap",
        "com.apple.FaceTime", "com.cisco.webexmeetingsapp", "com.vng.zalo", "com.hnc.Discord",
    ]
    /// The meeting is over when its app has not used the mic for this long.
    public var meetingEndSeconds: Double = 30
    public var meetingChunkSeconds: Double = 300
    /// Record the room all day from the Mac's microphone, the way the iPhone recorder does, for the time the
    /// iPhone is not recording. Off unless turned on from the menu bar panel.
    public var room = false
    /// Regex on the name of the microphone the room recording uses. Empty: the built-in microphone, else the
    /// default input. A Bluetooth headset is never picked by default, because recording from it all day switches
    /// it to its low-quality call mode.
    public var roomMic = ""
    public var roomChunkSeconds: Double = 300
    /// Report what the Mac plays out loud, so the server leaves the iPhone speech of those minutes out.
    public var sound = true
    /// Bundle ids whose sound is never counted. An entry also covers its helper processes.
    public var soundIgnoreApps: [String] = []
    /// Regex on output device names that are headphones: the iPhone cannot hear them. The built-in headphone
    /// jack is known without this list.
    public var headphones = ["(?i)airpods|headphone|headset|earphone|earbud|tai nghe|buds|beats|bose|wh-1000|wf-1000"]

    public init() {}

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Config()
        server = try c.decodeIfPresent(String.self, forKey: .server) ?? d.server
        token = try c.decodeIfPresent(String.self, forKey: .token) ?? d.token
        ignoreApps = try c.decodeIfPresent([String].self, forKey: .ignoreApps) ?? d.ignoreApps
        ignoreTitles = try c.decodeIfPresent([String].self, forKey: .ignoreTitles) ?? d.ignoreTitles
        idleSeconds = try c.decodeIfPresent(Double.self, forKey: .idleSeconds) ?? d.idleSeconds
        clipboardMaxChars = try c.decodeIfPresent(Int.self, forKey: .clipboardMaxChars) ?? d.clipboardMaxChars
        sendEverySeconds = try c.decodeIfPresent(Double.self, forKey: .sendEverySeconds) ?? d.sendEverySeconds
        calendar = try c.decodeIfPresent(Bool.self, forKey: .calendar) ?? d.calendar
        ignoreCalendars = try c.decodeIfPresent([String].self, forKey: .ignoreCalendars) ?? d.ignoreCalendars
        meeting = try c.decodeIfPresent(Bool.self, forKey: .meeting) ?? d.meeting
        meetingApps = try c.decodeIfPresent([String].self, forKey: .meetingApps) ?? d.meetingApps
        meetingEndSeconds = try c.decodeIfPresent(Double.self, forKey: .meetingEndSeconds) ?? d.meetingEndSeconds
        meetingChunkSeconds = try c.decodeIfPresent(Double.self, forKey: .meetingChunkSeconds) ?? d.meetingChunkSeconds
        room = try c.decodeIfPresent(Bool.self, forKey: .room) ?? d.room
        roomMic = try c.decodeIfPresent(String.self, forKey: .roomMic) ?? d.roomMic
        roomChunkSeconds = try c.decodeIfPresent(Double.self, forKey: .roomChunkSeconds) ?? d.roomChunkSeconds
        sound = try c.decodeIfPresent(Bool.self, forKey: .sound) ?? d.sound
        soundIgnoreApps = try c.decodeIfPresent([String].self, forKey: .soundIgnoreApps) ?? d.soundIgnoreApps
        headphones = try c.decodeIfPresent([String].self, forKey: .headphones) ?? d.headphones
    }

    public static var directory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/kim-rag", isDirectory: true)
    }

    public static var file: URL { directory.appendingPathComponent("mac.json") }

    /// Đọc file cấu hình. Chưa có thì ghi file mặc định để người dùng có chỗ sửa.
    public static func load(from url: URL = file) -> Config {
        if let data = try? Data(contentsOf: url), let config = try? JSONDecoder().decode(Config.self, from: data) {
            return config
        }
        let config = Config()
        if !FileManager.default.fileExists(atPath: url.path) {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            let enc = JSONEncoder()
            enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            try? enc.encode(config).write(to: url)
        }
        return config
    }

    /// Change one setting in the file, keeping every other key the user wrote as it is.
    public static func set(_ key: String, _ value: Any, in url: URL = file) throws {
        _ = load(from: url)
        let data = try Data(contentsOf: url)
        guard var json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CocoaError(.fileReadCorruptFile)
        }
        json[key] = value
        try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            .write(to: url, options: .atomic)
    }

    /// The configured meeting app a process belongs to, if any.
    public func meetingApp(for bundle: String) -> String? {
        meetingApps.first { bundle == $0 || bundle.hasPrefix($0 + ".") }
    }

    /// Whether a calendar is skipped.
    public func ignoresCalendar(_ name: String) -> Bool {
        ignoreCalendars.contains { name.range(of: $0, options: .regularExpression) != nil }
    }

    /// Tiêu đề có thuộc loại không được ghi hay không.
    public func hidesTitle(_ title: String) -> Bool {
        ignoreTitles.contains { pattern in
            title.range(of: pattern, options: .regularExpression) != nil
        }
    }
}
