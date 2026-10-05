import Foundation
import HarborCore
import Network

/// Measures round-trip time to servers without any extra server software:
/// a TCP SYN to the WireGuard port gets an immediate RST from the server's
/// kernel, and the time until "connection refused" is one round trip.
enum LatencyProbe {
    static func measure(_ servers: [Server], timeout: TimeInterval = 2, concurrency: Int = 12) async -> [String: Double] {
        var results: [String: Double] = [:]
        await withTaskGroup(of: (String, Double?).self) { group in
            var next = 0
            while next < min(concurrency, servers.count) {
                let s = servers[next]
                group.addTask { (s.id, await rtt(host: s.ipv4, port: UInt16(clamping: s.port), timeout: timeout)) }
                next += 1
            }
            for await (id, ms) in group {
                if let ms { results[id] = ms }
                if next < servers.count {
                    let s = servers[next]
                    group.addTask { (s.id, await rtt(host: s.ipv4, port: UInt16(clamping: s.port), timeout: timeout)) }
                    next += 1
                }
            }
        }
        return results
    }

    static func rtt(host: String, port: UInt16, timeout: TimeInterval) async -> Double? {
        guard let nwPort = NWEndpoint.Port(rawValue: port) else { return nil }
        let params = NWParameters.tcp
        (params.defaultProtocolStack.transportProtocol as? NWProtocolTCP.Options)?.connectionTimeout = Int(timeout.rounded(.up))
        // Measure the real path, not the tunnel we may already be inside.
        params.prohibitedInterfaceTypes = [.other]
        let conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: params)
        let start = DispatchTime.now()
        let elapsed = { Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000 }

        return await withCheckedContinuation { cont in
            let once = Once()
            let finish: (Double?) -> Void = { value in
                once.run {
                    conn.cancel()
                    cont.resume(returning: value)
                }
            }
            conn.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    finish(elapsed())
                case .waiting(let err), .failed(let err):
                    if case .posix(.ECONNREFUSED) = err { finish(elapsed()) } else { finish(nil) }
                default:
                    break
                }
            }
            conn.start(queue: .global(qos: .utility))
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { finish(nil) }
        }
    }
}

private final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    func run(_ body: () -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard !done else { return }
        done = true
        body()
    }
}
