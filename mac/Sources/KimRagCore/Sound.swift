import Foundation

/// An output device as the sound check sees it.
public struct SoundOutput: Equatable {
    public var name: String
    /// Core Audio transport type as four letters: "bltn" built-in, "blue" Bluetooth, "hdmi", "usb ", "airp"…
    public var transport: String
    /// The built-in output's data source: "ispk" speakers, "hdpn" the headphone jack.
    public var dataSource: String?
    public var muted: Bool
    /// 0…1, nil when the device has no main volume.
    public var volume: Float?

    public init(name: String, transport: String, dataSource: String? = nil, muted: Bool = false, volume: Float? = nil) {
        self.name = name
        self.transport = transport
        self.dataSource = dataSource
        self.muted = muted
        self.volume = volume
    }
}

/// What the Mac plays out loud right now. Sent as a `sound` event; the server leaves out the iPhone speech of
/// those minutes, because a video or a song is not a conversation.
public struct SoundState: Equatable {
    /// Names of the apps playing out loud, sorted. Empty when nothing is.
    public var apps: [String]
    public var output: String?

    public init(apps: [String] = [], output: String? = nil) {
        self.apps = apps
        self.output = output
    }

    public var on: Bool { !apps.isEmpty }

    /// The event payload. While recording is paused the apps are not named.
    public func payload(paused: Bool) -> [String: Any] {
        var out: [String: Any] = ["on": on]
        if on && !paused { out["apps"] = apps }
        if on, let output { out["output"] = output }
        return out
    }
}

extension Config {
    /// Whether the room hears this device, so the iPhone microphone does too.
    public func isAudible(_ output: SoundOutput) -> Bool {
        if output.muted || (output.volume ?? 1) < 0.01 { return false }
        if output.transport == "bltn" && output.dataSource == "hdpn" { return false }
        return !headphones.contains { output.name.range(of: $0, options: .regularExpression) != nil }
    }

    /// Whether a process's sound is never counted.
    public func ignoresSound(of bundle: String) -> Bool {
        soundIgnoreApps.contains { bundle == $0 || bundle.hasPrefix($0 + ".") }
    }
}
