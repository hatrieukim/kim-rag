import AppKit
import SwiftUI

/// Thời gian hôm nay theo app, lấy từ `GET /mac/today` của server: cùng cách tính với nhật ký ngày.
struct Today: Decodable {
    struct AppTime: Decodable, Identifiable {
        var app: String
        var seconds: Double
        var context: String?
        var id: String { app }
    }
    var totalSeconds: Double
    var first: String?
    var last: String?
    var apps: [AppTime]
    var commits: Int
}

/// A meeting being recorded (product 8), as the panel shows it.
struct MeetingStatus: Equatable {
    var app: String
    var started: Date
    /// The mic kim-rag records, and the one the meeting app uses (nil when Core Audio does not say).
    var mic = "?"
    var appMic: String?
    var micMatches: Bool?
    var micError: String?
    /// The app whose sound is recorded, "(all tabs)" for a browser.
    var sound = ""
    var soundError: String?
    var micLevel = 0.0
    var soundLevel = 0.0

    var micProblem: Bool { micError != nil || micMatches == false }
    var problem: Bool { micProblem || soundError != nil }
}

/// The all-day room recording from the Mac's microphone, while it is turned on.
struct RoomStatus: Equatable {
    var recording: Bool
    var mic: String
    var error: String?
}

/// Những gì bảng nhỏ hiển thị. Recorder cập nhật, SwiftUI tự vẽ lại.
final class PanelModel: ObservableObject {
    @Published var paused = false
    @Published var trusted = false
    @Published var calendarDenied = false
    @Published var currentApp = ""
    @Published var currentTitle = ""
    @Published var currentPrivate = false
    @Published var today: Today?
    @Published var waiting = 0
    @Published var lastSent: Date?
    @Published var lastError: String?
    @Published var meeting: MeetingStatus?
    /// Why a meeting that started is not being recorded.
    @Published var meetingBlocked: String?
    @Published var meetingWaiting = 0
    /// Apps the Mac plays out loud right now; the iPhone speech of these minutes is left out.
    @Published var soundApps: [String] = []
    @Published var roomOn = false
    @Published var room: RoomStatus?
    @Published var roomLevel = 0.0
    @Published var roomWaiting = 0

    var onTogglePause: () -> Void = {}
    var onToggleRoom: (Bool) -> Void = { _ in }
    var onEndConversation: () -> Void = {}
    var onOpenConfig: () -> Void = {}
    var onOpenWindow: () -> Void = {}
    var onQuit: () -> Void = {}
}

func duration(_ seconds: Double) -> String {
    let minutes = Int((seconds / 60).rounded())
    if minutes < 1 { return "dưới 1 phút" }
    if minutes < 60 { return "\(minutes) phút" }
    let (h, m) = minutes.quotientAndRemainder(dividingBy: 60)
    return m == 0 ? "\(h) giờ" : "\(h) giờ \(m) phút"
}

struct PanelView: View {
    @ObservedObject var model: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            if let m = model.meeting {
                MeetingSection(meeting: m)
                Divider()
            } else if let blocked = model.meetingBlocked {
                StatusLine(ok: false, text: "Meeting not recorded: \(blocked)")
                Divider()
            }
            RoomSection(model: model)
            Divider()
            current
            todaySection
            Divider()
            footer
        }
        .padding(16)
        .frame(width: 300)
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 10) {
            Circle()
                .fill(model.paused ? Color.secondary.opacity(0.5) : Color.red)
                .frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 1) {
                Text(model.paused ? "Đã tạm dừng" : "Đang ghi").font(.headline)
                Text("⌃⌥⌘P để \(model.paused ? "ghi tiếp" : "tạm dừng")")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button(model.paused ? "Ghi tiếp" : "Tạm dừng") { model.onTogglePause() }
                .controlSize(.small)
        }
    }

    private var current: some View {
        VStack(alignment: .leading, spacing: 3) {
            label("Đang dùng")
            if model.paused {
                Text("Không ghi gì cho tới khi bật lại").foregroundStyle(.secondary)
            } else if model.currentPrivate {
                Text(model.currentApp.isEmpty ? "App trong danh sách bỏ qua" : model.currentApp)
                Text("Không ghi tiêu đề").font(.caption).foregroundStyle(.secondary)
            } else {
                Text(model.currentApp.isEmpty ? "—" : model.currentApp).fontWeight(.medium)
                if !model.currentTitle.isEmpty {
                    Text(model.currentTitle).font(.caption).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
            if !model.soundApps.isEmpty {
                Label("Playing out loud: \(model.soundApps.joined(separator: ", ")). iPhone speech is left out.",
                      systemImage: "speaker.wave.2")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
        }
    }

    @ViewBuilder private var todaySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                label("Hôm nay")
                Spacer()
                if let t = model.today, t.totalSeconds > 0 {
                    Text(duration(t.totalSeconds)).font(.callout.monospacedDigit()).fontWeight(.medium)
                }
            }
            if let t = model.today, let top = t.apps.first, t.totalSeconds > 0 {
                ForEach(t.apps.prefix(4)) { a in
                    AppRow(item: a, fraction: a.seconds / max(top.seconds, 1))
                }
                HStack {
                    if let first = t.first, let last = t.last { Text("\(first)–\(last)") }
                    Spacer()
                    if t.commits > 0 { Text("\(t.commits) commit") }
                }
                .font(.caption).foregroundStyle(.secondary)
            } else {
                Text(model.today == nil ? "Chưa lấy được số liệu từ server" : "Chưa có hoạt động nào")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 6) {
            StatusLine(ok: model.lastError == nil, text: sendText)
            if model.meetingWaiting > 0 {
                StatusLine(ok: model.meeting != nil, text: "\(model.meetingWaiting) meeting chunk(s) waiting to upload")
            }
            if !model.trusted {
                HStack(spacing: 6) {
                    StatusLine(ok: false, text: "Chưa có quyền Accessibility")
                    Spacer()
                    Button("Mở cài đặt") {
                        NSWorkspace.shared.open(URL(string:
                            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
                    }
                    .controlSize(.small)
                }
            }
            if model.calendarDenied {
                HStack(spacing: 6) {
                    StatusLine(ok: false, text: "Chưa có quyền đọc Lịch")
                    Spacer()
                    Button("Mở cài đặt") {
                        NSWorkspace.shared.open(URL(string:
                            "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")!)
                    }
                    .controlSize(.small)
                }
            }
            HStack {
                Button("Mở kim-rag") { model.onOpenWindow() }
                Button("Cấu hình") { model.onOpenConfig() }
                Spacer()
                Button("Thoát") { model.onQuit() }
            }
            .buttonStyle(.borderless)
            .font(.callout)
            .padding(.top, 4)
        }
    }

    private var sendText: String {
        if let error = model.lastError {
            return "Chưa gửi được (\(error)), chờ \(model.waiting)"
        }
        var parts: [String] = []
        if let sent = model.lastSent {
            parts.append("Đã gửi lúc \(sent.formatted(date: .omitted, time: .shortened))")
        } else {
            parts.append("Chưa gửi lần nào")
        }
        if model.waiting > 0 { parts.append("chờ \(model.waiting)") }
        return parts.joined(separator: " · ")
    }

    private func label(_ text: String) -> some View {
        Text(text.uppercased()).font(.caption2).fontWeight(.semibold).foregroundStyle(.secondary).tracking(0.5)
    }
}

private struct AppRow: View {
    var item: Today.AppTime
    var fraction: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(item.app).lineLimit(1)
                if let ctx = item.context {
                    Text(ctx).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 8)
                Text(duration(item.seconds)).font(.callout.monospacedDigit()).foregroundStyle(.secondary)
            }
            .font(.callout)
            GeometryReader { geo in
                Capsule().fill(Color.accentColor.opacity(0.75))
                    .frame(width: max(3, geo.size.width * fraction))
            }
            .frame(height: 3)
        }
    }
}

