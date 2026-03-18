import SwiftUI
import MapKit

struct VisitsView: View {
    @EnvironmentObject var api: APIService
    @ObservedObject var locationService = LocationService.shared

    @State private var visits: [VisitInfo] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var cameraPosition: MapCameraPosition = .automatic
    @State private var selectedMapStyle: MapStyleOption = .standard
    @State private var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @State private var showAllVisits = false
    @State private var selectedPlaceDetail: PlaceDetail?

    struct PlaceDetail: Identifiable {
        let id: Int
        let visits: [VisitInfo]
    }

    enum MapStyleOption: String, CaseIterable {
        case standard = "Standard"
        case satellite = "Satellite"
        case hybrid = "Hybrid"

        var mapStyle: MapStyle {
            switch self {
            case .standard: .standard
            case .satellite: .imagery
            case .hybrid: .hybrid
            }
        }
    }

    private var visitsByPlace: [Int: [VisitInfo]] {
        Dictionary(grouping: visits, by: \.placeId)
    }

    var body: some View {
        NavigationStack {
            Group {
                if isLoading && visits.isEmpty {
                    ProgressView("Loading visits...")
                } else {
                    VStack(spacing: 0) {
                        mapSection
                        dateNavigationBar

                        if visits.isEmpty {
                            ContentUnavailableView(
                                showAllVisits ? "No Visits Yet" : "No Visits",
                                systemImage: "mappin.slash",
                                description: Text(showAllVisits
                                    ? "Visits are detected when you stay in one place for at least 5 minutes."
                                    : "No visits recorded on this day.")
                            )
                        } else {
                            ScrollView {
                                LazyVStack(spacing: 8) {
                                    ForEach(visits) { visit in
                                        visitRow(visit)
                                            .onTapGesture {
                                                withAnimation {
                                                    cameraPosition = .region(MKCoordinateRegion(
                                                        center: .init(
                                                            latitude: visit.latitude,
                                                            longitude: visit.longitude
                                                        ),
                                                        latitudinalMeters: 1000,
                                                        longitudinalMeters: 1000
                                                    ))
                                                }
                                            }
                                    }
                                }
                                .padding(.horizontal)
                                .padding(.top, 8)
                            }
                            .refreshable {
                                await loadVisits()
                            }
                        }
                    }
                }
            }
            .navigationTitle("Visits")
            .navigationBarTitleDisplayMode(.inline)
            .task { await loadVisits() }
            .sheet(item: $selectedPlaceDetail) { detail in
                placeDetailSheet(detail)
            }
        }
    }

    // MARK: - Map

