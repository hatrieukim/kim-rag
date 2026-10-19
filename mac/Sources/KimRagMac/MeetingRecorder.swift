import AppKit
import AVFoundation
import CoreAudio
import Foundation
import KimRagCore
import UserNotifications

/// Records online meetings (product 8).
///
/// A meeting starts when an app from `meetingApps` uses the microphone and ends `meetingEndSeconds` after it
/// stops. Two channels are recorded: the mic the meeting app is using (the user) and the sound of the meeting
/// app (everyone else). Audio is written as raw PCM in `meetings/pending`, cut every `meetingChunkSeconds`,
/// encoded to AAC in `meetings/outbox` and uploaded to `POST /chunks`; files are deleted once the server has them.
/// The iPhone keeps recording: the server drops its audio of the same minutes.
final class MeetingRecorder {
    static let pollSeconds: TimeInterval = 2
    static let writeSeconds: TimeInterval = 0.5
    /// Audio is written this far behind real time, so both tracks have arrived.
    static let lagSeconds: TimeInterval = 1
    /// A longer jump of the clock (the Mac slept) starts a new chunk instead of writing silence.
    static let jumpSeconds: TimeInterval = 5
    /// Without a single tap buffer after this long, the tap is rebuilt with a clock device.
    static let tapCheckSeconds: TimeInterval = 3
    /// Meeting apps that are browsers: their tap takes every tab, not just the meeting.
    static let browsers: Set<String> = ["com.google.Chrome", "com.microsoft.edgemac", "company.thebrowser.Browser"]

    private final class Meeting {
        let id = UUID().uuidString.lowercased()
        let key: String
        let name: String
        let started = Date()
        let mixer = MeetingMixer()
        var origin = Date()
        var lastSeen = Date()
        var chunk: MeetingChunk
        var file: FileHandle?
        var frames = 0
        var mic: MicCapture!
        var tap: AppAudioTap!
        var tapStarted = Date()
        var tapError: String?
        var micError: String?
        var micRetry = Date.distantPast
        var appMic: AudioDeviceID?
        var micName = "?"
        var notifiedMismatch = false

        init(key: String, name: String, chunk: MeetingChunk) {
            self.key = key
            self.name = name
            self.chunk = chunk
        }
    }

    private let model: PanelModel
    private let queue: EventQueue
    private let config: () -> Config
    private let paused: () -> Bool
    private let log: (String) -> Void
    var onChange: () -> Void = {}

    private let pending = Config.directory.appendingPathComponent("meetings/pending", isDirectory: true)
    private lazy var outbox: ChunkOutbox = {
        let box = ChunkOutbox(
            dir: Config.directory.appendingPathComponent("meetings/outbox", isDirectory: true),
            failed: Config.directory.appendingPathComponent("meetings/failed", isDirectory: true),
            what: "meeting", config: config, metadata: { data in
                let dec = JSONDecoder()
                dec.dateDecodingStrategy = .iso8601
                return (try? dec.decode(MeetingChunk.self, from: data))?.metadata()
            }, log: log)
        box.onChange = { [weak self] in self?.publish() }
        return box
    }()
    private let encoder = DispatchQueue(label: "com.kim.kimrag.meeting.encode", qos: .utility)
    private var meeting: Meeting?
    private var timers: [Timer] = []
    /// Whether the panel is open; meters are only updated then.
    var panelVisible: () -> Bool = { false }

    init(model: PanelModel, queue: EventQueue, config: @escaping () -> Config, paused: @escaping () -> Bool,
         log: @escaping (String) -> Void) {
        self.model = model
        self.queue = queue
        self.config = config
        self.paused = paused
        self.log = log
    }

    var active: Bool { meeting != nil }

