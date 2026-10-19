import SwiftUI

/// Every episode of the day (recordings, meetings, Claude Code sessions, pages read) and, on the right,
/// the one selected with each turn by speaker.
struct ConversationsView: View {
    @ObservedObject var model: WindowModel
    @State private var filter = "all"

    var body: some View {
        let day = dayString(model.day)
        Loader(key: day, load: { try await model.api.get("/app/day", query: ["day": day]) as DayData }) { data, _ in
            HSplitView {
                list(data.episodes)
                    .frame(minWidth: 260, idealWidth: 320, maxWidth: 420)
                Group {
                    if let id = model.openEpisode {
                        EpisodeView(model: model, id: id)
                    } else {
                        ContentUnavailableView("Chọn một mục bên trái", systemImage: "waveform")
                    }
                }
                .frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func list(_ episodes: [EpisodeCard]) -> some View {
        let shown = episodes.filter { filter == "all" || $0.kind == filter }
        return VStack(spacing: 0) {
            Picker("", selection: $filter) {
                Text("Tất cả").tag("all")
                Image(systemName: kindIcon("voice")).help("Ghi âm").tag("voice")
                Image(systemName: kindIcon("meeting")).help("Cuộc họp").tag("meeting")
                Image(systemName: kindIcon("code")).help("Claude Code").tag("code")
                Image(systemName: kindIcon("read")).help("Bài đọc").tag("read")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(8)
            if shown.isEmpty {
                ContentUnavailableView("Không có gì trong ngày", systemImage: "tray")
            } else {
                List(shown, selection: $model.openEpisode) { ep in
                    EpisodeRow(ep: ep).tag(ep.id)
                }
            }
        }
    }
}

private struct EpisodeRow: View {
    var ep: EpisodeCard

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: kindIcon(ep.kind)).foregroundStyle(.secondary).frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(ep.title).fontWeight(.medium).lineLimit(1)
                    Spacer(minLength: 4)
                    Text(clock(ep.startedAt)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                if let sub = ep.subtitle {
                    Text(sub).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if !ep.speakers.isEmpty {
                    Text(ep.speakers.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if ep.error != nil {
                    Label("Lỗi xử lý", systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                } else if !["indexed", "ingested", "transcribed"].contains(ep.stage) {
                    Text("Đang xử lý (\(ep.stage))").font(.caption).foregroundStyle(.orange)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

struct EpisodeView: View {
    @ObservedObject var model: WindowModel
    var id: String

    var body: some View {
        Loader(key: id, load: { try await model.api.get("/app/episodes/\(id)") as EpisodeDetail }) { ep, _ in
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header(ep)
                    if let summary = ep.summary, !summary.isEmpty {
                        Card(title: "Tóm tắt", icon: "text.alignleft") { MarkdownView(text: summary) }
                    }
                    if let error = ep.error {
                        Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                    }
                    if !ep.files.isEmpty {
                        Text("File đã sửa: " + ep.files.prefix(20).joined(separator: ", "))
                            .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    if let player = model.player.error {
                        Text(player).font(.caption).foregroundStyle(.orange)
                    }
                    turns(ep)
                }
                .padding(20)
            }
        }
    }

    private func header(_ ep: EpisodeDetail) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(kindName(ep.kind), systemImage: kindIcon(ep.kind)).font(.caption).foregroundStyle(.secondary)
            Text(ep.title).font(.title2.bold()).textSelection(.enabled)
            HStack(spacing: 8) {
                Text("\(clock(ep.startedAt))–\(clock(ep.endedAt))")
                if let sub = ep.subtitle { Text("· \(sub)") }
                if let branch = ep.branch { Text("· \(branch)") }
                Text("· episode \(ep.id.prefix(8))").textSelection(.enabled)
            }
            .font(.callout).foregroundStyle(.secondary)
            if let url = ep.url, let link = URL(string: url) {
                Link(url, destination: link).font(.callout).lineLimit(1)
            }
        }
    }

    @ViewBuilder private func turns(_ ep: EpisodeDetail) -> some View {
        // Turns in a row by the same speaker read as one block.
        let blocks = ep.turns.reduce(into: [[EpisodeDetail.Turn]]()) { acc, t in
            if let last = acc.last?.last, last.speaker == t.speaker, ep.kind != "read" { acc[acc.count - 1].append(t) }
            else { acc.append([t]) }
        }
        LazyVStack(alignment: .leading, spacing: 12) {
            ForEach(blocks, id: \.first!.idx) { block in
                TurnBlock(model: model, episode: ep, turns: block)
            }
        }
    }
}

private struct TurnBlock: View {
    @ObservedObject var model: WindowModel
    @ObservedObject var player: Player
    var episode: EpisodeDetail
    var turns: [EpisodeDetail.Turn]

    init(model: WindowModel, episode: EpisodeDetail, turns: [EpisodeDetail.Turn]) {
        self.model = model
        self.player = model.player
        self.episode = episode
        self.turns = turns
    }

    var body: some View {
        let first = turns[0]
        VStack(alignment: .leading, spacing: 4) {
            if episode.kind != "read" {
                HStack(spacing: 6) {
                    Text(first.speaker).fontWeight(.semibold)
                        .foregroundStyle(first.known ? Color.primary : Color.secondary)
                    Text(clock(first.startedAt)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            ForEach(turns) { t in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if episode.hasAudio {
                        let key = "\(episode.id)#\(t.idx)"
                        Button {
                            Task { await player.toggle(key, api: model.api, path: "/app/episodes/\(episode.id)/turns/\(t.idx)/audio") }
                        } label: {
                            Image(systemName: player.playing == key ? "stop.circle.fill" : "play.circle")
                        }
                        .buttonStyle(.borderless)
                        .help("Nghe lại lượt này")
                    }
                    Text(t.text).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}
