import Foundation
import CoreLocation
import Combine
import os
import WatchKit

@MainActor
class WatchLocationService: NSObject, ObservableObject {
    static let shared = WatchLocationService()

    private static let logger = Logger(subsystem: "ch.codelook.locationz.watch", category: "location")

    private let locationManager = CLLocationManager()
    private let api = WatchAPIService.shared
    private let connectivity = WatchConnectivityService.shared

    @Published var isTracking = false
    @Published var lastLocation: CLLocation?
    @Published var authorizationStatus: CLAuthorizationStatus = .notDetermined
    @Published var buffer: [LocationPoint] = []
    @Published var uploadError: String?

    // Current location duration tracking
    @Published var stationaryLocation: CLLocation?
    @Published var stationaryStartTime: Date?

    /// Distance threshold (metres) to consider the user has moved to a new location.
    private let movementThreshold: Double = 50.0

    /// Number of points to buffer before uploading.
    private let batchSize = 5

    /// Maximum buffer age before forced flush (seconds).
    private let maxBufferAge: TimeInterval = 120

    private var lastFlushTime = Date()
    private var lastPositionUploadTime = Date.distantPast

    // Watch device management
    @Published var watchDeviceId: Int? {
        didSet {
            if let id = watchDeviceId {
                UserDefaults.standard.set(id, forKey: "watch_own_device_id")
            } else {
                UserDefaults.standard.removeObject(forKey: "watch_own_device_id")
            }
        }
    }

