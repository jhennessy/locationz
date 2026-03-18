"""Location processing engine: stateful visit detection, place snapping, geocoding.

Processing pipeline (runs server-side after each batch upload):
1. Walk through GPS points chronologically with a state machine
2. Detect stationary visits using anchor-with-implicit-continuation
3. Snap each visit to nearest known Place (or create a new one)
4. Reverse-geocode new Places via Nominatim (OpenStreetMap, free)

Key design: the state machine persists in DeviceState across batch uploads.
An open visit (user still at location) gets its departure updated on each batch.
Silence (no GPS data) means the user is still at the current location.
A visit only ends when a point arrives that is far from the anchor.
"""

import datetime
import logging
import math
import statistics
import time
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models import Config, Device, DeviceState, Location, Place, Visit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (data-driven defaults from GPS scatter analysis)
# ---------------------------------------------------------------------------

MAX_HORIZONTAL_ACCURACY_M = 100.0  # discard real GPS points worse than this
VISIT_RADIUS_M = 50.0              # P95 of GPS scatter at known places
LIFECYCLE_RADIUS_M = 150.0         # relaxed radius for lifecycle events (GPS drift)
MIN_VISIT_DURATION_S = 300         # 5 minutes
PLACE_SNAP_RADIUS_M = 75.0        # snap to existing place within this distance

# Nominatim rate limiting (max 1 req/sec per OSM policy)
_last_nominatim_call = 0.0


def get_thresholds(db: Session) -> dict:
    """Read algorithm thresholds from the Config table, falling back to defaults."""
    defaults = {
        "max_horizontal_accuracy_m": MAX_HORIZONTAL_ACCURACY_M,
        "visit_radius_m": VISIT_RADIUS_M,
        "min_visit_duration_s": MIN_VISIT_DURATION_S,
        "place_snap_radius_m": PLACE_SNAP_RADIUS_M,
    }
    rows = db.query(Config).filter(Config.key.in_(defaults.keys())).all()
    for row in rows:
        defaults[row.key] = float(row.value)
    return defaults


