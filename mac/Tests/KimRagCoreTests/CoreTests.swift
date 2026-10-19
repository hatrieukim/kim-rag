import XCTest
@testable import KimRagCore

final class ScrubTests: XCTestCase {
    func testCheKhoaVaMatKhau() {
        let cases = [
            "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            "hf_AbCdEfGhIjKlMnOpQrStUvWxYz",
            "AIzaSyA-1234567890abcdefghijklmnopqrstu",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "AKIAABCDEFGHIJKLMNOP",
        ]
        for key in cases {
            let out = Scrub.clean("export X=\(key) xong")
            XCTAssertFalse(out.contains(key), out)
            XCTAssertTrue(out.contains(Scrub.mask), out)
        }
        XCTAssertEqual(Scrub.clean("password=hunter2hunter2"), "password=\(Scrub.mask)")
        XCTAssertEqual(Scrub.clean("mysql -u root -pS3cretPass db"), "mysql -u root -p\(Scrub.mask) db")
    }

    func testCheSoTheNhungGiuSoThuong() {
        XCTAssertEqual(Scrub.clean("thẻ 4111 1111 1111 1111 nhé"), "thẻ \(Scrub.mask) nhé")
        // Mã số thuế, số hóa đơn không qua phép thử Luhn thì giữ nguyên.
        XCTAssertEqual(Scrub.clean("MST 0312345678, HĐ 1234567890123"), "MST 0312345678, HĐ 1234567890123")
        XCTAssertEqual(Scrub.clean("ssh myserver"), "ssh myserver")
    }
}

final class ConfigTests: XCTestCase {
    func testKhoaThieuLayMacDinh() throws {
        let config = try JSONDecoder().decode(Config.self, from: Data(#"{"idleSeconds": 60}"#.utf8))
        XCTAssertEqual(config.idleSeconds, 60)
        XCTAssertEqual(config.server, Config().server)
        XCTAssertTrue(config.ignoreApps.contains("com.bitwarden.desktop"))
    }

    func testAnTieuDe() {
        let config = Config()
        XCTAssertTrue(config.hidesTitle("Google - Google Chrome (Incognito)"))
        XCTAssertTrue(config.hidesTitle("VietinBank iPay"))
        XCTAssertFalse(config.hidesTitle("main.py — kim-rag"))
    }
}

final class QueueTests: XCTestCase {
    func testGiuTrenDiaVaBoSauKhiGui() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("q-\(UUID().uuidString).jsonl")
        defer { try? FileManager.default.removeItem(at: url) }
        let q = EventQueue(url: url)
        q.add(type: "focus", payload: ["app": "Zalo"])
        q.add(type: "tick")
        XCTAssertEqual(EventQueue(url: url).count, 2)  // mở lại vẫn còn

        let (body, n) = try XCTUnwrap(q.batch(limit: 1))
        XCTAssertEqual(n, 1)
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [[String: Any]])
        XCTAssertEqual(sent.first?["type"] as? String, "focus")
        XCTAssertEqual(sent.first?["source"] as? String, "mac")
        XCTAssertTrue((sent.first?["ts"] as? String)?.contains("T") ?? false)

        q.drop(n)
        let reopened = EventQueue(url: url)
        XCTAssertEqual(reopened.count, 1)
        XCTAssertEqual(reopened.pending().first?["type"] as? String, "tick")
    }
}

final class ShellSpoolTests: XCTestCase {
    func testDocDong() throws {
        let line = "1760421600.25\t3.5\t1\t/Users/me/x\tgit commit -m \"a\\tb\"\\necho \\\\ok"
        let c = try XCTUnwrap(ShellSpool.parse(Substring(line)))
        XCTAssertEqual(c.status, 1)
        XCTAssertEqual(c.seconds, 3.5)
        XCTAssertEqual(c.cwd, "/Users/me/x")
        XCTAssertEqual(c.command, "git commit -m \"a\tb\"\necho \\ok")
        XCTAssertNil(ShellSpool.parse("hỏng"))
    }

    func testDoiTenRoiDocLanSau() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("shell.tsv")
        try "1760421600\t0.1\t0\t/tmp\tls\n".write(to: url, atomically: true, encoding: .utf8)
        XCTAssertEqual(ShellSpool.take(from: url).count, 0)  // lần đầu chỉ đổi tên
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        XCTAssertEqual(ShellSpool.take(from: url).map(\.command), ["ls"])
        XCTAssertEqual(ShellSpool.take(from: url).count, 0)
    }
}

final class CalendarItemTests: XCTestCase {
    let start = Date(timeIntervalSince1970: 1_791_000_000)

