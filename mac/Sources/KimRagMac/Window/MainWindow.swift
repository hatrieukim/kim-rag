import AppKit
import AVFoundation
import KimRagCore
import SwiftUI

/// The sections in the window's sidebar.
enum Pane: String, CaseIterable, Identifiable {
    case today, conversations, zalo, search, people, status
    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: "Hôm nay"
        case .conversations: "Hội thoại"
        case .zalo: "Zalo"
        case .search: "Tìm"
        case .people: "Người"
        case .status: "Hệ thống"
        }
    }

    var icon: String {
        switch self {
        case .today: "sun.max"
        case .conversations: "waveform"
        case .zalo: "bubble.left.and.bubble.right"
        case .search: "magnifyingglass"
        case .people: "person.2"
        case .status: "stethoscope"
        }
    }

    /// Sections that show one day and share the day picker.
    var byDay: Bool { self == .today || self == .conversations || self == .zalo }
}

/// State shared by every section: the server, the day being looked at, and the audio player.
final class WindowModel: ObservableObject {
    let api: Api
    @Published var pane: Pane = .today
    @Published var day = Calendar.current.startOfDay(for: Date())
    /// The episode to open in Hội thoại, set when a search result or a card is clicked.
    @Published var openEpisode: String?
    /// The Zalo chat open in the Zalo section.
    @Published var openChat: String?
    let player = Player()
    let search = SearchModel()
    var onOpenConfig: () -> Void = {}

    init(api: Api) { self.api = api }

    var isToday: Bool { Calendar.current.isDateInToday(day) }

    func shift(days: Int) {
        guard let d = Calendar.current.date(byAdding: .day, value: days, to: day) else { return }
        day = min(d, Calendar.current.startOfDay(for: Date()))
    }

    /// Jump to an episode on its own day.
    func show(episode id: String, on date: Date) {
        day = Calendar.current.startOfDay(for: date)
        openEpisode = id
        pane = .conversations
    }
}

/// Plays one WAV at a time from the server: a turn of a conversation or a speaker's voice.
final class Player: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published var playing: String?
    @Published var error: String?
    private var audio: AVAudioPlayer?

    @MainActor
    func toggle(_ key: String, api: Api, path: String) async {
        if playing == key {
            stop()
            return
        }
        stop()
        playing = key
        do {
            let data = try await api.data(path, timeout: 60)
            guard playing == key else { return }
            audio = try AVAudioPlayer(data: data)
            audio?.delegate = self
            audio?.play()
            error = nil
        } catch {
            self.error = error.localizedDescription
            playing = nil
        }
    }

    func stop() {
        audio?.stop()
        audio = nil
        playing = nil
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async { self.playing = nil }
    }
}

struct MainView: View {
    @ObservedObject var model: WindowModel

    var body: some View {
        NavigationSplitView {
            List(Pane.allCases, selection: $model.pane) { pane in
                Label(pane.title, systemImage: pane.icon).tag(pane)
            }
            .navigationSplitViewColumnWidth(min: 160, ideal: 180, max: 220)
        } detail: {
            Group {
                switch model.pane {
                case .today: TodayView(model: model)
                case .conversations: ConversationsView(model: model)
                case .zalo: ZaloView(model: model)
                case .search: SearchView(model: model)
                case .people: PeopleView(model: model)
                case .status: StatusView(model: model)
                }
            }
            .toolbar {
                if model.pane.byDay { DayPicker(model: model) }
            }
        }
        .navigationTitle(model.pane.title)
        .navigationSubtitle(model.pane.byDay ? "\(weekdayLabel(model.day)), \(dayLabel(model.day))" : "")
        .frame(minWidth: 860, minHeight: 540)
    }
}

private struct DayPicker: ToolbarContent {
    @ObservedObject var model: WindowModel

    var body: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Button { model.shift(days: -1) } label: { Image(systemName: "chevron.left") }
                .help("Ngày trước")
            DatePicker("", selection: $model.day, in: ...Date(), displayedComponents: .date)
                .labelsHidden()
                .datePickerStyle(.field)
            Button { model.shift(days: 1) } label: { Image(systemName: "chevron.right") }
                .help("Ngày sau")
                .disabled(model.isToday)
            Button("Hôm nay") { model.day = Calendar.current.startOfDay(for: Date()) }
                .disabled(model.isToday)
        }
    }
}

/// The main window. Closing it keeps the app running in the menu bar; only Quit stops recording.
final class MainWindowController: NSWindowController, NSWindowDelegate {
    let model: WindowModel

    init(model: WindowModel) {
        self.model = model
        let host = NSHostingController(rootView: MainView(model: model))
        host.sceneBridgingOptions = [.toolbars, .title]
        let window = NSWindow(contentViewController: host)
        window.title = "kim-rag"
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        window.toolbarStyle = .unified
        window.setContentSize(NSSize(width: 1100, height: 720))
        window.setFrameAutosaveName("KimRagMainWindow")
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The app sits in the Dock (with its menus) only while the window is open; closed, it lives in the menu bar.
    func present() {
        NSApp.setActivationPolicy(.regular)
        if window?.isVisible != true { window?.center() }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func windowWillClose(_ notification: Notification) {
        model.player.stop()
        NSApp.setActivationPolicy(.accessory)
    }
}

// MARK: - Small pieces shared by the sections

/// A section that loads once per key (a day, an id) and shows a spinner or the error meanwhile.
struct Loader<Value, Content: View>: View {
    var key: String
    var load: () async throws -> Value
    @ViewBuilder var content: (Value, @escaping () -> Void) -> Content

    @State private var value: Value?
    @State private var error: String?
    @State private var loadedKey: String?
    @State private var bump = 0

    var body: some View {
        Group {
            if let value, loadedKey == key {
                content(value) { bump += 1 }
            } else if let error {
                ContentUnavailableView {
                    Label("Không tải được", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error)
                } actions: {
                    Button("Thử lại") { bump += 1 }
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task(id: "\(key)#\(bump)") {
            if loadedKey != key { value = nil }
            error = nil
            do {
                let v = try await load()
                guard !Task.isCancelled else { return }
                value = v
                loadedKey = key
            } catch is CancellationError {
            } catch {
                guard !Task.isCancelled else { return }
                self.error = error.localizedDescription
            }
        }
    }
}

struct SectionTitle: View {
    var text: String
    var body: some View {
        Text(text.uppercased()).font(.caption).fontWeight(.semibold).foregroundStyle(.secondary).tracking(0.5)
    }
}

struct Card<Content: View>: View {
    var title: String
    var icon: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: icon).font(.headline)
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.secondary.opacity(0.15)))
    }
}

func kindIcon(_ kind: String) -> String {
    switch kind {
    case "meeting": "person.2.wave.2"
    case "code": "chevron.left.forwardslash.chevron.right"
    case "read": "book"
    case "chat": "bubble.left.and.bubble.right"
    default: "waveform"
    }
}

func kindName(_ kind: String) -> String {
    switch kind {
    case "meeting": "Cuộc họp"
    case "code": "Claude Code"
    case "read": "Bài đọc"
    case "chat": "Zalo"
    default: "Ghi âm"
    }
}
