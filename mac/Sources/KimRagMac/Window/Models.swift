import Foundation

// What the server's /app routes send (src/lifelog/views.py), decoded with snake_case keys.

struct DayData: Decodable {
    var day: String
    var weekday: String
    var episodes: [EpisodeCard]
    var mac: Today
    var calendar: [DayMeeting]
    var recorder: RecorderDay
    /// The Mac's room recording, used while the iPhone is off. Absent from an older server.
    var room: RecorderDay?
    var zalo: [ZaloChat]
}

struct EpisodeCard: Decodable, Identifiable, Hashable {
    var id: String
    /// voice, meeting, code or read.
    var kind: String
    var stage: String
    var startedAt: Date
    var endedAt: Date
    var speechS: Double?
    var title: String
    var subtitle: String?
    var speakers: [String]
    var summary: String?
    var error: String?
}

struct EpisodeDetail: Decodable {
    struct Turn: Decodable, Identifiable {
        var idx: Int
        var speaker: String
        var known: Bool
        var startedAt: Date
        var lengthS: Double
        var text: String
        var id: Int { idx }
    }
    var id: String
    var kind: String
    var stage: String
    var startedAt: Date
    var endedAt: Date
    var title: String
    var subtitle: String?
    var speakers: [String]
    var summary: String?
    var error: String?
    var hasAudio: Bool
    var url: String?
    var project: String?
    var branch: String?
    var files: [String]
    var turns: [Turn]
}

struct DayMeeting: Decodable, Identifiable {
    var title: String?
    var startsAt: Date
    var endsAt: Date
    var people: [String]
    var id: String { "\(startsAt.timeIntervalSince1970)\(title ?? "")" }
}

struct RecorderDay: Decodable {
    struct Gap: Decodable, Identifiable {
        var start: Date
        var end: Date
        var seconds: Double
        var reason: String
        var id: Date { start }
    }
    var chunks: Int
    var recordedS: Double
    var lostS: Double
    var calls: Int
    var first: Date?
    var last: Date?
    var gaps: [Gap]
}

struct ZaloChat: Decodable, Identifiable, Hashable {
    var id: String
    var kind: String
    var name: String?
    var messages: Int
    var mine: Int
    var mentionsMe: Int
    var skipped: Bool
    var last: Date
}

struct ZaloThread: Decodable {
    struct Message: Decodable, Identifiable {
        var ts: Date
        var msgId: String?
        var fromName: String?
        var isSelf: Bool
        var msgType: String?
        var text: String?
        var quote: String?
        var skipped: Bool
        var id: String { msgId ?? "\(ts.timeIntervalSince1970)" }
    }
    var id: String
    var kind: String?
    var name: String?
    var messages: [Message]
}

struct SummaryData: Decodable {
    var text: String
    var live: Bool
    var writtenAt: Date
}

struct TextData: Decodable {
    var text: String
}

struct SearchData: Decodable {
    struct Passage: Decodable, Identifiable {
        var episodeId: String
        var firstIdx: Int
        var lastIdx: Int
        var startedAt: Date
        var endedAt: Date
        var speakers: [String]
        var text: String
        var score: Double
        var origin: String
        var summary: Bool
        var id: String { "\(episodeId)-\(firstIdx)-\(summary)" }
    }
    struct Source: Decodable {
        var episodeId: String
        var firstIdx: Int
        var lastIdx: Int
        var startedAt: Date
        var origin: String
    }
    struct Fact: Decodable, Identifiable {
        var text: String
        var validAt: Date?
        var invalidAt: Date?
        var sources: [Source]
        var id: String { text }
    }
    var passages: [Passage]
    var facts: [Fact]
    var notes: [String]
}

struct SpeakersData: Decodable {
    struct Unknown: Decodable, Identifiable, Hashable {
        var id: String
        var episodes: Int
        var days: Int
        var speechS: Double
        var lastSeen: Date
        var names: [String]
        var roles: [String]
        var invited: [String]
    }
    struct Person: Decodable, Identifiable, Hashable {
        var id: String
        var name: String
        var isMe: Bool
        var hasSample: Bool
        var episodes: Int
        var speechS: Double
        var lastSeen: Date?
    }
    var queue: [Unknown]
    var people: [Person]
}

struct StatusData: Decodable {
    struct Check: Decodable, Identifiable {
        var section: String
        var good: Bool?
        var label: String
        var detail: String
        var required: Bool
        var id: String { section + label }
    }
    struct Failed: Decodable, Identifiable {
        var id: String
        var kind: String
        var startedAt: Date
        var attempts: Int
        var error: String?
    }
    var ok: Bool
    var dataDir: String
    var notes: [String: [String]]
    var checks: [Check]
    var chunks: [String: Int]?
    var episodes: [String: Int]?
    var waitingEvents: Int?
    var failed: [Failed]?
    var dbError: String?
}
