import Foundation

/// One chunk of the all-day recording from the Mac's own microphone, kept next to its audio until it is uploaded.
/// The Mac records the room this way while the iPhone recorder (product 5) is off; the server treats it like the
/// iPhone recording.
public struct RoomChunk: Codable, Equatable {
    public static let source = "mac-room"
    /// Reasons that end the conversation: the user pressed "End conversation", turned recording off or paused,
    /// or quit the app.
    public static let closing: Set<String> = ["close", "stop", "quit"]

    public var id: String
    public var startedAt: Date
    public var mic: String
    /// rotate | close | stop | quit | sleep | route_change | gap | app_killed
    public var endReason: String?

    public init(id: String = UUID().uuidString.lowercased(), startedAt: Date, mic: String) {
        self.id = id
        self.startedAt = startedAt
        self.mic = mic
    }

    public var clientId: String { "mac-room:\(id)" }

    /// The `metadata` form field of `POST /chunks`.
    public func metadata() -> [String: Any] {
        var m: [String: Any] = [
            "started_at": EventQueue.timestamp(startedAt), "client_id": clientId, "source": Self.source,
            "mic": mic, "device": "mac", "close_episode": endReason.map(Self.closing.contains) ?? false,
        ]
        if let endReason { m["end_reason"] = endReason }
        return m
    }
}
