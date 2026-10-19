import ActivityKit
import AVFoundation
import UIKit
import UserNotifications

/// Keeps recording all day: 5-minute chunks, resume after calls and route changes, upload in the background.
/// Every chunk records why it ended, so the server can explain every gap.
@MainActor
final class RecorderService: ObservableObject {
    static let shared = RecorderService()
    static let chunkSeconds: TimeInterval = 300
    static let retrySeconds: TimeInterval = 5
    static let notifyAfterSeconds: TimeInterval = 60
    /// iOS ends a Live Activity after 8 hours. Opening the app replaces one older than this, so the status
    /// lasts the rest of the day; a closed one cannot be restarted until the app is opened.
    static let renewActivityAfter: TimeInterval = 4 * 3600
    /// Posted by the Mac (`devicectl device notification post`) just before it reinstalls the app.
    static let prepareUpdate = "com.kim.kimrag.recorder.prepare-update"

    enum State { case idle, capturing, interrupted }

    @Published private(set) var state = State.idle
    @Published private(set) var todaySeconds: TimeInterval = 0
    @Published private(set) var pending = 0
    @Published private(set) var lastSent: Date?
    @Published private(set) var uploadError: String?
    /// When the user last ended a conversation by hand; shown briefly as confirmation.
    @Published private(set) var lastEnded: Date?
    @Published private(set) var level: Float = 0
    @Published private(set) var problem: String?
    /// Why the lock screen status is not shown, when it should be.
    @Published private(set) var activityProblem: String?

    private var capture = AudioCapture()
    private let sensors = ContextSensors()
    private var current: Sidecar?
    private var chunkTimer: Timer?
    /// Refreshes the screen once a second; runs only while the app is in front, so the CPU can sleep in the
    /// background between audio buffers.
    private var screenTimer: Timer?
    private var retryTimer: Timer?
    private var interruptedSince: Date?
    /// Since when another app has been playing on the speaker; the server leaves that sound out.
    private var playbackSince: Date?
    private var lastRestart = Date.distantPast
    private var activity: Activity<RecorderAttributes>?
    private var booted = false

    private init() {}

    // MARK: - Lifecycle