    func item(title: String?, location: String? = nil) -> CalendarItem {
        CalendarItem(id: "ABC", title: title, start: start, end: start.addingTimeInterval(3600), allDay: false,
                     location: location, calendar: "Work",
                     attendees: [CalendarPerson(name: "Tuấn", email: "tuan@example.com", status: "accepted", isMe: false)],
                     organizer: nil, config: Config())
    }

    func testPayloadAndStableUid() {
        let a = item(title: "Họp kế hoạch quý", location: "Phòng 3, pass wifi password=abc12345")
        XCTAssertTrue(a.key.hasPrefix("ABC@"))
        XCTAssertEqual(a.payload["title"] as? String, "Họp kế hoạch quý")
        XCTAssertFalse((a.payload["location"] as? String ?? "").contains("abc12345"))
        XCTAssertEqual((a.payload["attendees"] as? [[String: Any]])?.first?["name"] as? String, "Tuấn")
        XCTAssertEqual(a.uid, item(title: "Họp kế hoạch quý", location: "Phòng 3, pass wifi password=abc12345").uid)
        XCTAssertNotEqual(a.uid, item(title: "Họp kế hoạch quý (dời)").uid)
    }

    func testPrivateTitleIsDropped() {
        XCTAssertNil(item(title: "Gọi ngân hàng VCB digibank").payload["title"])
    }

    func testIgnoredCalendars() {
        let config = Config()
        XCTAssertTrue(config.ignoresCalendar("Birthdays"))
        XCTAssertTrue(config.ignoresCalendar("Ngày lễ ở Việt Nam"))
        XCTAssertFalse(config.ignoresCalendar("Work"))
    }

    func testQueueKeepsSourceAndUid() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("q-\(UUID().uuidString).jsonl")
        defer { try? FileManager.default.removeItem(at: url) }
        let queue = EventQueue(url: url)
        let a = item(title: "Họp")
        queue.add(type: "event", payload: a.payload, at: a.start, source: CalendarItem.source, uid: a.uid)
        let ev = try XCTUnwrap(queue.pending().first)
        XCTAssertEqual(ev["source"] as? String, "calendar")
        XCTAssertEqual(ev["uid"] as? String, a.uid)
    }
}