    // Buffer persistence
    private static let bufferFileURL: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("watch_location_buffer.json")
    }()

    override init() {
        super.init()
        self.watchDeviceId = UserDefaults.standard.object(forKey: "watch_own_device_id") as? Int
        locationManager.delegate = self
        locationManager.desiredAccuracy = kCLLocationAccuracyBest
        locationManager.allowsBackgroundLocationUpdates = true
        authorizationStatus = locationManager.authorizationStatus
        loadBuffer()

        if UserDefaults.standard.bool(forKey: "watch_tracking_enabled"), watchDeviceId != nil {
            Self.logger.notice("Auto-resuming watch tracking")
            startTracking()
        }
    }

    func requestPermission() {
        locationManager.requestWhenInUseAuthorization()
    }

    // MARK: - Tracking

    func startTracking() {
        guard connectivity.token != nil else {
            Self.logger.warning("Cannot start: no auth token")
            return
        }

        Task {
            await ensureWatchDevice()
            guard watchDeviceId != nil else {
                Self.logger.warning("Cannot start: no watch device ID")
                return
            }
            isTracking = true
            UserDefaults.standard.set(true, forKey: "watch_tracking_enabled")
            lastFlushTime = Date()
            locationManager.startUpdatingLocation()
            Self.logger.notice("Watch tracking started")
        }
    }

    func stopTracking() {
        isTracking = false
        UserDefaults.standard.set(false, forKey: "watch_tracking_enabled")
        locationManager.stopUpdatingLocation()
        stationaryLocation = nil
        stationaryStartTime = nil
        Self.logger.notice("Watch tracking stopped")

        Task { await flushBuffer() }
    }

    // MARK: - Watch device registration

    private func ensureWatchDevice() async {
        if watchDeviceId != nil { return }

        let deviceName = WKInterfaceDevice.current().name
        let identifier = "watch-\(WKInterfaceDevice.current().identifierForVendor?.uuidString ?? UUID().uuidString)"

        do {
            // Check if watch device already exists
            let devices = try await api.fetchDevices()
            if let existing = devices.first(where: { $0.identifier == identifier }) {
                watchDeviceId = existing.id
                Self.logger.notice("Found existing watch device: \(existing.id)")
                return
            }

            let device = try await api.createDevice(name: "Apple Watch (\(deviceName))", identifier: identifier)
            watchDeviceId = device.id
            Self.logger.notice("Created watch device: \(device.id)")
        } catch {
            Self.logger.error("Failed to register watch device: \(error.localizedDescription)")
        }
    }

    // MARK: - Location handling

    func handleLocations(_ locations: [CLLocation]) {
        guard let _ = watchDeviceId else { return }

        for location in locations {
            guard location.horizontalAccuracy >= 0 else { continue }

            let point = LocationPoint(from: location)
            buffer.append(point)
            lastLocation = location

            // Update stationary tracking
            updateStationaryState(location)
        }

        // Periodic position upload (every 15s)
        if let location = lastLocation, let deviceId = watchDeviceId,
           Date().timeIntervalSince(lastPositionUploadTime) >= 15 {
            lastPositionUploadTime = Date()
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let ts = formatter.string(from: location.timestamp)
            Task {
                try? await api.updatePosition(
                    deviceId: deviceId,
                    latitude: location.coordinate.latitude,
                    longitude: location.coordinate.longitude,
                    altitude: location.altitude,
                    accuracy: location.horizontalAccuracy >= 0 ? location.horizontalAccuracy : nil,
                    speed: location.speed >= 0 ? location.speed : nil,
                    timestamp: ts
                )
            }
        }

        // Flush buffer
        let timeSinceFlush = Date().timeIntervalSince(lastFlushTime)
        if buffer.count >= batchSize || timeSinceFlush >= maxBufferAge {
            lastFlushTime = Date()
            Task { await flushBuffer() }
        }
    }

    // MARK: - Stationary detection

    private func updateStationaryState(_ location: CLLocation) {
        guard let anchor = stationaryLocation else {
            // First location — set as anchor
            stationaryLocation = location
            stationaryStartTime = Date()
            return
        }

        let distance = location.distance(from: anchor)
        if distance > movementThreshold {
            // Moved — reset anchor
            stationaryLocation = location
            stationaryStartTime = Date()
            Self.logger.debug("Moved \(distance, format: .fixed(precision: 0))m — new anchor")
        }
    }

    var timeAtCurrentLocation: TimeInterval? {
        guard let start = stationaryStartTime else { return nil }
        return Date().timeIntervalSince(start)
    }

    var formattedTimeAtLocation: String {
        guard let elapsed = timeAtCurrentLocation else { return "--" }
        let totalSeconds = Int(elapsed)
        let hours = totalSeconds / 3600
        let minutes = (totalSeconds % 3600) / 60
        let seconds = totalSeconds % 60
        if hours > 0 {
            return String(format: "%dh %02dm", hours, minutes)
        } else if minutes > 0 {
            return String(format: "%dm %02ds", minutes, seconds)
        } else {
            return String(format: "%ds", seconds)
        }
    }

    // MARK: - Buffer

    func flushBuffer() async {
        guard let deviceId = watchDeviceId, !buffer.isEmpty else { return }

        let pointsToUpload = buffer
        buffer.removeAll()

        do {
            _ = try await api.uploadLocations(deviceId: deviceId, locations: pointsToUpload)
            uploadError = nil
            deleteBufferFile()
        } catch {
            buffer.insert(contentsOf: pointsToUpload, at: 0)
            uploadError = error.localizedDescription
            saveBuffer()
            Self.logger.error("Upload failed: \(error.localizedDescription)")
        }
    }

    private func saveBuffer() {
        guard !buffer.isEmpty else { return }
        do {
            let data = try JSONEncoder().encode(buffer)
            try data.write(to: Self.bufferFileURL, options: .atomic)
        } catch {
            Self.logger.error("Failed to save buffer: \(error.localizedDescription)")
        }
    }

    private func loadBuffer() {
        guard FileManager.default.fileExists(atPath: Self.bufferFileURL.path) else { return }
        do {
            let data = try Data(contentsOf: Self.bufferFileURL)
            let points = try JSONDecoder().decode([LocationPoint].self, from: data)
            buffer.insert(contentsOf: points, at: 0)
            try? FileManager.default.removeItem(at: Self.bufferFileURL)
            Self.logger.notice("Restored \(points.count) points from disk")
        } catch {
            Self.logger.error("Failed to load buffer: \(error.localizedDescription)")
        }
    }

    private func deleteBufferFile() {
        try? FileManager.default.removeItem(at: Self.bufferFileURL)
    }

    func handleAuthorizationChange(_ manager: CLLocationManager) {
        authorizationStatus = manager.authorizationStatus
    }
}

// MARK: - CLLocationManagerDelegate

extension WatchLocationService: CLLocationManagerDelegate {
    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        MainActor.assumeIsolated { handleLocations(locations) }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        MainActor.assumeIsolated { handleAuthorizationChange(manager) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Self.logger.error("CLLocationManager error: \(error.localizedDescription)")
    }
}
