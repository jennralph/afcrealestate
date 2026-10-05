import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// An error the API explained, with a message fit to show the user.
public struct APIError: Error, Codable, Equatable, LocalizedError, Sendable {
    public let code: String
    public let message: String
    public var status: Int = 0

    public init(code: String, message: String, status: Int = 0) {
        self.code = code
        self.message = message
        self.status = status
    }

    enum CodingKeys: String, CodingKey { case code, message }
    public var errorDescription: String? { message }
}

public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionTransport: HTTPTransport {
    let session: URLSession

    public init(session: URLSession = .shared) { self.session = session }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { cont in
            session.dataTask(with: request) { data, response, error in
                if let error {
                    cont.resume(throwing: error)
                } else if let http = response as? HTTPURLResponse {
                    cont.resume(returning: (data ?? Data(), http))
                } else {
                    cont.resume(throwing: URLError(.badServerResponse))
                }
            }.resume()
        }
    }
}

/// Client for the Harbor control plane (see vpn/backend/internal/api).
public struct APIClient: Sendable {
    public let baseURL: URL
    let transport: HTTPTransport

    public init(baseURL: URL, transport: HTTPTransport = URLSessionTransport()) {
        self.baseURL = baseURL
        self.transport = transport
    }

    public func createAccount() async throws -> AccountInfo {
        try await call("POST", "/v1/accounts")
    }

    public func account(number: String) async throws -> AccountInfo {
        try await call("GET", "/v1/account", number: number)
    }

    public func registerDevice(number: String, publicKey: String, name: String) async throws -> DeviceInfo {
        try await call("POST", "/v1/devices", number: number, body: ["public_key": publicKey, "name": name])
    }

    public func rotateKey(number: String, deviceID: String, publicKey: String) async throws -> DeviceInfo {
        try await call("PUT", "/v1/devices/\(deviceID)/key", number: number, body: ["public_key": publicKey])
    }

    public func removeDevice(number: String, deviceID: String) async throws {
        let _: Empty = try await call("DELETE", "/v1/devices/\(deviceID)", number: number)
    }

    public func servers() async throws -> ServerList {
        try await call("GET", "/v1/servers")
    }

    /// Sends a StoreKit 2 `Transaction.jwsRepresentation` for the server to verify.
    public func submitPurchase(number: String, signedTransaction: String) async throws -> AccountInfo {
        try await call("POST", "/v1/purchases/appstore", number: number,
                       body: ["signed_transaction": signedTransaction])
    }

    // MARK: -

    struct Empty: Decodable {}

    func call<T: Decodable>(_ method: String, _ path: String, number: String? = nil,
                            body: [String: String]? = nil) async throws -> T {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.timeoutInterval = 20
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let number { req.setValue("Account \(number)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await transport.send(req)
        guard (200..<300).contains(resp.statusCode) else {
            var err = (try? JSONDecoder().decode(APIError.self, from: data))
                ?? APIError(code: "http_\(resp.statusCode)", message: "The server returned an error (\(resp.statusCode)).")
            err.status = resp.statusCode
            throw err
        }
        if T.self == Empty.self { return Empty() as! T }
        return try Self.decoder.decode(T.self, from: data)
    }

    public static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        d.dateDecodingStrategy = .custom { decoder in
            let s = try decoder.singleValueContainer().decode(String.self)
            if let date = RFC3339.parse(s) { return date }
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "bad date \(s)"))
        }
        return d
    }()
}

/// Parses the RFC 3339 timestamps Go emits, which may carry 0–9 fractional digits.
public enum RFC3339 {
    public static func parse(_ s: String) -> Date? {
        var base = s
        var fraction = 0.0
        if let dot = s.firstIndex(of: ".") {
            let afterDot = s.index(after: dot)
            let end = s[afterDot...].firstIndex { !$0.isNumber } ?? s.endIndex
            fraction = Double("0." + s[afterDot..<end]) ?? 0
            base = String(s[..<dot]) + String(s[end...])
        }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: base).map { $0.addingTimeInterval(fraction) }
    }
}