final class MeetingTests: XCTestCase {
    func testMeetingAppsCoverHelperProcesses() {
        let config = Config()
        XCTAssertEqual(config.meetingApp(for: "com.google.Chrome.helper"), "com.google.Chrome")
        XCTAssertEqual(config.meetingApp(for: "com.microsoft.teams2"), "com.microsoft.teams2")
        XCTAssertNil(config.meetingApp(for: "com.google.Chromecast"))
        XCTAssertNil(config.meetingApp(for: "com.apple.Music"))
        let off = try! JSONDecoder().decode(Config.self, from: Data(#"{"meeting": false}"#.utf8))
        XCTAssertFalse(off.meeting)
        XCTAssertEqual(off.meetingApps, config.meetingApps)
    }

    private func frames(_ data: Data) -> [(Int16, Int16)] {
        data.withUnsafeBytes { raw in
            let s = Array(raw.bindMemory(to: Int16.self))
            return stride(from: 0, to: s.count, by: 2).map { (s[$0], s[$0 + 1]) }
        }
    }

    func testMixerKeepsTracksAlignedAndPadsTheLateOne() {
        let mixer = MeetingMixer()
        mixer.append([Float](repeating: 0.5, count: 100), track: 0)
        mixer.append([Float](repeating: -0.5, count: 40), track: 1)
        let out = frames(mixer.take(upTo: 100))
        XCTAssertEqual(out.count, 100)
        XCTAssertEqual(out[0].0, Int16(0.5 * Float(Int16.max)))
        XCTAssertEqual(out[39].1, Int16(-0.5 * Float(Int16.max)))
        XCTAssertEqual(out[40].1, 0)                      // the app track was late: silence, not a shift
        XCTAssertEqual(mixer.taken, 100)
        XCTAssertTrue(mixer.take(upTo: 100).isEmpty)       // nothing new is due
    }

    func testMixerDropsWhatATrackHasInExcess() {
        let mixer = MeetingMixer()
        let ahead = MeetingMixer.maxLead + 300
        mixer.append([Float](repeating: 0.1, count: 100 + ahead), track: 0)
        _ = mixer.take(upTo: 100)
        mixer.append([Float](repeating: 0.2, count: 1), track: 0)
        // 300 old samples were dropped, so what is left is at most the allowed lead.
        let next = frames(mixer.take(upTo: 100 + MeetingMixer.maxLead + 1))
        XCTAssertEqual(next.last!.0, Int16(0.2 * Float(Int16.max)))
    }

    func testLevelsAndReset() {
        let mixer = MeetingMixer()
        mixer.append([Float](repeating: 0.1, count: 160), track: 1)
        let levels = mixer.levels()
        XCTAssertEqual(levels.mic, 0)
        XCTAssertEqual(levels.app, 2.0 / 3.0, accuracy: 0.01)   // -20 dB on a 60 dB scale
        XCTAssertEqual(mixer.levels().app, 0)
        _ = mixer.take(upTo: 50)
        mixer.reset()
        XCTAssertEqual(mixer.taken, 0)
    }

    func testChunkMetadataAndWav() throws {
        var chunk = MeetingChunk(meetingId: "abc", index: 2, startedAt: Date(timeIntervalSince1970: 0), app: "Zoom",
                                 bundle: "us.zoom.xos", mic: "AirPods")
        chunk.last = true
        chunk.endReason = "meeting_end"
        let meta = chunk.metadata()
        XCTAssertEqual(meta["client_id"] as? String, "mac-meeting:abc:2")
        XCTAssertEqual(meta["source"] as? String, "mac-meeting")
        XCTAssertEqual(meta["close_episode"] as? Bool, true)
        XCTAssertEqual(meta["channels"] as? [String], ["mic", "app"])
        let wav = Wav.wrap(Data(count: 8), channels: 2, rate: 16_000)
        XCTAssertEqual(wav.count, 44 + 8)
        XCTAssertEqual(String(data: wav.prefix(4), encoding: .ascii), "RIFF")
    }
}

final class SoundTests: XCTestCase {
    func testOnlyDevicesTheRoomHearsCount() {
        let config = Config()
        XCTAssertTrue(config.isAudible(SoundOutput(name: "MacBook Pro Speakers", transport: "bltn", dataSource: "ispk", volume: 0.5)))
        XCTAssertTrue(config.isAudible(SoundOutput(name: "LG HDR 4K", transport: "hdmi")))
        XCTAssertTrue(config.isAudible(SoundOutput(name: "JBL Flip 6", transport: "blue", volume: 0.3)))
        XCTAssertFalse(config.isAudible(SoundOutput(name: "External Headphones", transport: "bltn", dataSource: "hdpn")))
        XCTAssertFalse(config.isAudible(SoundOutput(name: "Kim's AirPods Pro", transport: "blue")))
        XCTAssertFalse(config.isAudible(SoundOutput(name: "MacBook Pro Speakers", transport: "bltn", muted: true)))
        XCTAssertFalse(config.isAudible(SoundOutput(name: "MacBook Pro Speakers", transport: "bltn", volume: 0)))
    }

    func testIgnoredAppsCoverTheirHelpers() throws {
        let config = try JSONDecoder().decode(Config.self, from: Data(#"{"soundIgnoreApps": ["com.google.Chrome"]}"#.utf8))
        XCTAssertTrue(config.ignoresSound(of: "com.google.Chrome.helper"))
        XCTAssertFalse(config.ignoresSound(of: "com.spotify.client"))
        XCTAssertTrue(config.sound)
    }

    func testPausedPayloadDoesNotNameApps() {
        let state = SoundState(apps: ["Google Chrome"], output: "MacBook Pro Speakers")
        XCTAssertEqual(state.payload(paused: false)["apps"] as? [String], ["Google Chrome"])
        XCTAssertNil(state.payload(paused: true)["apps"])
        XCTAssertEqual(state.payload(paused: true)["on"] as? Bool, true)
        XCTAssertEqual(SoundState().payload(paused: false)["on"] as? Bool, false)
    }
}

final class RoomTests: XCTestCase {
    func testChunkMetadataClosesOnlyOnStop() {
        var chunk = RoomChunk(id: "abc", startedAt: Date(timeIntervalSince1970: 0), mic: "MacBook Pro Microphone")
        var m = chunk.metadata()
        XCTAssertEqual(m["source"] as? String, "mac-room")
        XCTAssertEqual(m["client_id"] as? String, "mac-room:abc")
        XCTAssertEqual(m["close_episode"] as? Bool, false)
        XCTAssertNil(m["end_reason"])
        for (reason, closes) in [("rotate", false), ("sleep", false), ("close", true), ("stop", true), ("quit", true)] {
            chunk.endReason = reason
            m = chunk.metadata()
            XCTAssertEqual(m["end_reason"] as? String, reason)
            XCTAssertEqual(m["close_episode"] as? Bool, closes, reason)
        }
    }

    func testSetKeepsOtherKeys() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("c-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data(#"{"token": "t", "idleSeconds": 60, "custom": [1]}"#.utf8).write(to: url)
        XCTAssertFalse(Config.load(from: url).room)
        try Config.set("room", true, in: url)
        let config = Config.load(from: url)
        XCTAssertTrue(config.room)
        XCTAssertEqual(config.token, "t")
        XCTAssertEqual(config.idleSeconds, 60)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        XCTAssertEqual(json["custom"] as? [Int], [1])
        XCTAssertNil(json["server"])  // defaults are not written into the user's file
    }
}
