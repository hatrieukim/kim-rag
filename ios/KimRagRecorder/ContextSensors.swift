import CallKit
import CoreLocation
import CoreMotion
import UIKit

/// Context stored with each chunk: rough location, motion, phone calls, battery.
/// A fresh place (about 100 m, mostly from Wi-Fi and cell towers) is asked for only after a chunk in which the
/// phone moved; while it stays still the last fix is reused, so a day at the desk costs one fix. The app is
/// already awake in the background for the microphone; with "Always" access this shows no blue pill.
/// Significant-change monitoring stays on as well: it gives a free place after a real move, and relaunches
/// the app after the system closes it, which lets recording resume.
final class ContextSensors: NSObject, CLLocationManagerDelegate, CXCallObserverDelegate {
    private let location = CLLocationManager()
    private let motion = CMMotionActivityManager()
    private let calls = CXCallObserver()
    private(set) var lastPlace: Sidecar.Place?
    /// The phone has moved (or may have) since `lastPlace` was fixed, so that place no longer stands for now.
    private var movedSinceFix = true
    private(set) var inCall = false

    var onCallChange: ((Bool) -> Void)?
    var onRelaunchHint: (() -> Void)?

    override init() {
        super.init()
        location.delegate = self
        location.desiredAccuracy = kCLLocationAccuracyHundredMeters
        location.allowsBackgroundLocationUpdates = true
        location.showsBackgroundLocationIndicator = false
        location.pausesLocationUpdatesAutomatically = false
        calls.setDelegate(self, queue: .main)
        UIDevice.current.isBatteryMonitoringEnabled = true
    }

    func start() {
        switch location.authorizationStatus {
        case .notDetermined: location.requestWhenInUseAuthorization()
        case .authorizedWhenInUse: location.requestAlwaysAuthorization()
        default: break
        }
        if CLLocationManager.significantLocationChangeMonitoringAvailable() {
            location.startMonitoringSignificantLocationChanges()
        }
        location.requestLocation()
        inCall = calls.calls.contains { !$0.hasEnded }
    }

    /// Ask for a fresh place; it arrives in a few seconds and is stored with the chunk being recorded.
    func refreshPlace() {
        location.requestLocation()
    }

    /// A chunk's motion came in: after any movement (or when motion is unknown, or an earlier fix never
    /// arrived) ask for a fresh place for the chunk that just started. "still" keeps the last fix.
    func noteMotion(_ motion: String?) {
        if motion != "still" { movedSinceFix = true }
        if movedSinceFix { refreshPlace() }
    }

    /// The last place, when it still holds: fixed after `since`, or nothing has moved since it was fixed.
    /// An older one from before a move would put the chunk somewhere else.
    func place(since: Date) -> Sidecar.Place? {
        guard let lastPlace, lastPlace.at >= since || !movedSinceFix else { return nil }
        return lastPlace
    }

    /// Without "Always", iOS gives no place while the screen is off.
    var locationAlways: Bool { location.authorizationStatus == .authorizedAlways }

    var battery: Float? {
        let level = UIDevice.current.batteryLevel
        return level < 0 ? nil : (level * 100).rounded() / 100
    }

    var charging: Bool {
        [.charging, .full].contains(UIDevice.current.batteryState)
    }

    /// The main motion activity between two times, or nil when unknown or not allowed.
    func activity(from: Date, to: Date, done: @escaping (String?) -> Void) {
        guard CMMotionActivityManager.isActivityAvailable(),
              CMMotionActivityManager.authorizationStatus() != .denied else { return done(nil) }
        motion.queryActivityStarting(from: from, to: to, to: .main) { list, _ in
            var time: [String: TimeInterval] = [:]
            let items = list ?? []
            for (i, a) in items.enumerated() {
                let end = i + 1 < items.count ? items[i + 1].startDate : to
                let span = end.timeIntervalSince(max(a.startDate, from))
                guard span > 0 else { continue }
                let name = a.automotive ? "automotive" : a.cycling ? "cycling" : a.running ? "running"
                    : a.walking ? "walking" : a.stationary ? "still" : nil
                if let name { time[name, default: 0] += span }
            }
            done(time.max { $0.value < $1.value }?.key)
        }
    }

    // MARK: CLLocationManagerDelegate

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if manager.authorizationStatus == .authorizedWhenInUse { manager.requestAlwaysAuthorization() }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let l = locations.last else { return }
        lastPlace = .init(lat: (l.coordinate.latitude * 1e5).rounded() / 1e5,
                          lon: (l.coordinate.longitude * 1e5).rounded() / 1e5,
                          accuracy: l.horizontalAccuracy.rounded(), at: l.timestamp)
        movedSinceFix = false
        onRelaunchHint?()
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    // MARK: CXCallObserverDelegate

    func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        let now = callObserver.calls.contains { !$0.hasEnded }
        if now != inCall {
            inCall = now
            onCallChange?(now)
        }
    }
}
