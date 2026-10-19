import QuartzCore
import SwiftUI

/// One screen, one control. Monochrome, quiet type, nothing red.
struct ContentView: View {
    @ObservedObject var service: RecorderService
    @Environment(\.scenePhase) private var phase
    @State private var showSettings = false
    @State private var server: TodayReport?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("kim")
                    .font(.system(size: 20, weight: .semibold))
                    .tracking(-0.3)
                Spacer()
                Button { showSettings = true } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 17, weight: .regular))
                        .foregroundStyle(.secondary)
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Settings")
            }
            .padding(.horizontal, 24)
            .padding(.top, 8)

            Spacer()

            Orb(state: service.state, level: service.level) {
                service.state == .idle ? service.start() : service.stop()
            }

            LevelWave(service: service)
                .padding(.top, 32)

            VStack(spacing: 6) {
                Text(title)
                    .font(.system(size: 17, weight: .medium))
                Text(subtitle)
                    .font(.system(size: 14))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            .padding(.top, 20)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 32)

            Spacer()

            Button { service.endConversation() } label: {
                Label("End conversation", systemImage: "checkmark")
                    .font(.system(size: 15, weight: .medium))
                    .padding(.horizontal, 22)
                    .padding(.vertical, 11)
                    .overlay(Capsule().strokeBorder(Color.primary.opacity(0.15)))
            }
            .buttonStyle(.plain)
            .opacity(service.state == .capturing ? 1 : 0.3)
            .disabled(service.state != .capturing)

            Text(footer)
                .font(.system(size: 12))
                .foregroundStyle(.tertiary)
                .padding(.top, 20)
                .padding(.bottom, 12)
                .monospacedDigit()
        }
        .sheet(isPresented: $showSettings) { SettingsView(service: service) }
        // Only while on screen. The app is never suspended (it records in the background), so a plain `.task`
        // would keep asking the server all day, waking the cellular radio once a minute.
        .task(id: phase) {
            guard phase == .active else { return }
            while !Task.isCancelled {
                Uploader.shared.today { server = $0 }
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    private var title: String {
        if let problem = service.problem { return problem }
        switch service.state {
        case .idle: return "Off"
        case .capturing: return "Listening"
        case .interrupted: return "Paused by the system"
        }
    }

    private var subtitle: String {
        var parts = ["\(duration(service.recordedToday)) today"]
        // Confirms the press for a minute.
        if let ended = service.lastEnded, Date().timeIntervalSince(ended) < 60 {
            parts.append("ended \(ended.formatted(date: .omitted, time: .shortened))")
        }
        if service.state == .interrupted { parts.append("resumes on its own") }
        return parts.joined(separator: " · ")
    }

    private var footer: String {
        var parts: [String] = []
        if let error = service.uploadError { parts.append(error) }
        if service.pending > 0 { parts.append("\(service.pending) waiting") }
        if let sent = service.lastSent { parts.append("sent \(sent.formatted(date: .omitted, time: .shortened))") }
        if let server, server.chunks > 0 { parts.append("server has \(duration(server.recordedS))") }
        return parts.isEmpty ? " " : parts.joined(separator: " · ")
    }
}

func duration(_ seconds: Double) -> String {
    let minutes = Int(seconds / 60)
    return minutes < 60 ? "\(minutes)m" : "\(minutes / 60)h \(minutes % 60)m"
}

/// The control: an outlined ring when off, a softly breathing disc while listening, a dashed ring when paused.
private struct Orb: View {
    var state: RecorderService.State
    var level: Float
    var action: () -> Void

    @State private var breathe = false

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle()
                    .strokeBorder(Color.primary.opacity(state == .idle ? 0.18 : 0.08),
                                  style: StrokeStyle(lineWidth: 1, dash: state == .interrupted ? [4, 5] : []))
                    .frame(width: 176, height: 176)
                Circle()
                    .fill(Color.primary)
                    .frame(width: state == .capturing ? 118 : 14, height: state == .capturing ? 118 : 14)
                    .scaleEffect(state == .capturing ? (breathe ? 1.0 : 0.96) + CGFloat(min(level, 0.5)) * 0.08 : 1)
                    .opacity(state == .interrupted ? 0.25 : 1)
            }
            .contentShape(Circle())
            .animation(.spring(response: 0.5, dampingFraction: 0.8), value: state)
            .animation(.easeOut(duration: 0.25), value: level)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(state == .idle ? "Start" : "Stop")
        .onAppear {
            withAnimation(.easeInOut(duration: 2.4).repeatForever(autoreverses: true)) { breathe = true }
        }
    }
}

