import AVFoundation
import Foundation
import KimRagCore

/// Finished audio chunks waiting in a folder (`<name>.m4a` or `<name>.wav` next to `<name>.json`), uploaded one at
/// a time to `POST /chunks`, oldest first, and deleted once the server has them. Used by the meeting recorder and
/// the room recorder.
final class ChunkOutbox {
    let dir: URL
    private let failed: URL
    private let what: String
    private let config: () -> Config
    /// The `metadata` form field, from the JSON file next to the audio.
    private let metadata: (Data) -> [String: Any]?
    private let log: (String) -> Void
    private var uploading = false
    /// Called on the main queue after each upload attempt.
    var onChange: () -> Void = {}

    init(dir: URL, failed: URL, what: String, config: @escaping () -> Config,
         metadata: @escaping (Data) -> [String: Any]?, log: @escaping (String) -> Void) {
        self.dir = dir
        self.failed = failed
        self.what = what
        self.config = config
        self.metadata = metadata
        self.log = log
        for d in [dir, failed] { try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true) }
    }

    var waiting: Int {
        ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []).filter { $0.hasSuffix(".json") }.count
    }

    /// Encode raw 16-bit PCM to AAC at `base`.m4a (WAV if the encoder fails) and write `info` next to it as JSON.
    /// Runs on any queue; the caller uploads afterwards.
    func add(pcm: Data, channels: Int, bitRate: Int, info: Data, base name: String) {
        let base = dir.appendingPathComponent(name)
        let audio = base.appendingPathExtension("m4a")
        do {
            try Self.encodeAAC(pcm, channels: channels, bitRate: bitRate, to: audio)
        } catch {
            log("AAC encoding failed (\(error)), sending WAV")
            try? FileManager.default.removeItem(at: audio)
            try? Wav.wrap(pcm, channels: channels, rate: MeetingMixer.rate).write(to: base.appendingPathExtension("wav"))
        }
        try? info.write(to: base.appendingPathExtension("json"))
    }

    static func encodeAAC(_ pcm: Data, channels: Int, bitRate: Int, to url: URL) throws {
        let frames = pcm.count / (2 * channels)
        guard let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(MeetingMixer.rate),
                                         channels: AVAudioChannelCount(channels), interleaved: true),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
              let dest = buffer.int16ChannelData?[0]
        else { throw CaptureError("no PCM buffer") }
        buffer.frameLength = AVAudioFrameCount(frames)
        pcm.withUnsafeBytes { raw in
            if let src = raw.baseAddress { memcpy(dest, src, frames * 2 * channels) }
        }
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: MeetingMixer.rate, AVNumberOfChannelsKey: channels,
            AVEncoderBitRateKey: bitRate,
        ]
        try? FileManager.default.removeItem(at: url)
        let file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatInt16, interleaved: true)
        try file.write(from: buffer)
    }

    func upload() {
        guard !uploading else { return }
        let files = ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        guard let json = files.first,
              let meta = (try? Data(contentsOf: json)).flatMap(metadata),
              let audio = ["m4a", "wav"].map({ json.deletingPathExtension().appendingPathExtension($0) })
                .first(where: { FileManager.default.fileExists(atPath: $0.path) }),
              let body = try? Data(contentsOf: audio),
              let url = URL(string: config().server + "/chunks")
        else { return }
        let boundary = "kimrag-\(UUID().uuidString)"
        var form = Data()
        func field(_ header: String, _ content: Data) {
            form.append(Data("--\(boundary)\r\n\(header)\r\n\r\n".utf8))
            form.append(content)
            form.append(Data("\r\n".utf8))
        }
        let metaData = (try? JSONSerialization.data(withJSONObject: meta, options: [.sortedKeys])) ?? Data("{}".utf8)
        field("Content-Disposition: form-data; name=\"metadata\"", metaData)
        field("Content-Disposition: form-data; name=\"audio\"; filename=\"\(audio.lastPathComponent)\"\r\n"
              + "Content-Type: \(audio.pathExtension == "wav" ? "audio/wav" : "audio/mp4")", body)
        form.append(Data("--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: url, timeoutInterval: 120)
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let token = config().token
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        uploading = true
        URLSession.shared.uploadTask(with: request, from: form) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.uploading = false
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                switch code {
                case 200, 201:
                    try? FileManager.default.removeItem(at: audio)
                    try? FileManager.default.removeItem(at: json)
                    self.upload()
                case 400, 413, 422:
                    let detail = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                    self.log("server refused \(self.what) chunk \(audio.lastPathComponent) (HTTP \(code)): \(detail.prefix(300))")
                    for file in [audio, json] {
                        try? FileManager.default.moveItem(at: file, to: self.failed.appendingPathComponent(file.lastPathComponent))
                    }
                default:
                    if let error { self.log("\(self.what) chunk upload failed: \(error.localizedDescription)") }
                }
                self.onChange()
            }
        }.resume()
    }
}
