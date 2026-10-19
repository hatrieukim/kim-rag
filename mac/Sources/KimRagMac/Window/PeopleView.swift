import SwiftUI

/// Voices worth naming (what /speakers did in the browser) and the people already named, with their profile.
struct PeopleView: View {
    @ObservedObject var model: WindowModel
    @State private var selected: String?
    @State private var refresh = 0

    var body: some View {
        Loader(key: "\(refresh)", load: { try await model.api.get("/speakers/data") as SpeakersData }) { data, _ in
            HSplitView {
                List(selection: $selected) {
                    if !data.queue.isEmpty {
                        Section("Giọng cần đặt tên (\(data.queue.count))") {
                            ForEach(data.queue) { u in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(u.names.first.map { "Có thể là \($0)" } ?? u.roles.first ?? "Giọng lạ")
                                        .fontWeight(.medium).lineLimit(1)
                                    Text("\(duration(u.speechS)) nói, \(u.days) ngày, \(u.episodes) bản ghi")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                .tag("u:" + u.id)
                            }
                        }
                    }
                    Section("Đã biết (\(data.people.count))") {
                        ForEach(data.people) { p in
                            HStack {
                                Text(p.name + (p.isMe ? " (bạn)" : "")).lineLimit(1)
                                Spacer()
                                Text(duration(p.speechS)).font(.caption).foregroundStyle(.secondary)
                            }
                            .tag("p:" + p.id)
                        }
                    }
                }
                .frame(minWidth: 240, idealWidth: 290, maxWidth: 380)
                Group {
                    if let s = selected, s.hasPrefix("u:"), let u = data.queue.first(where: { "u:" + $0.id == s }) {
                        UnknownVoiceView(model: model, voice: u) { refresh += 1; selected = nil }
                    } else if let s = selected, s.hasPrefix("p:"), let p = data.people.first(where: { "p:" + $0.id == s }) {
                        PersonView(model: model, person: p)
                    } else {
                        ContentUnavailableView("Chọn một người hoặc một giọng", systemImage: "person.2")
                    }
                }
                .frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}

private struct UnknownVoiceView: View {
    @ObservedObject var model: WindowModel
    @ObservedObject var player: Player
    var voice: SpeakersData.Unknown
    var done: () -> Void
    @State private var name = ""
    @State private var error: String?
    @State private var busy = false

    init(model: WindowModel, voice: SpeakersData.Unknown, done: @escaping () -> Void) {
        self.model = model
        self.player = model.player
        self.voice = voice
        self.done = done
    }

    var body: some View {
        Form {
            Section {
                LabeledContent("Đã nói", value: duration(voice.speechS))
                LabeledContent("Nghe thấy trong", value: "\(voice.episodes) bản ghi, \(voice.days) ngày")
                LabeledContent("Gần nhất", value: "\(dayLabel(voice.lastSeen)) \(clock(voice.lastSeen))")
                if !voice.names.isEmpty { LabeledContent("Tên đoán từ hội thoại", value: voice.names.joined(separator: ", ")) }
                if !voice.roles.isEmpty { LabeledContent("Vai", value: voice.roles.joined(separator: ", ")) }
                if !voice.invited.isEmpty { LabeledContent("Người trong lịch họp", value: voice.invited.joined(separator: ", ")) }
                Button {
                    Task { await player.toggle("u:" + voice.id, api: model.api, path: "/speakers/\(voice.id)/clip") }
                } label: {
                    Label(player.playing == "u:" + voice.id ? "Dừng" : "Nghe giọng",
                          systemImage: player.playing == "u:" + voice.id ? "stop.fill" : "play.fill")
                }
            }
            Section("Người này là ai?") {
                HStack {
                    TextField("Tên", text: $name, prompt: Text(voice.names.first ?? "Ví dụ: anh Tuấn"))
                        .onSubmit { Task { await save() } }
                    ForEach((voice.names + voice.invited).prefix(4), id: \.self) { n in
                        Button(n) { name = n }.buttonStyle(.bordered).controlSize(.small)
                    }
                }
                HStack {
                    Button("Đặt tên") { Task { await save() } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                    Button("Không quan trọng") { Task { await ignore() } }
                        .disabled(busy)
                        .help("Không hỏi lại, trừ khi giọng này còn xuất hiện nhiều")
                    if busy { ProgressView().controlSize(.small) }
                }
                if let error { Text(error).foregroundStyle(.orange) }
            }
        }
        .formStyle(.grouped)
        .onChange(of: voice.id) { name = ""; error = nil }
    }

    private struct NameBody: Encodable { var name: String }

    @MainActor private func save() async {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        await act { try await model.api.post("/speakers/\(voice.id)/name", body: NameBody(name: n)) }
    }

    @MainActor private func ignore() async {
        await act { try await model.api.post("/speakers/\(voice.id)/ignore") }
    }

    @MainActor private func act(_ call: () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do {
            try await call()
            player.stop()
            done()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct PersonView: View {
    @ObservedObject var model: WindowModel
    @ObservedObject var player: Player
    var person: SpeakersData.Person

    init(model: WindowModel, person: SpeakersData.Person) {
        self.model = model
        self.player = model.player
        self.person = person
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .firstTextBaseline) {
                    Text(person.name).font(.largeTitle.bold())
                    Spacer()
                    Button {
                        Task { await player.toggle("p:" + person.id, api: model.api, path: "/speakers/\(person.id)/clip") }
                    } label: {
                        Label(player.playing == "p:" + person.id ? "Dừng" : "Nghe giọng",
                              systemImage: player.playing == "p:" + person.id ? "stop.fill" : "play.fill")
                    }
                }
                Text("\(person.episodes) bản ghi · \(duration(person.speechS)) nói"
                     + (person.lastSeen.map { " · gần nhất \("\(dayLabel($0)) \(clock($0))")" } ?? ""))
                    .foregroundStyle(.secondary)
                Loader(key: person.id, load: {
                    try await model.api.get("/app/person", query: ["name": person.name], timeout: 120) as TextData
                }) { data, _ in
                    MarkdownView(text: data.text)
                }
                .frame(minHeight: 120)
            }
            .padding(20)
        }
    }
}
