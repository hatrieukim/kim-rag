import ActivityKit
import AppIntents
import Foundation

/// The lock screen status. Deliberately plain: a dot and a word, no "recording", no red, no timer.
/// A filled dot means audio is being captured; a hollow dot means it stopped (call, error) and needs a look.
struct RecorderAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var capturing: Bool
    }
}

/// "This conversation is over, process it now", from the lock screen. Runs inside the app process.
struct EndConversationIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "End conversation"
    static var description = IntentDescription("Send the conversation so far to be processed now.")

    func perform() async throws -> some IntentResult {
        #if !WIDGET_EXTENSION
        await MainActor.run { RecorderService.shared.endConversation() }
        #endif
        return .result()
    }
}