# ---------------------------------------------------------------------------
# Geo math
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two WGS-84 points."""
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Stateful visit detection
# ---------------------------------------------------------------------------

def process_device_locations(
    db: Session, device_id: int, user_id: int, thresholds: dict | None = None,
) -> list[Visit]:
    """Process new locations using the stateful visit detection algorithm.

    Maintains a DeviceState that persists across batch uploads.  An open visit
    gets its departure updated as new confirming points arrive.  The visit is
    finalized only when the user departs (a point far from the anchor).
    """
    if thresholds is None:
        thresholds = get_thresholds(db)

    radius = thresholds.get("visit_radius_m", VISIT_RADIUS_M)
    lifecycle_radius = radius * 3  # ~150m for lifecycle GPS drift
    min_duration = thresholds.get("min_visit_duration_s", MIN_VISIT_DURATION_S)
    max_accuracy = thresholds.get("max_horizontal_accuracy_m", MAX_HORIZONTAL_ACCURACY_M)

    # Get or create device state
    state = db.query(DeviceState).filter(DeviceState.device_id == device_id).first()
    if not state:
        state = DeviceState(device_id=device_id, state="unknown")
        db.add(state)
        db.flush()

    # Fetch new locations since last processed point
    since = state.last_confirmed_at or datetime.datetime.min
    raw_locations = (
        db.query(Location)
        .filter(Location.device_id == device_id, Location.timestamp > since)
        .order_by(Location.timestamp.asc())
        .all()
    )

    if not raw_locations:
        return []

    new_visits = _run_state_machine(
        db, state, raw_locations, user_id, device_id,
        radius, lifecycle_radius, min_duration, max_accuracy, thresholds,
    )

    db.commit()
    return new_visits


def _run_state_machine(
    db: Session,
    state: DeviceState,
    locations: list[Location],
    user_id: int,
    device_id: int,
    radius: float,
    lifecycle_radius: float,
    min_duration: float,
    max_accuracy: float,
    thresholds: dict,
) -> list[Visit]:
    """Core state machine: process a sequence of locations and update DeviceState."""
    new_visits = []
    # Collect non-lifecycle points for centroid computation of current cluster
    cluster_lats: list[float] = []
    cluster_lons: list[float] = []

    for loc in locations:
        is_lifecycle = loc.notes is not None
        pt_lat, pt_lon = loc.latitude, loc.longitude
        pt_time = loc.timestamp

        # Skip real GPS points with bad accuracy (lifecycle points kept for timing)
        if not is_lifecycle:
            acc = loc.horizontal_accuracy
            if acc is not None and acc > max_accuracy:
                continue

        if state.state == "stationary":
            dist = haversine_m(state.anchor_latitude, state.anchor_longitude, pt_lat, pt_lon)
            effective_radius = lifecycle_radius if is_lifecycle else radius

            if dist <= effective_radius:
                # Still here — update departure on the open visit
                state.last_confirmed_at = pt_time
                if not is_lifecycle:
                    cluster_lats.append(pt_lat)
                    cluster_lons.append(pt_lon)
                if state.open_visit_id:
                    visit = db.query(Visit).filter(Visit.id == state.open_visit_id).first()
                    if visit:
                        visit.departure = pt_time
                        visit.duration_seconds = int((pt_time - visit.arrival).total_seconds())
            else:
                # Departed — finalize the open visit
                if state.open_visit_id:
                    visit = db.query(Visit).filter(Visit.id == state.open_visit_id).first()
                    if visit:
                        visit.is_open = False
                        # Update centroid from collected cluster points
                        if cluster_lats:
                            visit.latitude = statistics.median(cluster_lats)
                            visit.longitude = statistics.median(cluster_lons)

                # Reset for new potential stay
                cluster_lats.clear()
                cluster_lons.clear()
                state.open_visit_id = None

                if not is_lifecycle:
                    state.state = "moving"
                    state.anchor_latitude = pt_lat
                    state.anchor_longitude = pt_lon
                    state.arrived_at = pt_time
                    state.last_confirmed_at = pt_time
                    cluster_lats.append(pt_lat)
                    cluster_lons.append(pt_lon)
                else:
                    # Lifecycle point far from anchor — user left but we don't
                    # have a precise new location yet
                    state.state = "moving"
                    state.anchor_latitude = None
                    state.anchor_longitude = None
                    state.arrived_at = pt_time
                    state.last_confirmed_at = pt_time

        elif state.state in ("moving", "unknown"):
            if is_lifecycle:
                # Lifecycle points during movement are not useful for anchoring
                state.last_confirmed_at = pt_time
                continue

            if state.anchor_latitude is not None:
                dist = haversine_m(state.anchor_latitude, state.anchor_longitude, pt_lat, pt_lon)
            else:
                dist = float("inf")

            if dist <= radius:
                # Still near the anchor — check if we've been here long enough
                state.last_confirmed_at = pt_time
                cluster_lats.append(pt_lat)
                cluster_lons.append(pt_lon)
                duration = (pt_time - state.arrived_at).total_seconds()

                if duration >= min_duration and state.open_visit_id is None:
                    # Promote to STATIONARY, create open visit
                    state.state = "stationary"
                    visit_lat = statistics.median(cluster_lats)
                    visit_lon = statistics.median(cluster_lons)

                    place = snap_to_place(db, user_id, visit_lat, visit_lon, thresholds)
                    if not place.address:
                        addr = reverse_geocode(place.latitude, place.longitude)
                        if addr:
                            place.address = addr

                    place.visit_count += 1
                    place.total_duration_seconds += int(duration)

                    visit = Visit(
                        device_id=device_id,
                        place_id=place.id,
                        latitude=visit_lat,
                        longitude=visit_lon,
                        arrival=state.arrived_at,
                        departure=pt_time,
                        duration_seconds=int(duration),
                        address=place.address,
                        is_open=True,
                    )
                    db.add(visit)
                    db.flush()
                    state.open_visit_id = visit.id
                    new_visits.append(visit)
            else:
                # New anchor — reset
                cluster_lats.clear()
                cluster_lons.clear()
                state.anchor_latitude = pt_lat
                state.anchor_longitude = pt_lon
                state.arrived_at = pt_time
                state.last_confirmed_at = pt_time
                cluster_lats.append(pt_lat)
                cluster_lons.append(pt_lon)

    return new_visits


# ---------------------------------------------------------------------------
# Place snapping
# ---------------------------------------------------------------------------

def snap_to_place(
    db: Session,
    user_id: int,
    lat: float,
    lon: float,
    thresholds: dict | None = None,
) -> Place:
    """Find the nearest existing Place within place_snap_radius_m, or create one."""
    snap_radius = (thresholds or {}).get("place_snap_radius_m", PLACE_SNAP_RADIUS_M)
    places = db.query(Place).filter(Place.user_id == user_id).all()

    best_place = None
    best_dist = float("inf")

    for p in places:
        d = haversine_m(lat, lon, p.latitude, p.longitude)
        if d < best_dist:
            best_dist = d
            best_place = p

    if best_place is not None and best_dist <= snap_radius:
        return best_place

    new_place = Place(
        user_id=user_id,
        latitude=lat,
        longitude=lon,
        visit_count=0,
        total_duration_seconds=0,
    )
    db.add(new_place)
    db.flush()
    return new_place


# ---------------------------------------------------------------------------
# Reverse geocoding (Nominatim / OpenStreetMap)
# ---------------------------------------------------------------------------

def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Look up an address from coordinates using Nominatim (free, 1 req/s)."""
    global _last_nominatim_call

    elapsed = time.time() - _last_nominatim_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "zoom": 18,
                "addressdetails": 1,
            },
            headers={"User-Agent": "Locationz/1.0"},
            timeout=10,
        )
        _last_nominatim_call = time.time()

        if resp.status_code == 200:
            data = resp.json()
            return data.get("display_name")
    except Exception as e:
        logger.warning(f"Nominatim reverse geocode failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Reprocessing: rebuild all visits from scratch
# ---------------------------------------------------------------------------

def reprocess_all(db: Session, user_id: int) -> dict:
    """Delete all visits/places/device states for the user and reprocess.

    Returns {"visits_created": int, "places_created": int}.
    """
    thresholds = get_thresholds(db)

    devices = db.query(Device).filter(Device.user_id == user_id).all()
    device_ids = [d.id for d in devices]

    # Clean slate
    if device_ids:
        db.query(Visit).filter(Visit.device_id.in_(device_ids)).delete(synchronize_session=False)
        db.query(DeviceState).filter(DeviceState.device_id.in_(device_ids)).delete(synchronize_session=False)
    db.query(Place).filter(Place.user_id == user_id).delete(synchronize_session=False)
    db.commit()

    total_visits = 0
    for device in devices:
        # Create a fresh state and run the state machine over ALL locations
        state = DeviceState(device_id=device.id, state="unknown")
        db.add(state)
        db.flush()

        radius = thresholds.get("visit_radius_m", VISIT_RADIUS_M)
        lifecycle_radius = radius * 3
        min_duration = thresholds.get("min_visit_duration_s", MIN_VISIT_DURATION_S)
        max_accuracy = thresholds.get("max_horizontal_accuracy_m", MAX_HORIZONTAL_ACCURACY_M)

        raw_locations = (
            db.query(Location)
            .filter(Location.device_id == device.id)
            .order_by(Location.timestamp.asc())
            .all()
        )

        if raw_locations:
            visits = _run_state_machine(
                db, state, raw_locations, user_id, device.id,
                radius, lifecycle_radius, min_duration, max_accuracy, thresholds,
            )
            # Close any remaining open visit at the end of reprocessing
            if state.open_visit_id:
                visit = db.query(Visit).filter(Visit.id == state.open_visit_id).first()
                if visit:
                    visit.is_open = False
            total_visits += len(visits)

    db.commit()

    places_created = db.query(Place).filter(Place.user_id == user_id).count()
    logger.info(
        "Reprocessed user=%d: %d visits, %d places created",
        user_id, total_visits, places_created,
    )
    return {"visits_created": total_visits, "places_created": places_created}
