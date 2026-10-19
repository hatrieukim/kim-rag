import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct KimRagWidgetBundle: WidgetBundle {
    var body: some Widget {
        RecorderLiveActivity()
    }
}

/// Lock screen: a quiet one-line strip. Someone glancing at the phone sees "Kim Rag" and a dot, nothing more.
struct RecorderLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: RecorderAttributes.self) { context in
            HStack(spacing: 10) {
                Dot(on: context.state.capturing, size: 6)
                Text("Kim Rag")
                    .font(.system(size: 14, weight: .medium, design: .default))
                    .tracking(0.3)
                    .foregroundStyle(.white.opacity(0.78))
                Spacer()
                Button(intent: EndConversationIntent()) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 13, weight: .regular))
                        .foregroundStyle(.white.opacity(0.6))
                        .frame(width: 32, height: 32)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 8)
            .activityBackgroundTint(Color.black.opacity(0.35))
            .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 8) {
                        Dot(on: context.state.capturing, size: 6)
                        Text("Kim Rag").font(.system(size: 14, weight: .medium)).foregroundStyle(.white.opacity(0.78))
                    }
                    .padding(.leading, 6)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Button(intent: EndConversationIntent()) {
                        Image(systemName: "checkmark").foregroundStyle(.white.opacity(0.6))
                    }
                    .buttonStyle(.plain)
                }
            } compactLeading: {
                Dot(on: context.state.capturing, size: 5)
            } compactTrailing: {
                EmptyView()
            } minimal: {
                Dot(on: context.state.capturing, size: 5)
            }
        }
    }
}

private struct Dot: View {
    var on: Bool
    var size: CGFloat

    var body: some View {
        Circle()
            .strokeBorder(.white.opacity(0.7), lineWidth: on ? 0 : 1)
            .background(Circle().fill(.white.opacity(on ? 0.7 : 0)))
            .frame(width: size, height: size)
    }
}
