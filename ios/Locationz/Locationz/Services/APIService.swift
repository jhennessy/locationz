import Foundation

enum APIError: LocalizedError {
    case invalidURL
    case httpError(Int, String)
    case decodingError
    case noToken
    case networkError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid URL"
        case .httpError(let code, let message):
            return "HTTP \(code): \(message)"
        case .decodingError:
            return "Failed to decode response"
        case .noToken:
            return "Not authenticated"
        case .networkError(let error):
            return error.localizedDescription
        }
    }
}

@MainActor
class APIService: ObservableObject {
    static let shared = APIService()

    var baseURL: String {
        didSet {
            UserDefaults.standard.set(baseURL, forKey: "server_base_url")
        }
    }

    @Published var token: String? {
        didSet {
            if let token = token {
                UserDefaults.standard.set(token, forKey: "auth_token")
            } else {
                UserDefaults.standard.removeObject(forKey: "auth_token")
            }
        }
    }

    @Published var currentUser: TokenResponse?

    var isAuthenticated: Bool { token != nil }

    /// When the current token was issued.  Used for proactive refresh before expiry.
    private var tokenIssuedAt: Date? {
        get { UserDefaults.standard.object(forKey: "auth_token_issued_at") as? Date }
        set { UserDefaults.standard.set(newValue, forKey: "auth_token_issued_at") }
    }

    /// Refresh the token proactively after 48 hours (server tokens expire at 72h).
    private let proactiveRefreshAge: TimeInterval = 48 * 3600

    init() {
        self.baseURL = UserDefaults.standard.string(forKey: "server_base_url") ?? "https://locationz.codelook.ch"
        self.token = UserDefaults.standard.string(forKey: "auth_token")
    }

    // MARK: - Generic request helpers

    /// Whether a token refresh (re-login) is already in flight, to avoid concurrent attempts.
    private var isRefreshing = false

    private func makeRequest(path: String, method: String, body: Data? = nil, authenticated: Bool = true) async throws -> Data {
        // Proactively refresh if the token is older than 48h (expires at 72h),
        // so background uploads don't waste their ~10s budget on a 401 round-trip.
        if authenticated, let issued = tokenIssuedAt,
           Date().timeIntervalSince(issued) >= proactiveRefreshAge {
            _ = try? await refreshToken()
        }

        return try await executeRequest(path: path, method: method, body: body, authenticated: authenticated)
    }

