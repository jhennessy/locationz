import SwiftUI
import CoreLocation

struct WatchTrackingView: View {
    @EnvironmentObject var connectivity: WatchConnectivityService
    @EnvironmentObject var locationService: WatchLocationService

    @State private var timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        NavigationStack {
            if connectivity.token == nil {
                notConnectedView
            } else {
                trackingContentView
            }
        }
    }

    // MARK: - Not Connected

    private var notConnectedView: some View {
        VStack(spacing: 12) {
            Image(systemName: "iphone.slash")
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text("Not Connected")
                .font(.headline)
            Text("Open Locationz on your iPhone to sync credentials.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Button("Retry") {
                connectivity.requestCredentials()
            }
            .buttonStyle(.borderedProminent)
            .tint(.blue)
        }
        .padding()
    }

    // MARK: - Tracking Content

    private var trackingContentView: some View {
        ScrollView {
            VStack(spacing: 12) {
                trackingToggle
                if locationService.isTracking {
                    timeAtLocationCard
                    locationCard
                    statusCard
                }
            }
            .padding(.horizontal, 4)
        }
        .navigationTitle("Locationz")
        .navigationBarTitleDisplayMode(.inline)
        .onReceive(timer) { _ in
            // Force view refresh for the elapsed time counter
            if locationService.isTracking {
                locationService.objectWillChange.send()
            }
        }
    }

    // MARK: - Toggle

    private var trackingToggle: some View {
        Button {
            if locationService.isTracking {
                locationService.stopTracking()
            } else {
                if locationService.authorizationStatus == .notDetermined {
                    locationService.requestPermission()
                }
                locationService.startTracking()
            }
        } label: {
            HStack {
                Image(systemName: locationService.isTracking ? "location.fill" : "location.slash")
                    .foregroundStyle(locationService.isTracking ? .green : .secondary)
                Text(locationService.isTracking ? "Tracking" : "Start Tracking")
                    .font(.headline)
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .tint(locationService.isTracking ? .green.opacity(0.3) : .blue)
    }

    // MARK: - Time at Location

    private var timeAtLocationCard: some View {
        VStack(spacing: 4) {
            Text("Here for")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(locationService.formattedTimeAtLocation)
                .font(.system(.title2, design: .rounded, weight: .bold))
                .monospacedDigit()
                .foregroundStyle(.orange)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(.orange.opacity(0.15), in: RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Location

    private var locationCard: some View {
        Group {
            if let loc = locationService.lastLocation {
                VStack(spacing: 2) {
                    Text(String(format: "%.5f, %.5f", loc.coordinate.latitude, loc.coordinate.longitude))
                        .font(.system(.caption, design: .monospaced))
                    if loc.speed >= 0 {
                        let kmh = loc.speed * 3.6
                        Text(String(format: "%.0f km/h", kmh))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
                .background(.blue.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    // MARK: - Status

    private var statusCard: some View {
        VStack(spacing: 4) {
            HStack {
                Label("\(locationService.buffer.count)", systemImage: "arrow.up.circle")
                    .font(.caption2)
                Spacer()
                Label(
                    String(format: "%.0fm", locationService.lastLocation?.horizontalAccuracy ?? 0),
                    systemImage: "scope"
                )
                .font(.caption2)
            }
            .foregroundStyle(.secondary)

            if let error = locationService.uploadError {
                Text(error)
                    .font(.system(size: 10))
                    .foregroundStyle(.red)
                    .lineLimit(1)
            }

            HStack {
                Circle()
                    .fill(connectivity.isPhoneReachable ? .green : .orange)
                    .frame(width: 6, height: 6)
                Text(connectivity.isPhoneReachable ? "Phone nearby" : "Independent")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 6)
        .background(.gray.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
    }
}
