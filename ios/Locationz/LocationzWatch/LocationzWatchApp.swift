import SwiftUI

@main
struct LocationzWatchApp: App {
    @StateObject private var connectivity = WatchConnectivityService.shared
    @StateObject private var locationService = WatchLocationService.shared

    var body: some Scene {
        WindowGroup {
            WatchTrackingView()
                .environmentObject(connectivity)
                .environmentObject(locationService)
        }
    }
}
