import Foundation
import os

@MainActor
class WatchAPIService {
    static let shared = WatchAPIService()

    private static let logger = Logger(subsystem: "ch.codelook.locationz.watch", category: "network")

    private var baseURL: String { WatchConnectivityService.shared.serverURL }
    private var token: String? { WatchConnectivityService.shared.token }

    private func makeRequest(path: String, method: String, body: Data? = nil) async throws -> Data {
        guard let url = URL(string: "\(baseURL)\(path)") else {
            throw URLError(.badURL)
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30

        if let token = token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body = body {
            request.httpBody = body
        }

        let (data, response) = try await URLSession.shared.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse,
              (200...299).contains(httpResponse.statusCode) else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            Self.logger.error("HTTP \(code) from \(path)")
            throw URLError(.badServerResponse)
        }

        return data
    }

    func uploadLocations(deviceId: Int, locations: [LocationPoint]) async throws -> BatchResponse {
        let batch = LocationBatch(deviceId: deviceId, locations: locations)
        let body = try JSONEncoder().encode(batch)
        let data = try await makeRequest(path: "/api/locations", method: "POST", body: body)
        let response = try JSONDecoder().decode(BatchResponse.self, from: data)
        Self.logger.notice("Uploaded \(response.received) points from watch")
        return response
    }

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

    func createDevice(name: String, identifier: String) async throws -> DeviceInfo {
        let body = try JSONEncoder().encode(DeviceCreateRequest(name: name, identifier: identifier))
        let data = try await makeRequest(path: "/api/devices", method: "POST", body: body)
        return try JSONDecoder().decode(DeviceInfo.self, from: data)
    }

    func fetchDevices() async throws -> [DeviceInfo] {
        let data = try await makeRequest(path: "/api/devices", method: "GET")
        return try JSONDecoder().decode([DeviceInfo].self, from: data)
    }

    func fetchVisits(deviceId: Int, limit: Int = 5) async throws -> [VisitInfo] {
        let data = try await makeRequest(path: "/api/visits/\(deviceId)?limit=\(limit)", method: "GET")
        return try JSONDecoder().decode([VisitInfo].self, from: data)
    }
}

struct PositionBatchRequest: Codable {
    let positions: [PositionPointRequest]
}

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
