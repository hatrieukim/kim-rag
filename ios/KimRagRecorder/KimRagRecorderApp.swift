import SwiftUI
import UIKit

@main
struct KimRagRecorderApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView(service: .shared)
        }
        .onChange(of: phase) { _, now in
            if now == .active { RecorderService.shared.becameActive() }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    /// Also runs when iOS relaunches the app in the background (finished uploads, significant location change).
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        MainActor.assumeIsolated { RecorderService.shared.bootstrap() }
        return true
    }

    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        Uploader.shared.backgroundDone = completionHandler
        Uploader.shared.start()
    }
}