    /// Called on every launch, including background relaunches (upload events, significant location change).
    func bootstrap() {
        guard !booted else { return }
        booted = true
        let center = NotificationCenter.default
        center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { note in
            MainActor.assumeIsolated { self.interruption(note) }
        }
        center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { note in
            MainActor.assumeIsolated { self.routeChanged(note) }
        }
        center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { self.restart(reason: "media_reset") }
        }
        center.addObserver(forName: UIApplication.willTerminateNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { self.terminate() }
        }
        // Not only scenePhase: on a cold launch the first .active may come before onChange is watching.
        center.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated {
                self.updateActivity()
                self.startScreenTimer()
            }
        }
        center.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { self.stopScreenTimer() }
        }
        // Another app starting or stopping its sound, instead of asking the audio session every second.
        center.addObserver(forName: AVAudioSession.silenceSecondaryAudioHintNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { self.trackPlayback() }
        }
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            DispatchQueue.main.async { RecorderService.shared.prepareForUpdate() }
        }, Self.prepareUpdate as CFString, nil, .deliverImmediately)
        capture.onConfigurationChange = { [weak self] in self?.restart(reason: "route_change") }
        sensors.onCallChange = { [weak self] inCall in if inCall { self?.current?.call = true } }
        sensors.onRelaunchHint = { [weak self] in self?.resumeIfWanted() }
        Uploader.shared.onChange = { [weak self] in self?.refreshUploads() }

        _ = ChunkStore.recoverLeftovers(except: nil)
        sensors.start()
        Uploader.shared.start()
        activity = Activity<RecorderAttributes>.activities.first
        loadToday()
        if UIApplication.shared.applicationState == .active { startScreenTimer() }
        resumeIfWanted()
        refreshUploads()
    }

    /// The app came to the foreground.
    func becameActive() {
        resumeIfWanted()
        updateActivity()
        Uploader.shared.flush()
    }

    // MARK: - User actions

    func start() {
        AppSettings.wantsRecording = true
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
        AVAudioApplication.requestRecordPermission { granted in
            Task { @MainActor in
                guard granted else {
                    self.problem = "Microphone access is off. Allow it in Settings."
                    AppSettings.wantsRecording = false
                    return
                }
                self.begin()
            }
        }
    }

    func stop() {
        AppSettings.wantsRecording = false
        retryTimer?.invalidate()
        finish(reason: "stop")
        capture.stop()
        playbackSince = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        state = .idle
        interruptedSince = nil
        problem = nil
        endActivity()
    }

    /// The conversation is over: close the chunk now and upload it marked as the last one, so the server
    /// processes the conversation at once instead of waiting for 10 minutes of silence. Recording goes on in a
    /// new chunk, which starts a new conversation.
    func endConversation() {
        guard state == .capturing else { return }
        rotate(reason: "close")
        lastEnded = Date()
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    /// The Mac is about to reinstall the app (weekly certificate renewal): close the chunk being recorded so it
    /// can be uploaded, and remember the reason for the short gap. Recording continues until the app is
    /// replaced, and the Mac launches the new version, which resumes on its own.
    func prepareForUpdate() {
        UserDefaults.standard.set(Date(), forKey: "updatingAt")
        rotate()
    }

    // MARK: - Capture

    private func resumeIfWanted() {
        guard AppSettings.wantsRecording, state != .capturing else { return }
        begin()
    }

    private func begin() {
        do {
            try AudioCapture.configureSession()
            let s = newSidecar()
            try capture.start(writingTo: ChunkStore.recording(s.id))
            current = s
            ChunkStore.save(s)
            state = .capturing
            problem = nil
            interruptedSince = nil
            retryTimer?.invalidate()
            UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ["stopped"])
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["stopped"])
            scheduleRotation()
            trackPlayback()
        } catch {
            capture.stop()
            current.map { ChunkStore.remove($0.id) }
            current = nil
            if state == .idle { state = .interrupted }
            interruptedSince = interruptedSince ?? Date()
            problem = "Cannot use the microphone: \(error.localizedDescription)"
            scheduleRetry()
            notifyIfStillStopped()
        }
        updateActivity()
    }

    private func newSidecar() -> Sidecar {
        var s = Sidecar(id: UUID().uuidString.lowercased(), startedAt: Date())
        s.input = AudioCapture.inputName
        s.batteryStart = sensors.battery
        s.call = sensors.inCall
        return s
    }

    private func scheduleRotation() {
        chunkTimer?.invalidate()
        chunkTimer = Timer.scheduledTimer(withTimeInterval: Self.chunkSeconds, repeats: true) { _ in
            MainActor.assumeIsolated { self.rotate() }
        }
    }

    /// Close the current chunk and continue in a new file without stopping the microphone.
    private func rotate(reason: String = "rotate") {
        guard state == .capturing, current != nil else { return }
        let next = newSidecar()
        do {
            try capture.rotate(to: ChunkStore.recording(next.id))
        } catch {
            return restart(reason: "error")
        }
        finish(reason: reason, at: next.startedAt)
        current = next
        ChunkStore.save(next)
        // Safety net for a playback start or stop whose notification did not reach the app.
        trackPlayback()
    }

    /// Close the current chunk: record why, then hand it to the uploader.
    private func finish(reason: String, at end: Date = Date()) {
        guard var s = current else { return }
        current = nil
        s.endedAt = end
        s.endReason = reason
        s.batteryEnd = sensors.battery
        s.charging = sensors.charging
        s.location = sensors.place(since: s.startedAt.addingTimeInterval(-60))
        s.call = s.call || sensors.inCall
        // Still playing: this chunk gets the part up to its end, the next one the rest.
        if let since = playbackSince { Self.addPlayback(to: &s, from: since, to: end) }
        ChunkStore.save(s)
        addToday(end.timeIntervalSince(s.startedAt), on: s.startedAt)
        sensors.activity(from: s.startedAt, to: end) { motion in
            // Always after the caller has closed the file, even when the answer comes back at once.
            DispatchQueue.main.async {
                s.motion = motion
                self.sensors.noteMotion(motion)
                ChunkStore.save(s)
                try? FileManager.default.moveItem(at: ChunkStore.recording(s.id), to: ChunkStore.audio(s.id))
                Uploader.shared.flush()
            }
        }
    }

    /// Something changed under the engine (route, media services): close the chunk and start again.
    private func restart(reason: String) {
        guard state == .capturing, Date().timeIntervalSince(lastRestart) > 1 else { return }
        lastRestart = Date()
        finish(reason: reason)
        capture.stop()
        playbackSince = nil
        begin()
    }

    private func interruption(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        switch type {
        case .began:
            guard state == .capturing else { return }
            finish(reason: "interruption")
            capture.stop()
            playbackSince = nil
            state = .interrupted
            interruptedSince = Date()
            updateActivity()
        case .ended:
            if AppSettings.wantsRecording { begin() }
        @unknown default:
            break
        }
    }

    private func routeChanged(_ note: Notification) {
        // Headphones in or out moves the other app's sound on or off the speaker.
        trackPlayback()
        guard let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
              let reason = AVAudioSession.RouteChangeReason(rawValue: raw),
              [.newDeviceAvailable, .oldDeviceUnavailable].contains(reason) else { return }
        restart(reason: "route_change")
    }

    private func scheduleRetry() {
        retryTimer?.invalidate()
        retryTimer = Timer.scheduledTimer(withTimeInterval: Self.retrySeconds, repeats: false) { _ in
            MainActor.assumeIsolated {
                guard AppSettings.wantsRecording, self.state != .capturing else { return }
                self.begin()
            }
        }
    }

    private func terminate() {
        guard state == .capturing else { return }
        finish(reason: "app_killed")
        capture.stop()
        playbackSince = nil
    }

    // MARK: - Status

    var locationAlways: Bool { sensors.locationAlways }

    private func startScreenTimer() {
        guard screenTimer == nil else { return }
        screenTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            MainActor.assumeIsolated { self.tick() }
        }
        tick()
    }

    private func stopScreenTimer() {
        screenTimer?.invalidate()
        screenTimer = nil
    }

    /// Once a second while on screen: the orb's level and the "today" counter.
    private func tick() {
        level = state == .capturing ? capture.level : 0
        if state == .capturing { objectWillChange.send() }
    }

    /// Called when another app's sound starts or stops, on route changes and at each rotation; the span is
    /// widened by a second on both sides to cover delivery delay.
    private func trackPlayback() {
        let playing = state == .capturing && AudioCapture.playbackOnSpeaker
        if playing, playbackSince == nil {
            playbackSince = Date()
        } else if !playing, let since = playbackSince {
            playbackSince = nil
            guard var s = current else { return }
            Self.addPlayback(to: &s, from: since, to: Date())
            current = s
            ChunkStore.save(s)
        }
    }

    private static func addPlayback(to s: inout Sidecar, from: Date, to end: Date) {
        let a = max(0, from.timeIntervalSince(s.startedAt) - 1)
        let b = end.timeIntervalSince(s.startedAt) + 1
        guard b > a else { return }
        s.playback = (s.playback ?? []) + [[(a * 10).rounded() / 10, (b * 10).rounded() / 10]]
    }

    /// Loudness of the last few seconds for the wave on screen, oldest first; empty when not recording.
    func recentLoudness() -> [AudioCapture.Loudness] {
        state == .capturing ? capture.recentLoudness() : []
    }

    /// Seconds recorded today, including the chunk in progress.
    var recordedToday: TimeInterval {
        todaySeconds + (current.map { Date().timeIntervalSince(max($0.startedAt, Calendar.current.startOfDay(for: Date()))) } ?? 0)
    }

    /// Recording could not start (often: iOS relaunched the app in the background, where it may not open the
    /// microphone). Schedule a quiet reminder now, so it arrives even if the app is suspended right after;
    /// a successful start cancels it.
    private func notifyIfStillStopped() {
        let center = UNUserNotificationCenter.current()
        center.getPendingNotificationRequests { pending in
            guard !pending.contains(where: { $0.identifier == "stopped" }) else { return }
            let content = UNMutableNotificationContent()
            content.title = "kim"
            content.body = "Paused. Open to continue."
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: Self.notifyAfterSeconds, repeats: false)
            center.add(UNNotificationRequest(identifier: "stopped", content: content, trigger: trigger))
        }
    }

    private func refreshUploads() {
        pending = ChunkStore.pendingCount
        lastSent = Uploader.shared.lastSent ?? lastSent
        uploadError = Uploader.shared.lastError
    }

    private static func dayKey(_ date: Date) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    private func loadToday() {
        let key = Self.dayKey(Date())
        todaySeconds = UserDefaults.standard.double(forKey: "seconds-\(key)")
    }

    private func addToday(_ seconds: TimeInterval, on day: Date) {
        let key = Self.dayKey(day)
        guard key == Self.dayKey(Date()) else { return loadToday() }
        todaySeconds += seconds
        UserDefaults.standard.set(todaySeconds, forKey: "seconds-\(key)")
    }

    // MARK: - Lock screen

    func updateActivity() {
        guard AppSettings.lockScreen, state != .idle else {
            activityProblem = nil
            return endActivity()
        }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            activityProblem = "Live Activities are off for kim. Turn them on in Settings › kim."
            return endActivity()
        }
        let content = ActivityContent(state: RecorderAttributes.ContentState(capturing: state == .capturing),
                                      staleDate: nil)
        let foreground = UIApplication.shared.applicationState == .active
        if let current = activity ?? Activity<RecorderAttributes>.activities.first(where: { $0.activityState == .active }),
           current.activityState == .active,
           !(foreground && Date().timeIntervalSince(activityStarted) > Self.renewActivityAfter) {
            activity = current
            activityProblem = nil
            Task { await current.update(content) }
            return
        }
        endActivity()
        // iOS allows this only in the foreground; in the background it fails and the next opening retries.
        do {
            activity = try Activity.request(attributes: RecorderAttributes(), content: content)
            activityStarted = Date()
            activityProblem = nil
        } catch {
            if foreground { activityProblem = "Lock screen status failed: \(error.localizedDescription)" }
        }
    }

    /// When the current Live Activity was started (it survives app relaunches, so this is stored).
    private var activityStarted: Date {
        get { UserDefaults.standard.object(forKey: "activityStarted") as? Date ?? .distantPast }
        set { UserDefaults.standard.set(newValue, forKey: "activityStarted") }
    }

    private func endActivity() {
        for a in Activity<RecorderAttributes>.activities {
            Task { await a.end(nil, dismissalPolicy: .immediate) }
        }
        activity = nil
    }
}
