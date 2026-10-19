import EventKit
import Foundation
import KimRagCore

/// Reads the macOS calendar every 15 minutes and whenever it changes, and queues the events of the last
/// 3 days and the next 7 days for the server. Only title, time, location, calendar name and attendees are
/// sent; notes and meeting links never leave the Mac.
final class CalendarReader {
    enum Access { case off, waiting, granted, denied }

    static let pastDays: Double = 3
    static let futureDays: Double = 7
    static let everySeconds: TimeInterval = 900

    private let store = EKEventStore()
    private let queue: EventQueue
    private let config: () -> Config
    private let onAccess: (Access) -> Void
    private let log: (String) -> Void
    private var sent = Set<String>()        // uids already queued since launch
    private var lastSnapshot = ""
    private var timer: Timer?
    private var pending: DispatchWorkItem?
    private(set) var access = Access.waiting

    init(queue: EventQueue, config: @escaping () -> Config, onAccess: @escaping (Access) -> Void,
         log: @escaping (String) -> Void) {
        self.queue = queue
        self.config = config
        self.onAccess = onAccess
        self.log = log
    }

    func start() {
        guard config().calendar else { return set(.off) }
        let done: (Bool, Error?) -> Void = { [weak self] granted, _ in
            DispatchQueue.main.async { self?.started(granted) }
        }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents(completion: done)
        } else {
            store.requestAccess(to: .event, completion: done)
        }
    }

    private func started(_ granted: Bool) {
        set(granted ? .granted : .denied)
        guard granted else { return log("no calendar access; allow KimRag Mac in System Settings > Privacy > Calendars") }
        NotificationCenter.default.addObserver(forName: .EKEventStoreChanged, object: store, queue: .main) { [weak self] _ in
            self?.syncSoon()
        }
        timer = Timer.scheduledTimer(withTimeInterval: Self.everySeconds, repeats: true) { [weak self] _ in self?.sync() }
        sync()
    }

    private func set(_ value: Access) {
        access = value
        onAccess(value)
    }

    /// Calendar apps fire many change notifications in a row: wait a little and read once.
    private func syncSoon() {
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.sync() }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }

    func sync() {
        let cfg = config()
        guard cfg.calendar, access == .granted else { return }
        let now = Date()
        let from = now.addingTimeInterval(-Self.pastDays * 86_400)
        let to = now.addingTimeInterval(Self.futureDays * 86_400)
        let calendars = store.calendars(for: .event).filter { !cfg.ignoresCalendar($0.title) }
        guard !calendars.isEmpty else { return }
        var keys: [String] = []
        var added = 0
        for event in store.events(matching: store.predicateForEvents(withStart: from, end: to, calendars: calendars))
        where event.status != .canceled {
            let item = CalendarItem(
                id: event.calendarItemExternalIdentifier ?? event.eventIdentifier ?? UUID().uuidString,
                title: event.title, start: event.startDate, end: event.endDate, allDay: event.isAllDay,
                location: event.location, calendar: event.calendar.title,
                attendees: (event.attendees ?? []).map(Self.person), organizer: event.organizer.map(Self.person),
                config: cfg)
            keys.append(item.key)
            if sent.insert(item.uid).inserted {
                queue.add(type: "event", payload: item.payload, at: item.start, source: CalendarItem.source, uid: item.uid)
                added += 1
            }
        }
        // Tell the server which events still exist, but only when the set changed.
        let signature = keys.sorted().joined(separator: "\n")
        if signature != lastSnapshot {
            lastSnapshot = signature
            queue.add(type: "snapshot", payload: CalendarItem.snapshot(from: from, to: to, keys: keys),
                      source: CalendarItem.source, uid: "calendar:snapshot:\(UUID().uuidString.lowercased())")
        }
        if added > 0 { log("calendar: \(added) new or changed event(s)") }
    }

    private static func person(_ p: EKParticipant) -> CalendarPerson {
        let url = p.url.absoluteString
        let email = url.lowercased().hasPrefix("mailto:") ? String(url.dropFirst(7)) : nil
        return CalendarPerson(name: p.name, email: email, status: status(p.participantStatus), isMe: p.isCurrentUser)
    }

    private static func status(_ s: EKParticipantStatus) -> String {
        switch s {
        case .accepted: return "accepted"
        case .declined: return "declined"
        case .tentative: return "tentative"
        case .pending: return "pending"
        case .delegated: return "delegated"
        case .completed: return "completed"
        case .inProcess: return "in-process"
        default: return "unknown"
        }
    }
}
