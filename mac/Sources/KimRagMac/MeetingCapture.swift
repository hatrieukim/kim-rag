import AVFoundation
import CoreAudio
import Foundation
import KimRagCore

struct CaptureError: Error, CustomStringConvertible {
    var description: String
    init(_ description: String) { self.description = description }
    init(_ what: String, _ status: OSStatus) { description = "\(what) failed (OSStatus \(status))" }
}

/// Converts any PCM buffer to the 16 kHz mono the meeting mixer works in.
final class Resampler {
    static let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: Double(MeetingMixer.rate),
                                      channels: 1, interleaved: false)!
    private let converter: AVAudioConverter

    init?(from format: AVAudioFormat) {
        guard let c = AVAudioConverter(from: format, to: Self.target) else { return nil }
        c.downmix = true
        converter = c
    }

    func convert(_ buffer: AVAudioPCMBuffer) -> [Float] {
        let ratio = Self.target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 64)
        guard buffer.frameLength > 0, let out = AVAudioPCMBuffer(pcmFormat: Self.target, frameCapacity: capacity)
        else { return [] }
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
        guard error == nil, let data = out.floatChannelData else { return [] }
        return Array(UnsafeBufferPointer(start: data[0], count: Int(out.frameLength)))
    }

    /// The average of every channel of Float32 Core Audio buffers (interleaved or not), as one mono buffer.
    static func mono(_ parts: [AudioBuffer], format: AVAudioFormat) -> AVAudioPCMBuffer? {
        guard let first = parts.first, first.mNumberChannels > 0 else { return nil }
        let frames = Int(first.mDataByteSize) / (4 * Int(first.mNumberChannels))
        guard frames > 0, let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
              let out = pcm.floatChannelData?[0] else { return nil }
        pcm.frameLength = AVAudioFrameCount(frames)
        let channels = Float(parts.reduce(0) { $0 + Int($1.mNumberChannels) })
        for i in 0..<frames { out[i] = 0 }
        for part in parts {
            let stride = Int(part.mNumberChannels)
            guard stride > 0, Int(part.mDataByteSize) >= frames * stride * 4,
                  let data = part.mData?.assumingMemoryBound(to: Float.self) else { continue }
            for i in 0..<frames {
                for c in 0..<stride { out[i] += data[i * stride + c] / channels }
            }
        }
        return pcm
    }
}

/// The sound one app plays (everyone else in the meeting), through a Core Audio process tap.
///
/// Needs the "System Audio Recording Only" permission, not screen recording. A tap mixes the output of the
/// given processes; it gives silence, not an error, while the app plays nothing.
final class AppAudioTap {
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "com.kim.kimrag.meeting.tap", qos: .userInitiated)
    private let lock = NSLock()
    private var buffers = 0
    private let onSamples: ([Float]) -> Void
    private(set) var processes: Set<AudioObjectID> = []
    private(set) var withClock = false

    init(onSamples: @escaping ([Float]) -> Void) {
        self.onSamples = onSamples
    }

    /// Audio buffers received since `start`. Zero after a few seconds means the tap is not working.
    var received: Int {
        lock.lock()
        defer { lock.unlock() }
        return buffers
    }

    /// `withClock` adds the default output device to the aggregate as its clock, for systems where a
    /// tap-only aggregate device does not run.
    func start(processes: Set<AudioObjectID>, withClock: Bool = false) throws {
        stop()
        self.processes = processes
        self.withClock = withClock
        let description = CATapDescription(stereoMixdownOfProcesses: Array(processes))
        description.uuid = UUID()
        description.name = "kim-rag meeting"
        description.muteBehavior = .unmuted
        description.isPrivate = true
        var status = AudioHardwareCreateProcessTap(description, &tap)
        guard status == noErr else { throw CaptureError("creating the process tap", status) }

        let format: AudioStreamBasicDescription = AudioSystem.value(tap, kAudioTapPropertyFormat, AudioStreamBasicDescription())
        guard format.mFormatID == kAudioFormatLinearPCM, format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
              format.mBitsPerChannel == 32, format.mSampleRate > 0,
              let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.mSampleRate, channels: 1,
                                       interleaved: false),
              let resampler = Resampler(from: mono)
        else {
            stop()
            throw CaptureError("the tap has an unexpected format")
        }

        var spec: [String: Any] = [
            kAudioAggregateDeviceNameKey: "kim-rag meeting",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: false,
            kAudioAggregateDeviceSubDeviceListKey: [[String: Any]](),
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString,
                                               kAudioSubTapDriftCompensationKey: true]],
        ]
        if withClock, let output = AudioSystem.uid(AudioSystem.defaultOutput()) {
            spec[kAudioAggregateDeviceMainSubDeviceKey] = output
            spec[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: output]]
        }
        status = AudioHardwareCreateAggregateDevice(spec as CFDictionary, &aggregate)
        guard status == noErr else {
            stop()
            throw CaptureError("creating the aggregate device", status)
        }
        status = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregate, queue) { [weak self] _, input, _, _, _ in
            self?.receive(input, format: format, mono: mono, resampler: resampler)
        }
        guard status == noErr else {
            stop()
            throw CaptureError("creating the IO proc", status)
        }
        status = AudioDeviceStart(aggregate, proc)
        guard status == noErr else {
            stop()
            throw CaptureError("starting the tap", status)
        }
    }

    func stop() {
        if aggregate != kAudioObjectUnknown {
            if let proc {
                AudioDeviceStop(aggregate, proc)
                AudioDeviceDestroyIOProcID(aggregate, proc)
            }
            AudioHardwareDestroyAggregateDevice(aggregate)
        }
        if tap != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tap) }
        proc = nil
        aggregate = AudioObjectID(kAudioObjectUnknown)
        tap = AudioObjectID(kAudioObjectUnknown)
        lock.lock()
        buffers = 0
        lock.unlock()
    }

    /// The tap's stream comes after the streams of the clock device, so it is the last buffer(s) of the list.
    private func receive(_ input: UnsafePointer<AudioBufferList>, format: AudioStreamBasicDescription,
                         mono: AVAudioFormat, resampler: Resampler) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard let last = list.last, last.mData != nil else { return }
        let parts = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
            ? Array(list.suffix(Int(format.mChannelsPerFrame))) : [last]
        guard let pcm = Resampler.mono(parts, format: mono) else { return }
        lock.lock()
        buffers += 1
        lock.unlock()
        onSamples(resampler.convert(pcm))
    }
}

