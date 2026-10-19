import AppKit
import CoreAudio
import Darwin
import KimRagCore

/// Reports what the Mac plays out loud as `sound` events, so the server leaves the iPhone speech of those
/// minutes out. Sent on every change and once a minute as a heartbeat; "off" before the Mac sleeps or the app
/// quits. Keeps running while the screen is locked, since music plays on.
final class SoundWatcher {
    static let pollSeconds: TimeInterval = 2
    static let heartbeatSeconds: TimeInterval = 60

    private let model: PanelModel
    private let queue: EventQueue
    private let config: () -> Config
    private let paused: () -> Bool
    private var timer: Timer?
    private var last: SoundState?
    private var lastSent = Date.distantPast
    private var asleep = false

    init(model: PanelModel, queue: EventQueue, config: @escaping () -> Config, paused: @escaping () -> Bool) {
        self.model = model
        self.queue = queue
        self.config = config
        self.paused = paused
    }

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollSeconds, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    /// The Mac is going to sleep, shutting down, or the app is quitting: nothing will play until `wake`.
    func halt() {
        asleep = true
        if last?.on == true { report(SoundState()) }
        last = nil
        model.soundApps = []
    }

    func wake() {
        asleep = false
        poll()
    }

    func poll() {
        guard !asleep else { return }
        let cfg = config()
        guard cfg.sound else {
            if last?.on == true { report(SoundState()) }
            last = nil
            model.soundApps = []
            return
        }
        let state = current(cfg)
        model.soundApps = state.apps
        if state != last || Date().timeIntervalSince(lastSent) >= Self.heartbeatSeconds {
            report(state)
        }
    }

    private func report(_ state: SoundState) {
        queue.add(type: "sound", payload: state.payload(paused: paused()))
        last = state
        lastSent = Date()
    }

    /// Apps playing on a device the room hears, and not using the mic (a call is a meeting, not a video).
    private func current(_ cfg: Config) -> SoundState {
        var apps = Set<String>()
        var output: String?
        let fallback = AudioSystem.defaultOutput()
        for p in AudioSystem.processes() where p.output && !p.input && !cfg.ignoresSound(of: p.bundle) {
            let devices = p.outputDevices.isEmpty ? [fallback] : p.outputDevices
            guard let device = devices.first(where: { cfg.isAudible(AudioSystem.output($0)) }) else { continue }
            apps.insert(Self.appName(bundle: p.bundle, pid: p.pid))
            output = output ?? AudioSystem.name(device)
        }
        return SoundState(apps: apps.sorted(), output: output)
    }

    /// "Google Chrome" for its helper "com.google.Chrome.helper"; the process name when it has no app.
    static func appName(bundle: String, pid: pid_t) -> String {
        let running = NSWorkspace.shared.runningApplications
        if !bundle.isEmpty, let app = running
            .filter({ guard let id = $0.bundleIdentifier else { return false }; return bundle == id || bundle.hasPrefix(id + ".") })
            .max(by: { ($0.bundleIdentifier?.count ?? 0) < ($1.bundleIdentifier?.count ?? 0) }),
           let name = app.localizedName {
            return name
        }
        if let name = NSRunningApplication(processIdentifier: pid)?.localizedName { return name }
        var buffer = [CChar](repeating: 0, count: 256)
        if proc_name(pid, &buffer, UInt32(buffer.count)) > 0 { return String(cString: buffer) }
        return bundle.isEmpty ? "pid \(pid)" : bundle
    }
}
