import HarborCore
import SwiftUI

struct LocationsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    var body: some View {
        NavigationStack {
            List {
                if query.isEmpty {
                    Section {
                        Button { choose(nil) } label: {
                            HStack(spacing: 12) {
                                Text("⚡️").font(.title2)
                                VStack(alignment: .leading) {
                                    Text("Smart Location").foregroundStyle(.primary)
                                    Text("Fastest server for where you are").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if model.settings.selectedLocationID == nil {
                                    Image(systemName: "checkmark").foregroundStyle(Color.harborAccent)
                                }
                            }
                        }
                    }
                    let favorites = model.settings.favorites.compactMap(model.location(id:))
                    if !favorites.isEmpty {
                        Section("Favorites") { ForEach(favorites) { row($0) } }
                    }
                }
                Section(query.isEmpty ? "All locations" : "Results") {
                    ForEach(filtered) { country in
                        if country.locations.count == 1, let only = country.locations.first {
                            row(only)
                        } else {
                            DisclosureGroup {
                                ForEach(country.locations) { row($0, showCountry: false) }
                            } label: {
                                HStack(spacing: 12) {
                                    Text(country.flag).font(.title2)
                                    Text(country.name)
                                    Spacer()
                                    Text("\(country.locations.count) cities").font(.caption).foregroundStyle(.secondary)
                                    if !(model.tier == .paid || country.isFree) { lock }
                                }
                            }
                        }
                    }
                }
            }
            .searchable(text: $query, prompt: "Country or city")
            .navigationTitle("Locations")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .overlay {
                if model.countries.isEmpty {
                    ContentUnavailableView("No locations yet", systemImage: "globe",
                                           description: Text("Pull to refresh when you're online."))
                } else if filtered.isEmpty {
                    ContentUnavailableView.search(text: query)
                }
            }
            .refreshable { await model.refresh() }
        }
    }

    var filtered: [Country] {
        let all = model.countries
        guard !query.isEmpty else { return all }
        return all.compactMap { c in
            if c.name.localizedCaseInsensitiveContains(query) || c.code.caseInsensitiveCompare(query) == .orderedSame { return c }
            let cities = c.locations.filter { $0.city.localizedCaseInsensitiveContains(query) }
            return cities.isEmpty ? nil : Country(code: c.code, name: c.name, locations: cities)
        }
    }

    var lock: some View {
        Image(systemName: "lock.fill").font(.caption).foregroundStyle(.secondary).accessibilityLabel("Harbor Plus")
    }

    func choose(_ location: Location?) {
        dismiss()
        Task { await model.select(location) }
    }

    @ViewBuilder
    func row(_ loc: Location, showCountry: Bool = true) -> some View {
        let isFavorite = model.settings.favorites.contains(loc.id)
        Button { choose(loc) } label: {
            HStack(spacing: 12) {
                Text(loc.flag).font(.title2)
                VStack(alignment: .leading, spacing: 2) {
                    Text(showCountry ? loc.name : loc.city).foregroundStyle(.primary)
                    if !loc.features.isEmpty {
                        Text(loc.features.sorted().map(\.capitalized).joined(separator: " · "))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if model.canUse(loc) {
                    LatencyBadge(ms: model.latency(of: loc), load: loc.load)
                } else {
                    lock
                }
                if model.settings.selectedLocationID == loc.id {
                    Image(systemName: "checkmark").foregroundStyle(Color.harborAccent)
                }
            }
        }
        .swipeActions(edge: .trailing) {
            Button {
                model.update { $0.toggleFavorite(loc.id) }
            } label: {
                Label(isFavorite ? "Unfavorite" : "Favorite", systemImage: isFavorite ? "star.slash" : "star")
            }
            .tint(.yellow)
        }
        .contextMenu {
            Button {
                model.update { $0.toggleFavorite(loc.id) }
            } label: {
                Label(isFavorite ? "Remove from Favorites" : "Add to Favorites", systemImage: "star")
            }
        }
    }
}

private struct LatencyBadge: View {
    let ms: Double?
    let load: Int

    var body: some View {
        HStack(spacing: 6) {
            if let ms {
                Text("\(Int(ms)) ms").font(.caption.monospacedDigit()).foregroundStyle(color(ms))
            }
            LoadBar(load: load)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(ms.map { "\(Int($0)) milliseconds, \(load) percent load" } ?? "\(load) percent load")
    }

    func color(_ ms: Double) -> Color { ms < 60 ? .harborProtected : ms < 150 ? .orange : .red }
}

private struct LoadBar: View {
    let load: Int
    var body: some View {
        Capsule().fill(.quaternary)
            .frame(width: 28, height: 5)
            .overlay(alignment: .leading) {
                Capsule().fill(load < 60 ? Color.harborProtected : load < 85 ? .orange : .red)
                    .frame(width: max(3, 28 * CGFloat(load) / 100))
            }
    }
}
