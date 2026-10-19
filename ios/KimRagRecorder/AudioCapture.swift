import AVFoundation
import QuartzCore
import Foundation

/// Captures the microphone with AVAudioEngine, converts to 16 kHz mono and writes AAC (ADTS) files.
/// The file can be swapped while capturing, so chunks follow each other without a gap.
final class AudioCapture {
    static let sampleRate: Double = 16_000
    static let fileSettings: [String: Any] = [
        AVFormatIDKey: kAudioFormatMPEG4AAC,
        AVSampleRateKey: sampleRate,
        AVNumberOfChannelsKey: 1,
        AVEncoderBitRateKey: 32_000,
    ]

    private var engine = AVAudioEngine()
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1,
                                          interleaved: false)!
    private var converter: AVAudioConverter?
    private let lock = NSLock()
    private var file: AVAudioFile?
    private(set) var running = false
    /// Last input level, 0...1, for the app's quiet activity indicator.
    private(set) var level: Float = 0
    /// Loudness of the last few seconds for the wave on screen: each captured buffer (about 85 ms) is split into
    /// `slices` parts, so about 47 values a second, each stamped with the time it was heard.
    struct Loudness {
        var time: Double        // end of the slice, in CACurrentMediaTime seconds
        var duration: Double
        var value: Float        // 0...1, see `loudness(rms:)`
    }
    static let slices = 4
    private static let historyCount = 512   // about 11 s
    private var history: [Loudness] = []
    private var historyEnd = 0
    /// The wave rises almost at once with a sound and falls back slowly, like a level meter, so bars do not
    /// flicker between loud and silent slices. Time constants in seconds.
    private static let attack = 0.015
    private static let release = 0.25
    private var envelope: Float = 0

    var onConfigurationChange: (() -> Void)?

    /// Configure the session for long background capture next to other audio (music keeps playing),
    /// on the iPhone's own microphone.
    static func configureSession() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers, .defaultToSpeaker])
        if let builtIn = session.availableInputs?.first(where: { $0.portType == .builtInMic }) {
            try session.setPreferredInput(builtIn)
        }
        try session.setActive(true)
    }

    static var inputName: String {
        AVAudioSession.sharedInstance().currentRoute.inputs.first?.portName ?? "?"
    }

    /// Another app (a video, music) is playing out of the iPhone's own speaker, so the microphone hears it.
    /// Sound in headphones does not reach the microphone and is not counted.
    static var playbackOnSpeaker: Bool {
        let session = AVAudioSession.sharedInstance()
        guard session.isOtherAudioPlaying || session.secondaryAudioShouldBeSilencedHint else { return false }
        return session.currentRoute.outputs.contains { $0.portType == .builtInSpeaker }
    }

    func start(writingTo url: URL) throws {
        try open(url)
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else { throw CaptureError.noInput }
        converter = AVAudioConverter(from: inFormat, to: outFormat)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: inFormat) { [weak self] buffer, when in
            self?.write(buffer, at: when)
        }
        NotificationCenter.default.addObserver(self, selector: #selector(configurationChanged),
                                               name: .AVAudioEngineConfigurationChange, object: engine)
        engine.prepare()
        try engine.start()
        running = true
    }

    /// Close the current file and continue in a new one.
    func rotate(to url: URL) throws {
        try open(url)
    }

    /// Stop capturing and close the file.
    func stop() {
        NotificationCenter.default.removeObserver(self, name: .AVAudioEngineConfigurationChange, object: engine)
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        running = false
        lock.lock()
        close(file)
        file = nil
        history = []
        historyEnd = 0
        envelope = 0
        lock.unlock()
        // A fresh engine picks up the new input format after a route change.
        engine = AVAudioEngine()
    }

    private func open(_ url: URL) throws {
        let next = try AVAudioFile(forWriting: url, settings: Self.fileSettings, commonFormat: .pcmFormatFloat32,
                                   interleaved: false)
        lock.lock()
        let old = file
        file = next
        lock.unlock()
        close(old)
    }

    private func close(_ f: AVAudioFile?) {
        guard let f else { return }
        if #available(iOS 18.0, *) { f.close() }
    }

    private func write(_ buffer: AVAudioPCMBuffer, at when: AVAudioTime) {
        guard let converter else { return }
        let ratio = outFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var fed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0 else { return }
        let frames = Int(out.frameLength)
        let begin = when.isHostTimeValid ? AVAudioTime.seconds(forHostTime: when.hostTime)
            : CACurrentMediaTime() - Double(frames) / outFormat.sampleRate
        var slices: [Loudness] = []
        if let data = out.floatChannelData?[0] {
            var peak: Float = 0
            for part in 0..<Self.slices {
                let from = frames * part / Self.slices, to = frames * (part + 1) / Self.slices
                guard to > from else { continue }
                var sum: Float = 0
                for i in from..<to {
                    peak = max(peak, abs(data[i]))
                    sum += data[i] * data[i]
                }
                let duration = Double(to - from) / outFormat.sampleRate
                let raw = Self.loudness(rms: (sum / Float(to - from)).squareRoot())
                let tau = raw > envelope ? Self.attack : Self.release
                envelope += (raw - envelope) * Float(1 - exp(-duration / tau))
                slices.append(Loudness(time: begin + Double(to) / outFormat.sampleRate, duration: duration,
                                       value: envelope))
            }
            level = peak
        }
        lock.lock()
        try? file?.write(from: out)
        for slice in slices {
            if history.count < Self.historyCount {
                history.append(slice)
            } else {
                history[historyEnd] = slice
            }
            historyEnd = (historyEnd + 1) % Self.historyCount
        }
        lock.unlock()
    }

    /// The recent loudness values, oldest first.
    func recentLoudness() -> [Loudness] {
        lock.lock()
        defer { lock.unlock() }
        guard history.count == Self.historyCount else { return history }
        return Array(history[historyEnd...] + history[..<historyEnd])
    }

    /// RMS on a decibel scale, -60 dB (a quiet room) to 0 dB, as 0...1. Speech near the phone is around 0.5.
    static func loudness(rms: Float) -> Float {
        guard rms > 0 else { return 0 }
        return min(1, max(0, (20 * log10(rms) + 60) / 60))
    }

    @objc private func configurationChanged(_ note: Notification) {
        DispatchQueue.main.async { self.onConfigurationChange?() }
    }

    enum CaptureError: LocalizedError {
        case noInput
        var errorDescription: String? { "No microphone input available" }
    }
}