    private func executeRequest(path: String, method: String, body: Data?, authenticated: Bool, isRetry: Bool = false) async throws -> Data {
        guard let url = URL(string: "\(baseURL)\(path)") else {
            throw APIError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        if authenticated {
            guard let token = token else { throw APIError.noToken }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        if let body = body {
            request.httpBody = body
        }

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw APIError.networkError(error)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError.httpError(0, "Invalid response")
        }

        if httpResponse.statusCode == 204 {
            return Data()
        }

        // On 401 for authenticated requests, try to re-login with stored credentials
        if httpResponse.statusCode == 401 && authenticated && !isRetry {
            if try await refreshToken() {
                return try await executeRequest(path: path, method: method, body: body, authenticated: authenticated, isRetry: true)
            }
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            let message = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw APIError.httpError(httpResponse.statusCode, message)
        }

        return data
    }

    /// Attempt to re-login using credentials stored in the Keychain.
    /// Returns `true` if a new token was obtained, `false` otherwise.
    private func refreshToken() async throws -> Bool {
        guard !isRefreshing else { return false }
        isRefreshing = true
        defer { isRefreshing = false }

        guard let credentials = KeychainService.loadCredentials() else {
            // No stored credentials — clear session so the user sees the login screen
            self.token = nil
            self.currentUser = nil
            return false
        }

        do {
            let body = try JSONEncoder().encode(LoginRequest(username: credentials.username, password: credentials.password))
            let data = try await executeRequest(path: "/api/login", method: "POST", body: body, authenticated: false, isRetry: true)
            let response = try JSONDecoder().decode(TokenResponse.self, from: data)
            self.token = response.token
            self.currentUser = response
            self.tokenIssuedAt = Date()
            return true
        } catch {
            // Re-login failed (wrong password, server down, etc.) — force logout
            self.token = nil
            self.currentUser = nil
            return false
        }
    }

    // MARK: - Auth

    func login(username: String, password: String) async throws {
        let body = try JSONEncoder().encode(LoginRequest(username: username, password: password))
        let data = try await makeRequest(path: "/api/login", method: "POST", body: body, authenticated: false)
        let response = try JSONDecoder().decode(TokenResponse.self, from: data)
        self.token = response.token
        self.currentUser = response
        self.tokenIssuedAt = Date()
        KeychainService.storeCredentials(username: username, password: password)
    }

    func register(username: String, email: String, password: String) async throws {
        let body = try JSONEncoder().encode(RegisterRequest(username: username, email: email, password: password))
        let data = try await makeRequest(path: "/api/register", method: "POST", body: body, authenticated: false)
        let response = try JSONDecoder().decode(TokenResponse.self, from: data)
        self.token = response.token
        self.currentUser = response
        self.tokenIssuedAt = Date()
        KeychainService.storeCredentials(username: username, password: password)
    }

    func logout() async {
        // Revoke token on server (best-effort)
        if token != nil {
            _ = try? await makeRequest(path: "/api/logout", method: "POST")
        }
        self.token = nil
        self.currentUser = nil
        self.tokenIssuedAt = nil
        KeychainService.clearCredentials()
    }

    // MARK: - Devices

    func fetchDevices() async throws -> [DeviceInfo] {
        let data = try await makeRequest(path: "/api/devices", method: "GET")
        return try JSONDecoder().decode([DeviceInfo].self, from: data)
    }

    func createDevice(name: String, identifier: String) async throws -> DeviceInfo {
        let body = try JSONEncoder().encode(DeviceCreateRequest(name: name, identifier: identifier))
        let data = try await makeRequest(path: "/api/devices", method: "POST", body: body)
        return try JSONDecoder().decode(DeviceInfo.self, from: data)
    }

    func deleteDevice(id: Int) async throws {
        _ = try await makeRequest(path: "/api/devices/\(id)", method: "DELETE")
    }

    // MARK: - Locations

    func uploadLocations(deviceId: Int, locations: [LocationPoint]) async throws -> BatchResponse {
        let batch = LocationBatch(deviceId: deviceId, locations: locations)
        let body = try JSONEncoder().encode(batch)
        let data = try await makeRequest(path: "/api/locations", method: "POST", body: body)
        return try JSONDecoder().decode(BatchResponse.self, from: data)
    }

    // MARK: - Visits

    func fetchVisits(deviceId: Int, limit: Int = 100, startDate: Date? = nil, endDate: Date? = nil) async throws -> [VisitInfo] {
        var path = "/api/visits/\(deviceId)?limit=\(limit)"
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        if let startDate {
            path += "&start_date=\(formatter.string(from: startDate))"
        }
        if let endDate {
            path += "&end_date=\(formatter.string(from: endDate))"
        }
        let data = try await makeRequest(path: path, method: "GET")
        return try JSONDecoder().decode([VisitInfo].self, from: data)
    }

    // MARK: - Places

    func fetchPlaces() async throws -> [PlaceInfo] {
        let data = try await makeRequest(path: "/api/places", method: "GET")
        return try JSONDecoder().decode([PlaceInfo].self, from: data)
    }

    func fetchFrequentPlaces(limit: Int = 20) async throws -> [PlaceInfo] {
        let data = try await makeRequest(path: "/api/places/frequent?limit=\(limit)", method: "GET")
        return try JSONDecoder().decode([PlaceInfo].self, from: data)
    }

    func fetchPlaceVisits(placeId: Int, limit: Int = 100) async throws -> [VisitInfo] {
        let data = try await makeRequest(path: "/api/places/\(placeId)/visits?limit=\(limit)", method: "GET")
        return try JSONDecoder().decode([VisitInfo].self, from: data)
    }

    // MARK: - Positions (live sharing)

    func updatePosition(deviceId: Int, latitude: Double, longitude: Double, altitude: Double?,
                         accuracy: Double?, speed: Double?, timestamp: String) async throws {
        let body = try JSONEncoder().encode(PositionBatchRequest(positions: [
            PositionPointRequest(
                deviceId: deviceId, latitude: latitude, longitude: longitude,
                altitude: altitude, accuracy: accuracy, speed: speed, timestamp: timestamp
            ),
        ]))
        _ = try await makeRequest(path: "/api/positions", method: "POST", body: body)
    }

    func fetchAllPositions() async throws -> [ServerPosition] {
        let data = try await makeRequest(path: "/api/positions", method: "GET")
        return try JSONDecoder().decode([ServerPosition].self, from: data)
    }

    func relayPeerPositions(relayDeviceId: Int, positions: [ServerRelayPosition]) async throws {
        let body = try JSONEncoder().encode(RelayBatchRequest(
            relayedByDeviceId: relayDeviceId,
            positions: positions
        ))
        _ = try await makeRequest(path: "/api/positions/relay", method: "POST", body: body)
    }
}

// MARK: - Position API models

struct PositionPointRequest: Codable {
    let deviceId: Int
    let latitude: Double
    let longitude: Double
    let altitude: Double?
    let accuracy: Double?
    let speed: Double?
    let timestamp: String

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case latitude, longitude, altitude, accuracy, speed, timestamp
    }
}

struct PositionBatchRequest: Codable {
    let positions: [PositionPointRequest]
}

struct ServerPosition: Codable, Identifiable {
    var id: Int { deviceId }
    let userId: Int
    let username: String
    let deviceId: Int
    let deviceName: String
    let latitude: Double
    let longitude: Double
    let altitude: Double?
    let accuracy: Double?
    let speed: Double?
    let timestamp: String
    let updatedAt: String
    let isStale: Bool
    let relayedByDeviceId: Int?

    enum CodingKeys: String, CodingKey {
        case userId = "user_id"
        case username
        case deviceId = "device_id"
        case deviceName = "device_name"
        case latitude, longitude, altitude, accuracy, speed, timestamp
        case updatedAt = "updated_at"
        case isStale = "is_stale"
        case relayedByDeviceId = "relayed_by_device_id"
    }
}

struct ServerRelayPosition: Codable {
    let deviceId: Int
    let latitude: Double
    let longitude: Double
    let altitude: Double?
    let accuracy: Double?
    let speed: Double?
    let timestamp: String

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case latitude, longitude, altitude, accuracy, speed, timestamp
    }
}

struct RelayBatchRequest: Codable {
    let relayedByDeviceId: Int
    let positions: [ServerRelayPosition]

    enum CodingKeys: String, CodingKey {
        case relayedByDeviceId = "relayed_by_device_id"
        case positions
    }
}
