import AVFoundation
import CoreAudio
import Foundation
import KimRagCore

/// Records the room all day from the Mac's microphone, the way the iPhone recorder (product 5) does, for the time
/// the iPhone is not recording. Turned on and off from the menu bar panel (`room` in mac.json).
///
/// Audio is written as raw 16 kHz mono PCM in `room/pending`, cut every `roomChunkSeconds`, encoded to AAC in
/// `room/outbox` and uploaded to `POST /chunks` with source "mac-room"; files are deleted once the server has them.
/// Everything is recorded, silence included: the server finds the speech. It also cuts out the minutes a Mac
/// meeting recording covers and what the Mac plays out loud, as it does for the iPhone.
///
/// Each chunk's start is the wall clock when its first samples arrived, so the time stays true across sleep,
/// a mic that stalls or a mic that changes. Recording stops while paused and while the Mac sleeps.
final class RoomRecorder {
    static let pollSeconds: TimeInterval = 2
    static let writeSeconds: TimeInterval = 1
    /// Without a single sample for this long the mic is restarted.
    static let stallSeconds: TimeInterval = 10
    static let retrySeconds: TimeInterval = 10
    /// After this many restarts in a row that brought no sound, wait longer and longer (up to `maxWaitSeconds`).
    static let quickRestarts = 3
    static let maxWaitSeconds: TimeInterval = 300
    /// A mic that has not started after this long is shown as not answering.
    static let startSeconds: TimeInterval = 15
    static let bitRate = 32_000

    private let model: PanelModel
    private let config: () -> Config
    private let paused: () -> Bool
    private let log: (String) -> Void
    /// Whether the panel is open; the meter is only updated then.
    var panelVisible: () -> Bool = { false }

    private let pending = Config.directory.appendingPathComponent("room/pending", isDirectory: true)
    private lazy var outbox: ChunkOutbox = {
        let box = ChunkOutbox(
            dir: Config.directory.appendingPathComponent("room/outbox", isDirectory: true),
            failed: Config.directory.appendingPathComponent("room/failed", isDirectory: true),
            what: "room", config: config, metadata: { data in Self.decode(data)?.metadata() }, log: log)
        box.onChange = { [weak self] in self?.publish() }
        return box
    }()
    private let encoder = DispatchQueue(label: "com.kim.kimrag.room.encode", qos: .utility)
    private lazy var mic: MicCapture = {
        let m = MicCapture { [weak self] in self?.receive($0) }
        m.onChange = { [weak self] in self?.restart(reason: "route_change", why: "the microphone changed") }
        return m
    }()

    /// Filled on the audio thread, emptied by `write`.
    private let lock = NSLock()
    private var samples: [Float] = []
    private var peak: Float = 0
    private var lastSamples = Date()

    private var running = false
    /// Restarts in a row with no sound in between.
    private var stalls = 0
    private var asleep = false
    private var micName = "?"
    private var error: String?
    private var retryAt = Date.distantPast
    private var chunk: RoomChunk?
    private var file: FileHandle?
    private var frames = 0
    private var timers: [Timer] = []

    init(model: PanelModel, config: @escaping () -> Config, paused: @escaping () -> Bool,
         log: @escaping (String) -> Void) {
        self.model = model
        self.config = config
        self.paused = paused
        self.log = log
    }