    private var mapSection: some View {
        Map(position: $cameraPosition) {
            ForEach(Array(visitsByPlace.keys.sorted()), id: \.self) { placeId in
                if let placeVisits = visitsByPlace[placeId],
                   let representative = placeVisits.first {
                    Annotation(
                        representative.displayLocation,
                        coordinate: .init(
                            latitude: representative.latitude,
                            longitude: representative.longitude
                        )
                    ) {
                        VStack(spacing: 2) {
                            if placeVisits.count > 1 {
                                Text("\(placeVisits.count)")
                                    .font(.caption2.bold())
                                    .foregroundStyle(.white)
                                    .frame(width: 20, height: 20)
                                    .background(.red, in: Circle())
                            }
                            Image(systemName: "mappin.circle.fill")
                                .foregroundStyle(.red)
                                .font(.title2)
                        }
                        .onTapGesture {
                            selectedPlaceDetail = PlaceDetail(
                                id: placeId,
                                visits: placeVisits.sorted {
                                    ($0.arrivalDate ?? .distantPast) > ($1.arrivalDate ?? .distantPast)
                                }
                            )
                        }
                    }
                }
            }
        }
        .mapStyle(selectedMapStyle.mapStyle)
        .frame(height: 300)
        .overlay(alignment: .bottomTrailing) {
            Picker("Map Style", selection: $selectedMapStyle) {
                ForEach(MapStyleOption.allCases, id: \.self) { option in
                    Text(option.rawValue).tag(option)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 220)
            .padding(8)
        }
    }

    // MARK: - Date Navigation

    private var dateNavigationBar: some View {
        HStack {
            Button {
                selectedDate = Calendar.current.date(byAdding: .day, value: -1, to: selectedDate) ?? selectedDate
                showAllVisits = false
                Task { await loadVisits() }
            } label: {
                Image(systemName: "chevron.left")
                    .fontWeight(.semibold)
            }
            .disabled(showAllVisits)

            Spacer()

            if showAllVisits {
                Text("All Visits")
                    .font(.headline)
            } else {
                Button {
                    selectedDate = Calendar.current.startOfDay(for: Date())
                    Task { await loadVisits() }
                } label: {
                    Group {
                        if Calendar.current.isDateInToday(selectedDate) {
                            Text("Today")
                        } else if Calendar.current.isDateInYesterday(selectedDate) {
                            Text("Yesterday")
                        } else {
                            Text(selectedDate, format: .dateTime.weekday(.abbreviated).month().day())
                        }
                    }
                    .font(.headline)
                }
                .foregroundStyle(.primary)
            }

            Spacer()

            Button {
                selectedDate = Calendar.current.date(byAdding: .day, value: 1, to: selectedDate) ?? selectedDate
                showAllVisits = false
                Task { await loadVisits() }
            } label: {
                Image(systemName: "chevron.right")
                    .fontWeight(.semibold)
            }
            .disabled(showAllVisits || Calendar.current.isDateInToday(selectedDate))

            Divider()
                .frame(height: 20)
                .padding(.horizontal, 4)

            Button(showAllVisits ? "Today" : "All") {
                if showAllVisits {
                    showAllVisits = false
                    selectedDate = Calendar.current.startOfDay(for: Date())
                } else {
                    showAllVisits = true
                }
                Task { await loadVisits() }
            }
            .font(.subheadline.bold())
        }
        .padding(.horizontal)
        .padding(.vertical, 10)
        .background(Color(.secondarySystemBackground))
    }

    // MARK: - Place Detail Sheet

    private func placeDetailSheet(_ detail: PlaceDetail) -> some View {
        NavigationStack {
            List {
                ForEach(detail.visits) { visit in
                    VStack(alignment: .leading, spacing: 4) {
                        if let arrival = visit.arrivalDate {
                            Text(arrival, format: .dateTime.weekday(.wide).month().day().year())
                                .font(.subheadline.bold())
                        }
                        HStack {
                            if let arrival = visit.arrivalDate {
                                Text(arrival, format: .dateTime.hour().minute())
                            }
                            if let departure = visit.departureDate {
                                Text("–")
                                Text(departure, format: .dateTime.hour().minute())
                            }
                            Spacer()
                            Text(visit.formattedDuration)
                                .foregroundStyle(.secondary)
                        }
                        .font(.subheadline)
                    }
                }
            }
            .navigationTitle(detail.visits.first?.displayLocation ?? "Visits")
            .navigationBarTitleDisplayMode(.inline)
            .presentationDetents([.medium])
        }
    }

    // MARK: - Visit Row

    private func visitRow(_ visit: VisitInfo) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack {
                Text(visit.formattedDuration)
                    .font(.caption.bold())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(.blue, in: Capsule())
            }
            .frame(width: 64)

            VStack(alignment: .leading, spacing: 4) {
                Text(visit.displayLocation)
                    .font(.subheadline)
                    .lineLimit(2)

                if let arrival = visit.arrivalDate {
                    HStack(spacing: 4) {
                        if showAllVisits {
                            Text(arrival, format: .dateTime.month(.abbreviated).day())
                        }
                        Image(systemName: "arrow.down.circle")
                            .foregroundStyle(.green)
                        Text(arrival, format: .dateTime.hour().minute())
                        if let departure = visit.departureDate {
                            Image(systemName: "arrow.up.circle")
                                .foregroundStyle(.red)
                            Text(departure, format: .dateTime.hour().minute())
                        }
                    }
                    .font(.subheadline)
                }
            }

            Spacer()
        }
        .padding()
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .shadow(color: .black.opacity(0.05), radius: 2, y: 1)
    }

    // MARK: - Data Loading

    private func loadVisits() async {
        guard let deviceId = locationService.deviceId else {
            isLoading = false
            return
        }
        isLoading = true

        do {
            if showAllVisits {
                visits = try await api.fetchVisits(deviceId: deviceId, limit: 1000)
            } else {
                let endOfDay = Calendar.current.date(byAdding: .day, value: 1, to: selectedDate)!
                visits = try await api.fetchVisits(
                    deviceId: deviceId,
                    limit: 200,
                    startDate: selectedDate,
                    endDate: endOfDay
                )
            }
            // Sort earliest first
            visits.sort { ($0.arrivalDate ?? .distantPast) < ($1.arrivalDate ?? .distantPast) }
            zoomToFitVisits()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func zoomToFitVisits() {
        guard !visits.isEmpty else { return }
        if visits.count == 1, let only = visits.first {
            withAnimation {
                cameraPosition = .region(MKCoordinateRegion(
                    center: .init(latitude: only.latitude, longitude: only.longitude),
                    latitudinalMeters: 2000,
                    longitudinalMeters: 2000
                ))
            }
            return
        }
        let lats = visits.map(\.latitude)
        let lons = visits.map(\.longitude)
        let center = CLLocationCoordinate2D(
            latitude: (lats.min()! + lats.max()!) / 2,
            longitude: (lons.min()! + lons.max()!) / 2
        )
        let span = MKCoordinateSpan(
            latitudeDelta: max((lats.max()! - lats.min()!) * 1.5, 0.01),
            longitudeDelta: max((lons.max()! - lons.min()!) * 1.5, 0.01)
        )
        withAnimation {
            cameraPosition = .region(MKCoordinateRegion(center: center, span: span))
        }
    }
}

#Preview("With Visits") {
    let service = LocationService.shared
    service.deviceId = 1
    return VisitsView()
        .environmentObject(APIService.shared)
}

#Preview("Empty") {
    VisitsView()
        .environmentObject(APIService.shared)
}
