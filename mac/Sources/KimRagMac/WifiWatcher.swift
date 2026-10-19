import CoreLocation
import CoreWLAN
import Foundation
import KimRagCore

/// Reports which Wi-Fi network the Mac is on as `wifi` events ({ssid, bssid}, both absent when not joined), so
/// the server knows where the day happened. Sent at start, after wake and whenever the network changes.
/// macOS hides the network name from apps without Location Services access, so this asks for it once.
final class WifiWatcher: NSObject, CLLocationManagerDelegate {
    static let pollSeconds: TimeInterval = 30

    private let queue: EventQueue
    private let paused: () -> Bool
    private let log: (String) -> Void
    private let location = CLLocationManager()
    private var timer: Timer?
    private var last: [String: String]?

    init(queue: EventQueue, paused: @escaping () -> Bool, log: @escaping (String) -> Void) {
        self.queue = queue
        self.paused = paused
        self.log = log
        super.init()
        location.delegate = self
    }

    func start() {
        if location.authorizationStatus == .notDetermined { location.requestWhenInUseAuthorization() }
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollSeconds, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    /// After sleep the network may have changed while nothing was watching.
    func wake() {
        last = nil
        poll()
    }

    /// Without Location Services macOS returns no name even when joined, which would read as "no Wi-Fi".
    func poll() {
        guard !paused(), location.authorizationStatus == .authorizedAlways else { return }
        let now = current()
        guard now != last else { return }
        last = now
        queue.add(type: "wifi", payload: now)
    }

    private func current() -> [String: String] {
        guard let wifi = CWWiFiClient.shared().interface(), wifi.powerOn() else { return [:] }
        var info: [String: String] = [:]
        info["ssid"] = wifi.ssid()
        info["bssid"] = wifi.bssid()
        return info
    }

    // MARK: CLLocationManagerDelegate

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedAlways: log("Wi-Fi: Location Services allowed, network names are readable")
        case .denied, .restricted: log("Wi-Fi: Location Services denied, network names stay hidden")
        default: return
        }
        poll()
    }
}
