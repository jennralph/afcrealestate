import XCTest
@testable import HarborCore
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

private func server(_ id: String, _ cc: String, _ city: String, lon: Double = 0, free: Bool = false, load: Int = 0) -> Server {
    Server(id: id, countryCode: cc, country: cc.uppercased(), city: city, longitude: lon,
           ipv4: "198.51.100.1", publicKey: "k", free: free, load: load)
}

final class ServerSelectionTests: XCTestCase {
    let servers = [
        server("se1", "se", "Stockholm", lon: 18, free: true, load: 20),
        server("us1", "us", "New York", lon: -74, load: 10),
        server("us2", "us", "New York", lon: -74, load: 90),
        server("jp1", "jp", "Tokyo", lon: 139, load: 30),
    ]

    func testFreeTierOnlySeesFreeServers() {
        let pick = ServerSelection.smart(servers, tier: .free, context: .init(regionCode: "US"))
        XCTAssertEqual(pick?.id, "se1")
    }

    func testPrefersMeasuredLatencyButAvoidsCrowding() {
        // us2 is 10 ms closer but at 90% load: 30*3 = 90 ms penalty.
        let ctx = ServerSelection.Context(latencies: ["us1": 40, "us2": 30, "jp1": 200])
        XCTAssertEqual(ServerSelection.smart(servers, tier: .paid, context: ctx)?.id, "us1")
    }

    func testFallsBackToRegionThenTimeZone() {
        XCTAssertEqual(ServerSelection.smart(servers, tier: .paid, context: .init(regionCode: "US"))?.id, "us1")
        // Someone in a country with no server, at UTC+9, should land in Tokyo.
        XCTAssertEqual(ServerSelection.smart(servers, tier: .paid, context: .init(regionCode: "KR", utcOffsetSeconds: 9 * 3600))?.id, "jp1")
    }

    func testBestWithinLocation() {
        let list = ServerList(servers: servers, dns: .init(standard: [], blockAds: [], blockAdsMalware: []))
        let ny = list.location(id: "us-new-york")!
        XCTAssertEqual(ServerSelection.best(in: ny, tier: .paid, context: .init())?.id, "us1")
        XCTAssertNil(ServerSelection.best(in: ny, tier: .free, context: .init()))
    }

    func testCountriesGrouping() {
        let list = ServerList(servers: servers, dns: .init(standard: [], blockAds: [], blockAdsMalware: []))
        let countries = list.countries()
        XCTAssertEqual(countries.map(\.code), ["jp", "se", "us"])
        XCTAssertEqual(countries.last?.locations.first?.servers.count, 2)
        XCTAssertTrue(countries[1].isFree)
        XCTAssertEqual(Flag.emoji("se"), "🇸🇪")
    }
}

