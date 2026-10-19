import AudioToolbox
import CoreAudio
import Foundation
import KimRagCore

/// A process that Core Audio knows about: which app it belongs to and whether it is using the mic.
struct AudioProcess {
    var object: AudioObjectID
    var pid: pid_t
    var bundle: String
    var input: Bool
    var output: Bool
    /// Input devices the process is reading from right now.
    var inputDevices: [AudioDeviceID]
    /// Output devices the process is playing on right now.
    var outputDevices: [AudioDeviceID] = []
}

/// Small typed wrappers over the Core Audio property API.
enum AudioSystem {
    static let system = AudioObjectID(kAudioObjectSystemObject)

    private static func address(_ selector: AudioObjectPropertySelector,
                                _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func value<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ fallback: T,
                         scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> T {
        var addr = address(selector, scope)
        var out = fallback
        var size = UInt32(MemoryLayout<T>.size)
        return AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &out) == noErr ? out : fallback
    }

    static func ids(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                    scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [AudioObjectID] {
        var addr = address(selector, scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var out = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &out) == noErr else { return [] }
        return out
    }

    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = address(selector)
        var out: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &out) == noErr else { return nil }
        return out?.takeRetainedValue() as String?
    }

    /// Every process doing audio, except this one.
    static func processes() -> [AudioProcess] {
        let me = ProcessInfo.processInfo.processIdentifier
        return ids(system, kAudioHardwarePropertyProcessObjectList).compactMap { obj in
            let pid: pid_t = value(obj, kAudioProcessPropertyPID, -1)
            guard pid != me else { return nil }
            let input: UInt32 = value(obj, kAudioProcessPropertyIsRunningInput, 0)
            let output: UInt32 = value(obj, kAudioProcessPropertyIsRunningOutput, 0)
            return AudioProcess(
                object: obj, pid: pid, bundle: string(obj, kAudioProcessPropertyBundleID) ?? "",
                input: input != 0, output: output != 0,
                inputDevices: input != 0 ? ids(obj, kAudioProcessPropertyDevices, scope: kAudioObjectPropertyScopeInput) : [],
                outputDevices: output != 0 ? ids(obj, kAudioProcessPropertyDevices, scope: kAudioObjectPropertyScopeOutput) : [])
        }
    }

    static func defaultInput() -> AudioDeviceID {
        value(system, kAudioHardwarePropertyDefaultInputDevice, AudioDeviceID(kAudioObjectUnknown))
    }

    static func defaultOutput() -> AudioDeviceID {
        value(system, kAudioHardwarePropertyDefaultOutputDevice, AudioDeviceID(kAudioObjectUnknown))
    }

    static func name(_ device: AudioDeviceID) -> String {
        string(device, kAudioObjectPropertyName) ?? "?"
    }

    static func uid(_ device: AudioDeviceID) -> String? {
        string(device, kAudioDevicePropertyDeviceUID)
    }

    /// An output device as the sound check sees it: name, transport, built-in data source, mute and volume.
    static func output(_ device: AudioDeviceID) -> SoundOutput {
        let out = kAudioDevicePropertyScopeOutput
        let transport: UInt32 = value(device, kAudioDevicePropertyTransportType, 0)
        let source: UInt32 = value(device, kAudioDevicePropertyDataSource, 0, scope: out)
        let mute: UInt32 = value(device, kAudioDevicePropertyMute, 0, scope: out)
        let volume: Float32 = value(device, kAudioHardwareServiceDeviceProperty_VirtualMainVolume, -1, scope: out)
        return SoundOutput(name: name(device), transport: fourCC(transport), dataSource: source == 0 ? nil : fourCC(source),
                           muted: mute != 0, volume: volume < 0 ? nil : volume)
    }

    static func fourCC(_ code: UInt32) -> String {
        String(decoding: [24, 16, 8, 0].map { UInt8((code >> $0) & 0xff) }, as: UTF8.self)
    }

    /// Every device with a microphone.
    static func inputDevices() -> [AudioDeviceID] {
        ids(system, kAudioHardwarePropertyDevices).filter(hasInput)
    }

    static func transport(_ device: AudioDeviceID) -> UInt32 {
        value(device, kAudioDevicePropertyTransportType, 0)
    }

    /// Whether the device has input channels (a microphone), not just speakers.
    static func hasInput(_ device: AudioDeviceID) -> Bool {
        !ids(device, kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput).isEmpty
    }
}
