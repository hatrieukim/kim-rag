import SwiftUI

/// What was searched last, kept while the user moves between sections.
final class SearchModel: ObservableObject {
    @Published var query = ""
    @Published var mode = "search"
    @Published var useSince = false
    @Published var since = Calendar.current.date(byAdding: .day, value: -30, to: Date())!
    @Published var result: SearchData?
    @Published var timeline: String?
    @Published var busy = false
    @Published var error: String?

    @MainActor
    func run(_ api: Api) async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        let from = useSince ? dayString(since) : nil
        do {
            if mode == "search" {
                result = try await api.get("/app/search", query: ["q": q, "since": from], timeout: 120)
                timeline = nil
            } else {
                timeline = (try await api.get("/app/timeline", query: ["topic": q, "since": from], timeout: 120) as TextData).text
                result = nil
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct SearchView: View {
    @ObservedObject var model: WindowModel
    @ObservedObject var search: SearchModel

    init(model: WindowModel) {
        self.model = model
        self.search = model.search
    }

    var body: some View {
        VStack(spacing: 0) {
            bar.padding(16)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let error = search.error {
                        Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                    }
                    if let r = search.result { results(r) }
                    if let t = search.timeline { MarkdownView(text: t) }
                    if search.result == nil && search.timeline == nil && search.error == nil && !search.busy {
                        ContentUnavailableView("Tìm theo nghĩa trong mọi thứ đã ghi",
                                               systemImage: "magnifyingglass",
                                               description: Text("Ghi âm, cuộc họp, phiên Claude Code, bài đọc. "
                                                                 + "\"Dòng thời gian\" cho biết một chủ đề đã đổi thế nào."))
                            .padding(.top, 60)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var bar: some View {
        HStack(spacing: 10) {
            Picker("", selection: $search.mode) {
                Text("Tìm").tag("search")
                Text("Dòng thời gian").tag("timeline")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 200)
            TextField(search.mode == "search" ? "Ví dụ: anh Tuấn hứa gửi báo giá khi nào" : "Ví dụ: Redis",
                      text: $search.query)
                .textFieldStyle(.roundedBorder)
                .onSubmit { Task { await search.run(model.api) } }
            Toggle("Từ", isOn: $search.useSince)
            DatePicker("", selection: $search.since, displayedComponents: .date)
                .labelsHidden()
                .disabled(!search.useSince)
            if search.busy {
                ProgressView().controlSize(.small)
            } else {
                Button("Tìm") { Task { await search.run(model.api) } }
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    @ViewBuilder private func results(_ r: SearchData) -> some View {
        ForEach(r.notes, id: \.self) { note in
            Label(note, systemImage: "info.circle").font(.callout).foregroundStyle(.secondary)
        }
        SectionTitle(text: "Nguyên văn: \(r.passages.count) đoạn")
        if r.passages.isEmpty { Text("Không có đoạn nào.").foregroundStyle(.secondary) }
        ForEach(r.passages) { p in
            Button { model.show(episode: p.episodeId, on: p.startedAt) } label: {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text("\(dayLabel(p.startedAt)) \(clock(p.startedAt))")
                        Text("· \(p.origin)")
                        if p.summary { Text("· tóm tắt cả phiên") }
                        Spacer()
                        Text(String(format: "%.2f", p.score)).monospacedDigit()
                            .foregroundStyle(p.score >= 0.55 ? Color.green : Color.secondary)
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    Text(p.text).lineLimit(8).multilineTextAlignment(.leading)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Mở trong Hội thoại")
        }
        SectionTitle(text: "Fact: \(r.facts.count)").padding(.top, 8)
        if r.facts.isEmpty { Text("Không có fact nào.").foregroundStyle(.secondary) }
        ForEach(r.facts) { f in
            VStack(alignment: .leading, spacing: 3) {
                Text(f.text).strikethrough(f.invalidAt != nil).textSelection(.enabled)
                HStack(spacing: 6) {
                    if let v = f.validAt { Text("đúng từ \(dayLabel(v))") }
                    if let i = f.invalidAt { Text("· hết đúng từ \(dayLabel(i))") }
                    ForEach(f.sources.prefix(3), id: \.episodeId) { s in
                        Button("\(s.origin), \(dayLabel(s.startedAt)) \(clock(s.startedAt))") {
                            model.show(episode: s.episodeId, on: s.startedAt)
                        }
                        .buttonStyle(.link)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