final class SettingsTests: XCTestCase {
    func testDecodingToleratesMissingKeys() throws {
        let s = try JSONDecoder().decode(HarborSettings.self, from: Data(#"{"killSwitch":true,"unknown":1}"#.utf8))
        XCTAssertTrue(s.killSwitch)
        XCTAssertEqual(s.threatProtection, .adsTrackers)
        XCTAssertEqual(s.autoConnect, .untrustedWiFi)
    }

    func testRecentsAreMostRecentFirstAndCapped() {
        var s = HarborSettings()
        for id in ["a", "b", "c", "d", "e", "f", "b"] { s.noteUsed(locationID: id) }
        XCTAssertEqual(s.recents, ["b", "f", "e", "d", "c"])
    }

    func testSharedStoreRoundTrip() {
        let defaults = UserDefaults(suiteName: "harbor-tests-\(UUID().uuidString)")!
        let store = SharedStore(defaults: defaults)
        var s = store.settings
        s.trust("Home Wi-Fi")
        s.trust("Home Wi-Fi")
        store.settings = s
        XCTAssertEqual(store.settings.trustedNetworks, ["Home Wi-Fi"])
        store.snapshot = TunnelSnapshot(connected: true, locationName: "Stockholm, Sweden")
        XCTAssertEqual(store.snapshot.locationName, "Stockholm, Sweden")
    }
}

final class OnDemandPolicyTests: XCTestCase {
    func testOff() {
        var s = HarborSettings()
        s.autoConnect = .off
        XCTAssertNil(OnDemandPolicy.rules(for: s))
    }

    func testUntrustedWiFi() {
        var s = HarborSettings()
        s.autoConnect = .untrustedWiFi
        s.trustedNetworks = ["Home"]
        XCTAssertEqual(OnDemandPolicy.rules(for: s), [
            .init(.disconnect, .wifi, ssids: ["Home"]),
            .init(.connect, .wifi),
            .init(.ignore, .any),
        ])
    }

    func testAlways() {
        var s = HarborSettings()
        s.autoConnect = .always
        XCTAssertEqual(OnDemandPolicy.rules(for: s), [.init(.connect, .any)])
    }

    func testKeyRotation() {
        let t0 = Date(timeIntervalSince1970: 0)
        XCTAssertFalse(KeyRotation.isDue(last: t0, now: t0.addingTimeInterval(6 * 86_400), everyDays: 7))
        XCTAssertTrue(KeyRotation.isDue(last: t0, now: t0.addingTimeInterval(7 * 86_400), everyDays: 7))
        XCTAssertFalse(KeyRotation.isDue(last: t0, now: t0.addingTimeInterval(999 * 86_400), everyDays: 0))
    }
}

final class FormattingTests: XCTestCase {
    func testAccountNumber() {
        XCTAssertEqual(AccountNumber.normalize("1234 5678-9012 3456\n"), "1234567890123456")
        XCTAssertNil(AccountNumber.normalize("1234"))
        XCTAssertNil(AccountNumber.normalize("1234 5678 9012 345a"))
        XCTAssertEqual(AccountNumber.grouped("1234567890123456"), "1234 5678 9012 3456")
        XCTAssertEqual(AccountNumber.masked("1234567890123456"), "•••• •••• •••• 3456")
    }

    func testBytesAndDuration() {
        XCTAssertEqual(Format.bytes(999), "999 B")
        XCTAssertEqual(Format.bytes(1_500), "1.5 KB")
        XCTAssertEqual(Format.bytes(2_000_000), "2 MB")
        XCTAssertEqual(Format.bytes(123_456_789), "123 MB")
        XCTAssertEqual(Format.duration(65), "01:05")
        XCTAssertEqual(Format.duration(3725), "1:02:05")
    }

    func testUAPIStats() {
        let uapi = """
        public_key=abc
        last_handshake_time_sec=1700000000
        last_handshake_time_nsec=0
        rx_bytes=1000
        tx_bytes=500
        """
        let s = TunnelStats.parse(uapi: uapi)
        XCTAssertEqual(s.rxBytes, 1000)
        XCTAssertEqual(s.txBytes, 500)
        XCTAssertEqual(s.lastHandshake, Date(timeIntervalSince1970: 1_700_000_000))
    }

    func testTunnelSpecRoundTrip() throws {
        let spec = TunnelSpec(serverID: "se1", serverPublicKey: "pk", endpoint: "198.51.100.1:51820",
                              addresses: ["10.64.0.8/32"], dns: ["10.64.0.2"], locationName: "Stockholm, Sweden", countryCode: "se")
        XCTAssertEqual(TunnelSpec(providerConfiguration: try spec.providerConfiguration()), spec)
        XCTAssertNil(TunnelSpec(providerConfiguration: nil))
        XCTAssertTrue(spec.redactedConfig().contains("PrivateKey = (stored in Keychain)"))
    }

    func testRFC3339() {
        XCTAssertEqual(RFC3339.parse("2026-10-01T12:00:00Z"), Date(timeIntervalSince1970: 1_790_856_000))
        XCTAssertEqual(RFC3339.parse("2026-10-01T12:00:00.5Z")!.timeIntervalSince1970, 1_790_856_000.5, accuracy: 0.001)
        XCTAssertEqual(RFC3339.parse("2026-10-01T12:00:00.123456789Z")!.timeIntervalSince1970, 1_790_856_000.123, accuracy: 0.001)
        XCTAssertNil(RFC3339.parse("yesterday"))
    }
}

private struct StubTransport: HTTPTransport {
    let status: Int
    let body: String
    let check: @Sendable (URLRequest) -> Void

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        check(request)
        let resp = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        return (Data(body.utf8), resp)
    }
}

final class APIClientTests: XCTestCase {
    let base = URL(string: "https://api.example.net")!

    func testDecodesAccountFromGo() async throws {
        // Shape and timestamps exactly as harbor-api emits them.
        let body = """
        {"tier":"paid","paid_until":"2026-11-01T00:00:00.123456789Z","max_devices":7,"devices":[
          {"id":"ab12","name":"iPhone","public_key":"pk","ipv4_address":"10.64.0.8/32",
           "ipv6_address":"fd68:6172:626f:7200::8/128","created":"2026-10-01T12:00:00Z","key_rotated":"2026-10-01T12:00:00Z"}]}
        """
        let client = APIClient(baseURL: base, transport: StubTransport(status: 200, body: body) { req in
            XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Account 1234567890123456")
            XCTAssertEqual(req.url?.path, "/v1/account")
        })
        let a = try await client.account(number: "1234567890123456")
        XCTAssertEqual(a.tier, .paid)
        XCTAssertEqual(a.devices.first?.ipv6Address, "fd68:6172:626f:7200::8/128")
        XCTAssertNotNil(a.paidUntil)
    }

    func testSurfacesServerMessage() async {
        let client = APIClient(baseURL: base, transport: StubTransport(
            status: 409, body: #"{"code":"device_limit","message":"Remove one."}"#) { _ in })
        do {
            _ = try await client.registerDevice(number: "1", publicKey: "k", name: "n")
            XCTFail("expected error")
        } catch let e as APIError {
            XCTAssertEqual(e.code, "device_limit")
            XCTAssertEqual(e.status, 409)
            XCTAssertEqual(e.errorDescription, "Remove one.")
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testDecodesServerList() async throws {
        let body = """
        {"servers":[{"id":"se1","country_code":"se","country":"Sweden","city":"Stockholm","latitude":59.3,"longitude":18.1,
          "hostname":"se1.example","ipv4":"198.51.100.1","port":51820,"public_key":"pk","free":true,"features":[],"load":12}],
         "dns":{"standard":["10.64.0.1"],"block_ads":["10.64.0.2"],"block_ads_malware":["10.64.0.3"]}}
        """
        let client = APIClient(baseURL: base, transport: StubTransport(status: 200, body: body) { _ in })
        let list = try await client.servers()
        XCTAssertEqual(list.servers.first?.countryCode, "se")
        XCTAssertNil(list.servers.first?.ipv6)
        XCTAssertEqual(list.dns.servers(for: .adsTrackersMalware), ["10.64.0.3"])
    }
}
