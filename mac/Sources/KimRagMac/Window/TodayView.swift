import SwiftUI

/// One day at a glance: numbers first (no LLM, fast), then the journal or the week summary.
struct TodayView: View {
    @ObservedObject var model: WindowModel

    var body: some View {
        let day = dayString(model.day)
        Loader(key: day, load: { try await model.api.get("/app/day", query: ["day": day]) as DayData }) { data, reload in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("\(data.weekday), \(dayLabel(model.day))")
                            .font(.largeTitle.bold())
                        Spacer()
                        Button { reload() } label: { Label("Tải lại", systemImage: "arrow.clockwise") }
                    }
                    counts(data)
                    HStack(alignment: .top, spacing: 14) {
                        MacCard(today: data.mac)
                        VStack(spacing: 14) {
                            RecorderCard(day: data.recorder, isToday: model.isToday)
                            if let room = data.room, room.chunks > 0 {
                                RecorderCard(day: room, isToday: model.isToday, title: "Mic Mac", icon: "laptopcomputer")
                            }
                            CalendarCard(items: data.calendar)
                        }
                    }
                    SummaryCard(model: model)
                }
                .padding(20)
                .frame(maxWidth: 980, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
        }
    }

    private func counts(_ data: DayData) -> some View {
        let byKind = Dictionary(grouping: data.episodes, by: \.kind).mapValues(\.count)
        let zalo = data.zalo.filter { !$0.skipped }.reduce(0) { $0 + $1.messages }
        return HStack(spacing: 10) {
            Stat(value: "\(byKind["voice", default: 0])", label: "ghi âm", icon: "waveform") { model.pane = .conversations }
            Stat(value: "\(byKind["meeting", default: 0])", label: "cuộc họp", icon: "person.2.wave.2") { model.pane = .conversations }
            Stat(value: "\(byKind["code", default: 0])", label: "phiên Claude Code", icon: "chevron.left.forwardslash.chevron.right") {
                model.pane = .conversations
            }
            Stat(value: "\(byKind["read", default: 0])", label: "bài đọc", icon: "book") { model.pane = .conversations }
            Stat(value: "\(zalo)", label: "tin Zalo", icon: "bubble.left.and.bubble.right") { model.pane = .zalo }
        }
    }
}

private struct Stat: View {
    var value: String
    var label: String
    var icon: String
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                Image(systemName: icon).foregroundStyle(.secondary)
                Text(value).font(.title2.bold().monospacedDigit())
                Text(label).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.secondary.opacity(0.15)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

private struct MacCard: View {
    var today: Today

    var body: some View {
        Card(title: "Trên Mac", icon: "laptopcomputer") {
            if today.totalSeconds <= 0 {
                Text("Chưa có hoạt động nào").foregroundStyle(.secondary)
            } else {
                HStack(alignment: .firstTextBaseline) {
                    Text(duration(today.totalSeconds)).font(.title3.bold())
                    if let first = today.first, let last = today.last {
                        Text("\(first)–\(last)").foregroundStyle(.secondary)
                    }
                    Spacer()
                    if today.commits > 0 { Text("\(today.commits) commit").foregroundStyle(.secondary) }
                }
                let top = today.apps.first?.seconds ?? 1
                ForEach(today.apps) { a in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(a.app).lineLimit(1)
                            if let ctx = a.context {
                                Text(ctx).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                            }
                            Spacer(minLength: 8)
                            Text(duration(a.seconds)).monospacedDigit().foregroundStyle(.secondary)
                        }
                        .font(.callout)
                        GeometryReader { geo in
                            Capsule().fill(Color.accentColor.opacity(0.75))
                                .frame(width: max(3, geo.size.width * a.seconds / max(top, 1)))
                        }
                        .frame(height: 4)
                    }
                }
            }
        }
    }
}

private struct RecorderCard: View {
    var day: RecorderDay
    var isToday: Bool
    var title = "iPhone"
    var icon = "iphone"

    var body: some View {
        Card(title: title, icon: icon) {
            if day.chunks == 0 {
                Text(isToday ? "Chưa nhận bản ghi nào hôm nay" : "Không có bản ghi").foregroundStyle(.secondary)
            } else {
                HStack(alignment: .firstTextBaseline) {
                    Text(duration(day.recordedS)).font(.title3.bold())
                    Text("đã ghi").foregroundStyle(.secondary)
                    Spacer()
                    if let first = day.first, let last = day.last {
                        Text("\(clock(first))–\(clock(last))").foregroundStyle(.secondary)
                    }
                }
                if day.lostS >= 60 {
                    Label("Mất \(duration(day.lostS)) lúc đáng lẽ đang ghi", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange).font(.callout)
                }
                if day.calls > 0 {
                    Text("\(day.calls) cuộc gọi điện thoại không ghi").font(.callout).foregroundStyle(.secondary)
                }
                ForEach(day.gaps.filter { $0.seconds >= 300 }.prefix(5)) { g in
                    Text("\(clock(g.start))–\(clock(g.end)) · \(g.reason)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}

private struct CalendarCard: View {
    var items: [DayMeeting]

    var body: some View {
        Card(title: "Lịch", icon: "calendar") {
            if items.isEmpty {
                Text("Không có lịch").foregroundStyle(.secondary)
            }
            ForEach(items) { m in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("\(clock(m.startsAt))–\(clock(m.endsAt))").monospacedDigit().foregroundStyle(.secondary)
                        Text(m.title ?? "(riêng tư)").lineLimit(1)
                    }
                    if !m.people.isEmpty {
                        Text(m.people.prefix(6).joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
                .font(.callout)
            }
        }
    }
}

/// The written journal of the day, or of its week. Writing one calls an LLM, so it loads on its own.
private struct SummaryCard: View {
    @ObservedObject var model: WindowModel
    @State private var period = "day"
    @State private var refresh = 0

    var body: some View {
        let day = dayString(model.day)
        Card(title: period == "day" ? "Nhật ký ngày" : "Tóm tắt tuần", icon: "text.book.closed") {
            HStack {
                Picker("", selection: $period) {
                    Text("Ngày").tag("day")
                    Text("Tuần").tag("week")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 160)
                Spacer()
                Button { refresh += 1 } label: { Label("Viết lại", systemImage: "arrow.clockwise") }
                    .help("Viết lại từ dữ liệu mới nhất (gọi LLM)")
            }
            Loader(key: "\(day)-\(period)-\(refresh)", load: {
                try await model.api.get("/app/summary", query: ["day": day, "period": period,
                                                               "refresh": refresh > 0 ? "true" : nil],
                                        timeout: 300) as SummaryData
            }) { data, _ in
                VStack(alignment: .leading, spacing: 8) {
                    if data.live {
                        Text("Viết lúc \(clock(data.writtenAt)) từ dữ liệu tới lúc đó")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    MarkdownView(text: data.text)
                }
            }
            .frame(minHeight: 120)
        }
    }
}
