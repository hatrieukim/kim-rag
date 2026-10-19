import CryptoKit
import Foundation

/// One person on a calendar event.
public struct CalendarPerson: Equatable {
    public var name: String?
    public var email: String?
    public var status: String       // accepted | declined | tentative | pending | unknown ...
    public var isMe: Bool

    public init(name: String?, email: String?, status: String, isMe: Bool) {
        self.name = name
        self.email = email
        self.status = status
        self.isMe = isMe
    }

    var payload: [String: Any] {
        var out: [String: Any] = ["status": status, "me": isMe]
        if let name, !name.isEmpty { out["name"] = name }
        if let email, !email.isEmpty { out["email"] = email }
        return out
    }
}

/// One occurrence of a calendar event, as sent to the server (source "calendar", type "event").
/// Built from EventKit by the app; kept free of EventKit so it can be tested.
public struct CalendarItem {
    public static let source = "calendar"
    public static let maxText = 200

    public var key: String          // calendar item id + "@" + occurrence start: recurring events stay apart
    public var title: String?
    public var start: Date
    public var end: Date
    public var allDay: Bool
    public var location: String?
    public var calendar: String
    public var attendees: [CalendarPerson]
    public var organizer: CalendarPerson?

    public init(id: String, title: String?, start: Date, end: Date, allDay: Bool, location: String?,
                calendar: String, attendees: [CalendarPerson], organizer: CalendarPerson?, config: Config) {
        key = "\(id)@\(EventQueue.timestamp(start))"
        // Private titles (banking, incognito patterns) are dropped; the rest is scrubbed like window titles.
        if let title, !title.isEmpty, !config.hidesTitle(title) {
            self.title = String(Scrub.clean(title).prefix(Self.maxText))
        }
        if let location, !location.isEmpty {
            self.location = String(Scrub.clean(location).prefix(Self.maxText))
        }
        self.start = start
        self.end = end
        self.allDay = allDay
        self.calendar = calendar
        self.attendees = attendees
        self.organizer = organizer
    }

    public var payload: [String: Any] {
        var out: [String: Any] = [
            "key": key, "start": EventQueue.timestamp(start), "end": EventQueue.timestamp(end),
            "all_day": allDay, "calendar": calendar, "attendees": attendees.map(\.payload),
        ]
        if let title { out["title"] = title }
        if let location { out["location"] = location }
        if let organizer { out["organizer"] = organizer.payload }
        return out
    }

    /// Same content, same uid: sending an unchanged event again is ignored by the server.
    public var uid: String {
        let data = (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data(key.utf8)
        return "calendar:" + Self.hash(data)
    }

    public static func hash(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined().prefix(32).description
    }

    /// Keys of every event in a window, so the server can mark the ones that disappeared (type "snapshot").
    public static func snapshot(from: Date, to: Date, keys: [String]) -> [String: Any] {
        ["from": EventQueue.timestamp(from), "to": EventQueue.timestamp(to), "keys": keys.sorted()]
    }
}