    func start() {
        try? FileManager.default.createDirectory(at: pending, withIntermediateDirectories: true)
        recoverLeftovers()
        if config().meeting && MicCapture.permission == .notDetermined {
            MicCapture.askPermission { [weak self] ok in self?.log("microphone permission: \(ok ? "granted" : "denied")") }
        }
        timers = [
            Timer.scheduledTimer(withTimeInterval: Self.pollSeconds, repeats: true) { [weak self] _ in self?.poll() },
            Timer.scheduledTimer(withTimeInterval: Self.writeSeconds, repeats: true) { [weak self] _ in self?.write() },
            Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.outbox.upload() },
            Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.meters() },
        ]
        outbox.upload()
    }

    /// Wait for chunks being encoded, so quitting does not leave them half done.
    func flush() {
        encoder.sync {}
    }

    // MARK: - Watching for meetings

    func poll() {
        let config = config()
        let processes = AudioSystem.processes()
        if let m = meeting {
            let mine = processes.filter { config.meetingApp(for: $0.bundle) == m.key }
            if mine.contains(where: \.input) { m.lastSeen = Date() }
            if paused() {
                stop(reason: "pause")
            } else if !config.meeting {
                stop(reason: "disabled")
            } else if Date().timeIntervalSince(m.lastSeen) > config.meetingEndSeconds {
                stop(reason: "meeting_end")
            } else {
                follow(m, mine)
            }
            return
        }
        guard config.meeting, !paused(),
              let caller = processes.first(where: { $0.input && config.meetingApp(for: $0.bundle) != nil }),
              let key = config.meetingApp(for: caller.bundle)
        else { return }
        begin(key: key, processes: processes.filter { config.meetingApp(for: $0.bundle) == key })
    }

    private func begin(key: String, processes: [AudioProcess]) {
        guard MicCapture.permission == .authorized else {
            if MicCapture.permission == .notDetermined { MicCapture.askPermission { _ in } }
            if model.meetingBlocked == nil {
                log("meeting in \(key) not recorded: no microphone permission")
            }
            model.meetingBlocked = "Microphone permission is off"
            return
        }
        model.meetingBlocked = nil
        let name = NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == key }?.localizedName ?? key
        let chunk = MeetingChunk(meetingId: "", index: 0, startedAt: Date(), app: name, bundle: key, mic: "?")
        let m = Meeting(key: key, name: name, chunk: chunk)
        m.chunk.meetingId = m.id
        m.mic = MicCapture { [weak m] in m?.mixer.append($0, track: 0) }
        m.mic.onChange = { [weak self, weak m] in
            guard let self, let m else { return }
            self.log("microphone changed, restarting it")
            self.startMic(m, device: m.mic.device == kAudioObjectUnknown ? AudioSystem.defaultInput() : m.mic.device)
        }
        m.tap = AppAudioTap { [weak m] in m?.mixer.append($0, track: 1) }
        meeting = m
        follow(m, processes)
        m.chunk.mic = m.micName
        openChunk(m, at: m.origin)
        queue.add(type: "meeting_start", payload: ["meeting_id": m.id, "app": name, "bundle": key, "mic": m.micName])
        log("meeting started in \(name), mic \(m.micName)\(m.tapError.map { ", app sound: \($0)" } ?? "")")
        notify("Recording meeting", "Mic: \(m.micName) · Sound: \(appLabel(m))")
        publish()
    }

    /// Keep recording the right mic and every process of the app.
    private func follow(_ m: Meeting, _ processes: [AudioProcess]) {
        m.appMic = processes.flatMap(\.inputDevices).first { AudioSystem.hasInput($0) }
        let want = m.appMic ?? AudioSystem.defaultInput()
        if want != m.mic.device && (m.micError == nil || Date() >= m.micRetry) {
            startMic(m, device: want)
        }
        let objects = Set(processes.map(\.object))
        if !objects.isEmpty && objects != m.tap.processes {
            startTap(m, objects, withClock: false)
        } else if m.tapError == nil, !m.tap.withClock, m.tap.received == 0,
                  Date().timeIntervalSince(m.tapStarted) > Self.tapCheckSeconds {
            log("no sound buffers from the tap, retrying with the output device as clock")
            startTap(m, m.tap.processes, withClock: true)
        }
        if let app = m.appMic, app != m.mic.device, !m.notifiedMismatch {
            m.notifiedMismatch = true
            notify("Meeting mic mismatch", "\(m.name) uses \(AudioSystem.name(app)), kim-rag records \(m.micName)")
        }
        publish()
    }

    private func startMic(_ m: Meeting, device: AudioDeviceID) {
        let name = AudioSystem.name(device)
        m.micName = name
        m.notifiedMismatch = false
        m.mic.start(device: device) { [weak self, weak m] failure in
            guard let self, let m else { return }
            if let failure {
                m.micError = "\(failure)"
                m.micRetry = Date().addingTimeInterval(10)
                self.log("mic \(name): \(failure)")
            } else {
                m.micError = nil
                self.log("recording mic \(name)")
            }
            self.publish()
        }
    }

    private func startTap(_ m: Meeting, _ objects: Set<AudioObjectID>, withClock: Bool) {
        do {
            try m.tap.start(processes: objects, withClock: withClock)
            m.tapError = nil
        } catch {
            m.tapError = "\(error)"
            log("app sound: \(error)")
        }
        m.tapStarted = Date()
    }

    // MARK: - Writing chunks

    private func write() {
        guard let m = meeting else { return }
        let rate = Double(MeetingMixer.rate)
        let due = Date().timeIntervalSince(m.origin) - Self.lagSeconds
        if due - Double(m.mixer.taken) / rate > Self.jumpSeconds {
            // The Mac slept or hung: start a new chunk and a new timeline instead of writing silence.
            closeChunk(m, reason: "gap", last: false)
            m.mixer.reset()
            m.origin = Date()
            openChunk(m, at: m.origin)
            return
        }
        append(m, m.mixer.take(upTo: Int(due * rate)))
        if Double(m.frames) >= config().meetingChunkSeconds * rate {
            closeChunk(m, reason: "rotate", last: false)
            openChunk(m, at: m.origin.addingTimeInterval(Double(m.mixer.taken) / rate))
        }
    }

    private func append(_ m: Meeting, _ data: Data) {
        guard !data.isEmpty else { return }
        try? m.file?.write(contentsOf: data)
        m.frames += data.count / 4
    }

    private func pcmURL(_ chunk: MeetingChunk) -> URL {
        pending.appendingPathComponent("\(chunk.meetingId)-\(chunk.index).pcm")
    }

    private func openChunk(_ m: Meeting, at start: Date) {
        if m.file != nil { m.chunk.index += 1 }
        m.chunk.startedAt = start
        m.chunk.mic = m.micName
        m.frames = 0
        let url = pcmURL(m.chunk)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        m.file = try? FileHandle(forWritingTo: url)
        save(m.chunk, next: url)
    }

    private func closeChunk(_ m: Meeting, reason: String, last: Bool) {
        try? m.file?.close()
        var chunk = m.chunk
        chunk.endReason = reason
        chunk.last = last
        finish(chunk, pcm: pcmURL(chunk))
    }

    private func save(_ chunk: MeetingChunk, next url: URL) {
        let enc = JSONEncoder()
        enc.dateEncodingStrategy = .iso8601
        try? enc.encode(chunk).write(to: url.deletingPathExtension().appendingPathExtension("json"))
    }

    /// Encode a finished PCM chunk to AAC in the outbox (WAV if the encoder fails), then upload it.
    private func finish(_ chunk: MeetingChunk, pcm: URL) {
        let box = outbox
        encoder.async { [weak self] in
            defer {
                try? FileManager.default.removeItem(at: pcm)
                try? FileManager.default.removeItem(at: pcm.deletingPathExtension().appendingPathExtension("json"))
            }
            guard let data = try? Data(contentsOf: pcm), data.count >= 4 * MeetingMixer.rate else { return } // < 1 s
            let enc = JSONEncoder()
            enc.dateEncodingStrategy = .iso8601
            box.add(pcm: data, channels: 2, bitRate: 48_000, info: (try? enc.encode(chunk)) ?? Data(),
                    base: "\(chunk.meetingId)-\(chunk.index)")
            DispatchQueue.main.async { self?.outbox.upload() }
        }
    }

    /// End the meeting: write what is left, close the last chunk and tell the server.
    func stop(reason: String) {
        guard let m = meeting else { return }
        meeting = nil
        m.mic.stop()
        m.tap.stop()
        append(m, m.mixer.take(upTo: Int(Date().timeIntervalSince(m.origin) * Double(MeetingMixer.rate))))
        closeChunk(m, reason: reason, last: true)
        let seconds = Int(Date().timeIntervalSince(m.started))
        queue.add(type: "meeting_end", payload: ["meeting_id": m.id, "app": m.name, "seconds": seconds, "reason": reason])
        log("meeting in \(m.name) ended (\(reason)) after \(seconds / 60) min")
        publish()
    }

    /// Chunks left in `pending` by a crash: send them, and tell the server those meetings are over.
    private func recoverLeftovers() {
        let files = (try? FileManager.default.contentsOfDirectory(at: pending, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .iso8601
        var ended: [String: Date] = [:]
        for pcm in files where pcm.pathExtension == "pcm" {
            let json = pcm.deletingPathExtension().appendingPathExtension("json")
            guard var chunk = (try? Data(contentsOf: json)).flatMap({ try? dec.decode(MeetingChunk.self, from: $0) }) else {
                try? FileManager.default.removeItem(at: pcm)
                continue
            }
            let modified = (try? pcm.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? Date()
            chunk.endReason = "app_killed"
            chunk.last = true
            finish(chunk, pcm: pcm)
            ended[chunk.meetingId] = max(ended[chunk.meetingId] ?? .distantPast, modified)
        }
        for (id, at) in ended {
            queue.add(type: "meeting_end", payload: ["meeting_id": id, "reason": "app_killed"], at: at)
            log("recovered an unfinished meeting \(id.prefix(8))")
        }
    }

    // MARK: - What the panel shows

    private func appLabel(_ m: Meeting) -> String {
        Self.browsers.contains(m.key) ? "\(m.name) (all tabs)" : m.name
    }

    private func publish() {
        model.meetingWaiting = outbox.waiting
        guard let m = meeting else {
            if model.meeting != nil {
                model.meeting = nil
                onChange()
            }
            return
        }
        var s = model.meeting ?? MeetingStatus(app: m.name, started: m.started)
        s.app = m.name
        s.started = m.started
        s.mic = m.micName
        s.appMic = m.appMic.map(AudioSystem.name)
        s.micMatches = m.appMic.map { $0 == m.mic.device }
        s.micError = m.micError
        s.sound = appLabel(m)
        s.soundError = m.tapError
        let changed = model.meeting?.problem != s.problem || model.meeting == nil
        model.meeting = s
        if changed { onChange() }
    }

    private func meters() {
        guard let m = meeting, model.meeting != nil else { return }
        let (mic, app) = m.mixer.levels()
        guard panelVisible() else { return }
        model.meeting?.micLevel = max(mic, (model.meeting?.micLevel ?? 0) * 0.7)
        model.meeting?.soundLevel = max(app, (model.meeting?.soundLevel ?? 0) * 0.7)
    }

    private func notify(_ title: String, _ body: String) {
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }
}