/// The microphone, read straight from one input device through a Core Audio IO proc.
///
/// Not through AVAudioEngine: its input node always runs together with the default output device, so with a
/// Bluetooth headset connected a chosen built-in mic gave no buffers at all, or a format that crashed the tap.
/// Starting and stopping wait on coreaudiod, which can hang; they run on a queue of their own so the menu bar
/// never waits on them.
final class MicCapture {
    private let onSamples: ([Float]) -> Void
    /// Called on the main queue when the device goes away or changes format; the owner restarts the capture.
    var onChange: () -> Void = {}
    /// The device asked for (main thread); unknown when stopped or when starting it failed.
    private(set) var device = AudioDeviceID(kAudioObjectUnknown)
    /// Bumped by every start and stop (main thread); answers about an older start are dropped.
    private var generation = 0
    /// Whether a start is still waiting for coreaudiod (main thread).
    private(set) var starting = false

    private let control = DispatchQueue(label: "com.kim.kimrag.mic.control", qos: .userInitiated)
    private let io = DispatchQueue(label: "com.kim.kimrag.mic.io", qos: .userInitiated)
    /// Control queue only.
    private var running = AudioDeviceID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private var listener: AudioObjectPropertyListenerBlock?
    private static let watched = [
        AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceIsAlive, mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain),
        AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate,
                                   mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain),
        AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeInput,
                                   mElement: kAudioObjectPropertyElementMain),
    ]

    init(onSamples: @escaping ([Float]) -> Void) {
        self.onSamples = onSamples
    }

    static var permission: AVAuthorizationStatus { AVCaptureDevice.authorizationStatus(for: .audio) }

    static func askPermission(then: @escaping (Bool) -> Void) {
        AVCaptureDevice.requestAccess(for: .audio) { ok in DispatchQueue.main.async { then(ok) } }
    }

    /// Start recording `device`, stopping the one before. Returns at once; `done` runs on the main queue with nil
    /// or the error, unless another start or stop came first.
    func start(device: AudioDeviceID, done: @escaping (Error?) -> Void) {
        generation += 1
        let gen = generation
        self.device = device
        starting = true
        control.async {
            self.close()
            var failure: Error?
            do {
                try self.open(device, generation: gen)
            } catch {
                self.close()
                failure = error
            }
            DispatchQueue.main.async {
                guard gen == self.generation else { return }
                self.starting = false
                if failure != nil { self.device = AudioDeviceID(kAudioObjectUnknown) }
                done(failure)
            }
        }
    }

    func stop() {
        generation += 1
        device = AudioDeviceID(kAudioObjectUnknown)
        starting = false
        control.async { self.close() }
    }

    /// Control queue.
    private func open(_ device: AudioDeviceID, generation gen: Int) throws {
        guard Self.permission == .authorized else { throw CaptureError("no microphone permission") }
        let streams = AudioSystem.ids(device, kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput)
        guard let first = streams.first else { throw CaptureError("the device has no microphone") }
        let format: AudioStreamBasicDescription = AudioSystem.value(first, kAudioStreamPropertyVirtualFormat,
                                                                    AudioStreamBasicDescription())
        guard format.mFormatID == kAudioFormatLinearPCM, format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
              format.mBitsPerChannel == 32, format.mSampleRate > 0,
              let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.mSampleRate, channels: 1,
                                       interleaved: false),
              let resampler = Resampler(from: mono)
        else { throw CaptureError("the microphone has no usable format") }

        running = device
        var status = AudioDeviceCreateIOProcIDWithBlock(&proc, device, io) { [weak self] _, input, _, _, _ in
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            guard let self, let pcm = Resampler.mono(Array(list), format: mono) else { return }
            self.onSamples(resampler.convert(pcm))
        }
        guard status == noErr else { throw CaptureError("creating the IO proc", status) }
        status = AudioDeviceStart(device, proc)
        guard status == noErr else { throw CaptureError("starting the microphone", status) }

        // Restart only for a real change: the device is gone, or its rate or streams are not what we opened.
        let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, gen == self.generation else { return }
            let alive: UInt32 = AudioSystem.value(device, kAudioDevicePropertyDeviceIsAlive, 0)
            let rate: Float64 = AudioSystem.value(device, kAudioDevicePropertyNominalSampleRate, 0)
            let now = AudioSystem.ids(device, kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput)
            if alive == 0 || rate != format.mSampleRate || now.first != first { self.onChange() }
        }
        for var addr in Self.watched {
            AudioObjectAddPropertyListenerBlock(device, &addr, .main, listener)
        }
        self.listener = listener
    }

    /// Control queue.
    private func close() {
        guard running != kAudioObjectUnknown else { return }
        if let listener {
            for var addr in Self.watched {
                AudioObjectRemovePropertyListenerBlock(running, &addr, .main, listener)
            }
        }
        if let proc {
            AudioDeviceStop(running, proc)
            AudioDeviceDestroyIOProcID(running, proc)
        }
        listener = nil
        proc = nil
        running = AudioDeviceID(kAudioObjectUnknown)
    }
}