private struct StatusLine: View {
    var ok: Bool
    var text: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(ok ? Color.green : Color.orange)
            Text(text).lineLimit(1)
        }
        .font(.caption)
    }
}

/// The room recording from the Mac's microphone: a switch, the mic with a live meter, and "End conversation".
private struct RoomSection: View {
    @ObservedObject var model: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("ROOM MIC").font(.caption2).fontWeight(.semibold).foregroundStyle(.secondary).tracking(0.5)
                Spacer()
                Toggle("Record the room", isOn: Binding(get: { model.roomOn }, set: { model.onToggleRoom($0) }))
                    .toggleStyle(.switch).controlSize(.mini).labelsHidden()
            }
            if let room = model.room {
                Track(icon: "mic.fill", title: room.recording ? "Recording" : model.paused ? "Paused" : "Not recording",
                      detail: room.mic, ok: room.error == nil, note: room.error,
                      level: room.recording ? model.roomLevel : 0)
                HStack {
                    Button("End conversation") { model.onEndConversation() }
                        .controlSize(.small).disabled(!room.recording)
                    Spacer()
                    if model.roomWaiting > 0 {
                        Text("\(model.roomWaiting) chunk(s) waiting").font(.caption).foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("Off. Turn on to record conversations around the Mac while the iPhone is not recording.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// Two lines while a meeting is recorded: the user's mic and the meeting app's sound, each with a live meter.
private struct MeetingSection: View {
    var meeting: MeetingStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text("MEETING").font(.caption2).fontWeight(.semibold).foregroundStyle(.secondary).tracking(0.5)
                Text(meeting.app).font(.caption).fontWeight(.medium)
                Spacer()
                Text(meeting.started, style: .timer).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            Track(icon: "mic.fill", title: "You", detail: meeting.mic, ok: !meeting.micProblem,
                  note: micNote, level: meeting.micLevel)
            Track(icon: "person.2.fill", title: "Others", detail: meeting.sound, ok: meeting.soundError == nil,
                  note: meeting.soundError, level: meeting.soundLevel)
        }
    }

    private var micNote: String? {
        if let error = meeting.micError { return error }
        if meeting.micMatches == false { return "\(meeting.app) uses \(meeting.appMic ?? "?")" }
        if meeting.micMatches == nil { return "Could not tell which mic \(meeting.app) uses" }
        return nil
    }
}

private struct Track: View {
    var icon: String
    var title: String
    var detail: String
    var ok: Bool
    var note: String?
    var level: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: icon).frame(width: 14).foregroundStyle(.secondary)
                Text(title).fontWeight(.medium)
                Text(detail).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(ok ? Color.green : Color.orange)
            }
            .font(.callout)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.15))
                    Capsule().fill(Color.accentColor.opacity(0.8))
                        .frame(width: geo.size.width * level)
                        .animation(.linear(duration: 0.1), value: level)
                }
            }
            .frame(height: 4)
            if let note {
                Text(note).font(.caption).foregroundStyle(.orange).lineLimit(2)
            }
        }
    }
}