/// How loud the microphone hears things over the last 8 seconds: newest on the right, taller is louder.
/// Each bar is a tenth of a second (the loudest slice in it), placed by the time it was heard, so the wave glides
/// left slowly instead of jumping. It runs a fraction of a second behind, so each audio buffer (about 85 ms) has
/// arrived before it is shown.
private struct LevelWave: View {
    @ObservedObject var service: RecorderService
    private static let window: Double = 8
    private static let bar: Double = 0.1
    private static let delay: Double = 0.2

    var body: some View {
        // In step with the display, at most 60 times a second, and only while recording; iOS stops it when the
        // app is not on screen, so it costs nothing in the background.
        TimelineView(.animation(minimumInterval: 1.0 / 60, paused: service.state != .capturing)) { _ in
            let now = CACurrentMediaTime() - Self.delay
            var bars: [Int: Float] = [:]
            for v in service.recentLoudness() where v.time <= now && v.time > now - Self.window - Self.bar {
                let slot = Int((v.time / Self.bar).rounded(.up))
                bars[slot] = max(bars[slot] ?? 0, v.value)
            }
            return Canvas { context, size in
                let mid = size.height / 2
                let perSecond = size.width / Self.window
                let width = max(1, Self.bar * perSecond * 0.6)
                context.fill(Path(CGRect(x: 0, y: mid - 0.5, width: size.width, height: 1)),
                             with: .color(.primary.opacity(0.12)))
                for (slot, value) in bars {
                    let h = max(1.5, CGFloat(value) * size.height)
                    let x = size.width - (now - Double(slot) * Self.bar) * perSecond - width
                    let rect = CGRect(x: x, y: mid - h / 2, width: width, height: h)
                    context.fill(Path(roundedRect: rect, cornerRadius: width / 2),
                                 with: .color(.primary.opacity(0.25 + 0.6 * Double(value))))
                }
            }
        }
        .frame(width: 280, height: 40)
        .clipped()
        .accessibilityLabel("Sound level")
    }
}

private struct SettingsView: View {
    @ObservedObject var service: RecorderService
    @Environment(\.dismiss) private var dismiss
    @State private var server = AppSettings.server
    @State private var token = AppSettings.token
    @State private var wifiOnly = AppSettings.wifiOnly
    @State private var lockScreen = AppSettings.lockScreen
    @State private var check: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server", text: $server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Token (optional)", text: $token)
                    Button("Check connection") { test() }
                    if let check { Text(check).font(.footnote).foregroundStyle(.secondary) }
                } header: {
                    Text("Server")
                } footer: {
                    Text("Your kim-rag server, reached through Tailscale.")
                }
                Section {
                    Toggle("Upload on Wi-Fi only", isOn: $wifiOnly)
                    Toggle("Quiet status on lock screen", isOn: $lockScreen)
                    if let why = service.activityProblem {
                        Text(why).font(.footnote).foregroundStyle(.secondary)
                    }
                    if !service.locationAlways {
                        Text("Location is not set to Always, so recordings made with the screen off have no place. Settings › kim › Location › Always.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                } footer: {
                    Text("The lock screen shows only a dot and the words Kim Rag. The orange microphone dot is shown by iOS and cannot be hidden.")
                }
                Section {
                    LabeledContent("Waiting to upload", value: "\(service.pending)")
                    LabeledContent("Version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { save(); dismiss() }
                }
            }
        }
    }

    private func save() {
        AppSettings.server = server
        AppSettings.token = token
        AppSettings.wifiOnly = wifiOnly
        AppSettings.lockScreen = lockScreen
        Uploader.shared.settingsChanged()
        service.updateActivity()
    }

    private func test() {
        save()
        check = "Checking…"
        guard var url = URL(string: AppSettings.server) else { check = "Not a valid address"; return }
        url.append(path: "health")
        URLSession.shared.dataTask(with: URLRequest(url: url, timeoutInterval: 10)) { _, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode
            DispatchQueue.main.async {
                check = code == 200 ? "Connected" : error.map { "Cannot reach: \($0.localizedDescription)" } ?? "Server answered \(code ?? 0)"
            }
        }.resume()
    }
}
