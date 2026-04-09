import SwiftUI

@main
struct LocationzApp: App {
    @StateObject private var api = APIService.shared
    @Environment(\.scenePhase) private var scenePhase

    /// Eagerly initialize LocationService so it starts tracking even when
    /// the app is relaunched in the background by a geofence event after jetsam.
    private let locationService = LocationService.shared

    /// Initialize WatchConnectivity to sync credentials with Apple Watch.
    private let phoneConnectivity = PhoneConnectivityService.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(api)
                .onChange(of: scenePhase) { _, newPhase in
                    switch newPhase {
                    case .active:
                        Log.lifecycle.notice("App became active")
                        LocationService.shared.handleForegroundTransition()
                        PhoneConnectivityService.shared.sendCredentialsToWatch()
                    case .inactive:
                        Log.lifecycle.notice("App became inactive")
                    case .background:
                        Log.lifecycle.notice("App entering background")
                        LocationService.shared.handleBackgroundTransition()
                    @unknown default:
                        Log.lifecycle.warning("Unknown scene phase")
                    }
                }
        }
    }
}
