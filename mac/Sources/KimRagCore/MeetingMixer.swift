import Foundation

/// Puts the two tracks of a meeting side by side: the microphone (left) and the meeting app (right).
///
/// Both tracks arrive as 16 kHz mono from different audio clocks, in bursts, and either may stop for a moment
/// (the mic is being switched, the tap is rebuilt). The mixer follows the wall clock: `take(upTo:)` returns
/// exactly the frames due by then, pads a track that is behind with silence and drops what a track has in excess,
/// so the two channels stay aligned with each other and with real time.
public final class MeetingMixer {
    public static let rate = 16_000
    /// A track may run this far ahead of the clock before its oldest samples are dropped.
    public static let maxLead = rate / 2

    private var tracks: [[Float]] = [[], []]
    private var peaks: [Float] = [0, 0]
    private let lock = NSLock()
    /// Frames handed out so far.
    public private(set) var taken = 0

    public init() {}

    /// Add samples to track 0 (mic) or 1 (app). Safe to call from audio threads.
    public func append(_ samples: [Float], track: Int) {
        guard !samples.isEmpty else { return }
        let rms = (samples.reduce(0) { $0 + $1 * $1 } / Float(samples.count)).squareRoot()
        lock.lock()
        tracks[track].append(contentsOf: samples)
        peaks[track] = max(peaks[track], rms)
        lock.unlock()
    }

    /// Interleaved 16-bit stereo frames from `taken` up to `target`.
    public func take(upTo target: Int) -> Data {
        lock.lock()
        defer { lock.unlock() }
        let n = target - taken
        guard n > 0 else { return Data() }
        var out = [Int16](repeating: 0, count: n * 2)
        for t in 0..<2 {
            let excess = tracks[t].count - n - Self.maxLead
            if excess > 0 { tracks[t].removeFirst(excess) }
            let have = min(n, tracks[t].count)
            for i in 0..<have {
                out[i * 2 + t] = Int16(max(-1, min(1, tracks[t][i])) * Float(Int16.max))
            }
            tracks[t].removeFirst(have)
        }
        taken = target
        return out.withUnsafeBufferPointer { Data(buffer: $0) }
    }

    /// Start a new timeline at frame 0, for example after the Mac slept.
    public func reset() {
        lock.lock()
        tracks = [[], []]
        taken = 0
        lock.unlock()
    }

    /// Loudness of each track since the last call, 0…1 on a 60 dB scale, for the meters.
    public func levels() -> (mic: Double, app: Double) {
        lock.lock()
        let p = peaks
        peaks = [0, 0]
        lock.unlock()
        return (Self.level(p[0]), Self.level(p[1]))
    }

    public static func level(_ rms: Float) -> Double {
        guard rms > 0 else { return 0 }
        let db = 20 * log10(Double(rms))
        return max(0, min(1, (db + 60) / 60))
    }
}

/// A WAV file around raw 16-bit PCM, used when AAC encoding is not available.
public enum Wav {
    public static func wrap(_ pcm: Data, channels: Int, rate: Int) -> Data {
        var d = Data()
        func put<T: FixedWidthInteger>(_ v: T) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        d.append(contentsOf: Array("RIFF".utf8)); put(UInt32(36 + pcm.count))
        d.append(contentsOf: Array("WAVEfmt ".utf8)); put(UInt32(16)); put(UInt16(1)); put(UInt16(channels))
        put(UInt32(rate)); put(UInt32(rate * channels * 2)); put(UInt16(channels * 2)); put(UInt16(16))
        d.append(contentsOf: Array("data".utf8)); put(UInt32(pcm.count))
        d.append(pcm)
        return d
    }
}

/// What the server needs to know about one chunk of a meeting, kept next to its audio until it is uploaded.
public struct MeetingChunk: Codable, Equatable {
    public var meetingId: String
    public var index: Int
    public var startedAt: Date
    public var app: String
    public var bundle: String
    public var mic: String
    public var endReason: String?
    public var last = false

    public init(meetingId: String, index: Int, startedAt: Date, app: String, bundle: String, mic: String) {
        self.meetingId = meetingId
        self.index = index
        self.startedAt = startedAt
        self.app = app
        self.bundle = bundle
        self.mic = mic
    }

    public var clientId: String { "mac-meeting:\(meetingId):\(index)" }

    /// The `metadata` form field of `POST /chunks`.
    public func metadata() -> [String: Any] {
        var m: [String: Any] = [
            "started_at": EventQueue.timestamp(startedAt), "client_id": clientId, "source": "mac-meeting",
            "meeting_id": meetingId, "app": app, "bundle": bundle, "mic": mic, "channels": ["mic", "app"],
            "close_episode": last,
        ]
        if let endReason { m["end_reason"] = endReason }
        return m
    }
}