    func start() {
        try? FileManager.default.createDirectory(at: pending, withIntermediateDirectories: true)
        recoverLeftovers()
        timers = [
            Timer.scheduledTimer(withTimeInterval: Self.pollSeconds, repeats: true) { [weak self] _ in self?.poll() },
            Timer.scheduledTimer(withTimeInterval: Self.writeSeconds, repeats: true) { [weak self] _ in self?.write() },
            Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.outbox.upload() },
            Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.meter() },
        ]
        poll()
        outbox.upload()
    }

    /// Wait for chunks being encoded, so quitting does not leave them half done.
    func flush() {
        encoder.sync {}
    }

    func sleep() {
        asleep = true
        stop(reason: "sleep")
    }

    func wake() {
        asleep = false
        poll()
    }

    /// The user pressed "End conversation": what was recorded so far ends that conversation, and recording goes
    /// on in a new chunk.
    func endConversation() {
        guard running else { return }
        write()
        close(reason: "close")
        log("room recording: conversation ended by hand")
    }

    // MARK: - Following the setting, pause, sleep and the microphone

    /// Recording is on when the setting is on, not paused and not asleep; on, it keeps the right mic running.
    func poll() {
        defer { publish() }
        guard config().room, !paused(), !asleep else {
            stop(reason: asleep ? "sleep" : "stop")
            error = nil
            stalls = 0
            return
        }
        lock.lock()
        let silentFor = Date().timeIntervalSince(lastSamples)
        lock.unlock()
        if mic.starting {
            if silentFor > Self.startSeconds, error == nil {
                error = "\(micName) is not answering"
                log("room recording: \(micName) has not started after \(Int(silentFor)) s")
            }
            return
        }
        guard running else {
            if Date() >= retryAt { begin() }
            return
        }
        if let want = pickMic(), want != mic.device {
            restart(reason: "route_change", why: "switching to \(AudioSystem.name(want))")
        } else if silentFor > Self.stallSeconds {
            stalls += 1
            let why = "no sound from \(micName) for \(Int(silentFor)) s"
            if stalls <= Self.quickRestarts {
                restart(reason: "gap", why: why)
            } else {
                // Restarting a mic that stays silent every few seconds helps nothing; try again later.
                let wait = min(Self.maxWaitSeconds, Self.retrySeconds * pow(2, Double(stalls - Self.quickRestarts)))
                stop(reason: "gap")
                fail("\(why), trying again in \(Int(wait)) s", wait: wait)
            }
        }
    }

    /// The mic named by `roomMic`, else the built-in one, else the default input unless it is a Bluetooth headset
    /// (recording from it all day would switch it to its low-quality call mode).
    func pickMic() -> AudioDeviceID? {
        let inputs = AudioSystem.inputDevices()
        let pattern = config().roomMic
        if !pattern.isEmpty,
           let named = inputs.first(where: { AudioSystem.name($0).range(of: pattern, options: .regularExpression) != nil }) {
            return named
        }
        if let builtIn = inputs.first(where: { AudioSystem.transport($0) == kAudioDeviceTransportTypeBuiltIn }) {
            return builtIn
        }
        let fallback = AudioSystem.defaultInput()
        let bluetooth = [kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE]
        guard fallback != kAudioObjectUnknown, !bluetooth.contains(AudioSystem.transport(fallback)) else { return nil }
        return fallback
    }

    private func begin() {
        guard MicCapture.permission == .authorized else {
            if MicCapture.permission == .notDetermined {
                MicCapture.askPermission { [weak self] ok in
                    self?.log("microphone permission: \(ok ? "granted" : "denied")")
                    self?.retryAt = .distantPast
                    self?.poll()
                }
            }
            fail("Microphone permission is off")
            return
        }
        guard let device = pickMic() else {
            fail("No microphone to record from (Bluetooth headsets are skipped)")
            return
        }
        micName = AudioSystem.name(device)
        lock.lock()
        samples = []
        lastSamples = Date()
        lock.unlock()
        mic.start(device: device) { [weak self] failure in
            guard let self else { return }
            if let failure {
                self.fail("\(self.micName): \(failure)")
            } else {
                self.lock.lock()
                self.samples = []
                self.lastSamples = Date()
                self.lock.unlock()
                self.running = true
                self.error = nil
                self.log("room recording from \(self.micName)")
            }
            self.publish()
        }
    }

    private func fail(_ text: String, wait: TimeInterval = RoomRecorder.retrySeconds) {
        if error != text { log("room recording: \(text)") }
        error = text
        retryAt = Date().addingTimeInterval(wait)
    }

    /// Stop the mic and close the chunk with what is left.
    func stop(reason: String) {
        if mic.starting { mic.stop() }
        guard running else { return }
        mic.stop()
        running = false
        write()
        close(reason: reason)
        log("room recording stopped (\(reason))")
    }

    private func restart(reason: String, why: String) {
        guard running else { return }
        log("room recording: \(why), restarting the mic")
        mic.stop()
        running = false
        write()
        close(reason: reason)
        begin()
        publish()
    }

    // MARK: - Writing chunks

    /// Audio thread.
    private func receive(_ s: [Float]) {
        guard !s.isEmpty else { return }
        let rms = (s.reduce(0) { $0 + $1 * $1 } / Float(s.count)).squareRoot()
        lock.lock()
        samples.append(contentsOf: s)
        peak = max(peak, rms)
        lastSamples = Date()
        lock.unlock()
    }

    private func write() {
        lock.lock()
        let s = samples
        samples = []
        lock.unlock()
        guard !s.isEmpty else { return }
        stalls = 0
        let rate = Double(MeetingMixer.rate)
        if chunk == nil { open(at: Date().addingTimeInterval(-Double(s.count) / rate)) }
        let pcm = s.map { Int16(max(-1, min(1, $0)) * Float(Int16.max)) }
        try? file?.write(contentsOf: pcm.withUnsafeBufferPointer { Data(buffer: $0) })
        frames += s.count
        if Double(frames) >= config().roomChunkSeconds * rate { close(reason: "rotate") }
    }

    private static func name(_ chunk: RoomChunk) -> String {
        "\(Int(chunk.startedAt.timeIntervalSince1970))-\(chunk.id.prefix(8))"
    }

    private func pcmURL(_ chunk: RoomChunk) -> URL {
        pending.appendingPathComponent(Self.name(chunk)).appendingPathExtension("pcm")
    }

    private static func encode(_ chunk: RoomChunk) -> Data {
        let enc = JSONEncoder()
        enc.dateEncodingStrategy = .iso8601
        return (try? enc.encode(chunk)) ?? Data()
    }

    private static func decode(_ data: Data) -> RoomChunk? {
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .iso8601
        return try? dec.decode(RoomChunk.self, from: data)
    }

    private func open(at start: Date) {
        let c = RoomChunk(startedAt: start, mic: micName)
        let url = pcmURL(c)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        file = try? FileHandle(forWritingTo: url)
        frames = 0
        chunk = c
        try? Self.encode(c).write(to: url.deletingPathExtension().appendingPathExtension("json"))
    }

    private func close(reason: String) {
        guard var c = chunk else { return }
        try? file?.close()
        file = nil
        chunk = nil
        c.endReason = reason
        finish(c, pcm: pcmURL(c))
    }

    /// Encode a finished PCM chunk into the outbox, then upload it. Under a second of audio is dropped.
    private func finish(_ chunk: RoomChunk, pcm: URL) {
        let box = outbox
        encoder.async { [weak self] in
            defer {
                try? FileManager.default.removeItem(at: pcm)
                try? FileManager.default.removeItem(at: pcm.deletingPathExtension().appendingPathExtension("json"))
            }
            guard let data = try? Data(contentsOf: pcm), data.count >= 2 * MeetingMixer.rate else { return }
            box.add(pcm: data, channels: 1, bitRate: Self.bitRate, info: Self.encode(chunk), base: Self.name(chunk))
            DispatchQueue.main.async { self?.outbox.upload() }
        }
    }

    /// Chunks left in `pending` when the app was killed: send them as they are.
    private func recoverLeftovers() {
        let files = (try? FileManager.default.contentsOfDirectory(at: pending, includingPropertiesForKeys: nil)) ?? []
        for pcm in files where pcm.pathExtension == "pcm" {
            let json = pcm.deletingPathExtension().appendingPathExtension("json")
            guard var chunk = (try? Data(contentsOf: json)).flatMap(Self.decode) else {
                try? FileManager.default.removeItem(at: pcm)
                continue
            }
            chunk.endReason = "app_killed"
            finish(chunk, pcm: pcm)
            log("recovered an unfinished room chunk \(chunk.id.prefix(8))")
        }
    }

    // MARK: - What the panel shows

    private func publish() {
        let on = config().room
        let status = on ? RoomStatus(recording: running, mic: running ? micName : (pickMic().map(AudioSystem.name) ?? "?"),
                                     error: error) : nil
        if model.roomOn != on { model.roomOn = on }
        if model.room != status { model.room = status }
        let waiting = outbox.waiting
        if model.roomWaiting != waiting { model.roomWaiting = waiting }
    }

    private func meter() {
        lock.lock()
        let p = peak
        peak = 0
        lock.unlock()
        guard panelVisible() else { return }
        let level = running ? MeetingMixer.level(p) : 0
        model.roomLevel = max(level, model.roomLevel * 0.7)
    }
}
