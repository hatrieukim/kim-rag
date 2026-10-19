import AppKit

// Chương trình ghi hoạt động trên Mac cho kim-rag (Sản phẩm 4). Chạy nền với một icon trên thanh menu,
// and since product 11 a main window to look at the memory. The Dock icon shows only while that window is open.
let app = NSApplication.shared
// Opened again from Finder or Launchpad while launchd already runs it: show that copy's window and leave, so
// two copies never record the same Mac.
let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
if others.contains(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
    DistributedNotificationCenter.default().postNotificationName(Recorder.openWindowNote, object: nil,
                                                                 deliverImmediately: true)
    exit(0)
}
app.setActivationPolicy(.accessory)
let recorder = Recorder()
app.delegate = recorder
app.run()
