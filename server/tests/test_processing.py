"""Tests for the stateful visit detection algorithm."""

import datetime
from unittest.mock import patch

import pytest

from processing import (
    haversine_m,
    snap_to_place,
    process_device_locations,
    reprocess_all,
    _run_state_machine,
    VISIT_RADIUS_M,
    LIFECYCLE_RADIUS_M,
    MIN_VISIT_DURATION_S,
    MAX_HORIZONTAL_ACCURACY_M,
)
from models import DeviceState, Location, Place, Visit
from tests.gps_test_fixtures import (
    GPS_TRACE,
    HOME_CENTER,
    COFFEE_SHOP_CENTER,
    OFFICE_CENTER,
)


# =====================================================================
# Haversine tests
# =====================================================================

class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_m(37.7749, -122.4194, 37.7749, -122.4194) == 0.0

    def test_known_distance(self):
        d = haversine_m(37.7793, -122.4193, 37.7956, -122.3935)
        assert 2000 < d < 3000

    def test_short_distance(self):
        d = haversine_m(37.7749, -122.4194, 37.7758, -122.4194)
        assert 90 < d < 110


# =====================================================================
# Place snapping tests
# =====================================================================

class TestPlaceSnapping:
    def test_creates_new_place(self, db, test_user):
        place = snap_to_place(db, test_user.id, 37.7615, -122.4240)
        assert place.id is not None
        assert place.user_id == test_user.id

    def test_snaps_to_existing_place(self, db, test_user):
        place1 = snap_to_place(db, test_user.id, 37.7615, -122.4240)
        db.commit()
        place2 = snap_to_place(db, test_user.id, 37.7616, -122.4241)
        assert place2.id == place1.id

    def test_does_not_snap_to_distant_place(self, db, test_user):
        place1 = snap_to_place(db, test_user.id, 37.7615, -122.4240)
        db.commit()
        place2 = snap_to_place(db, test_user.id, 37.7738, -122.4128)
        assert place2.id != place1.id


# =====================================================================
# State machine tests
# =====================================================================

_BASE = datetime.datetime(2024, 1, 15, 8, 0, 0)


def _make_location(lat, lon, ts, device_id=1, notes=None, accuracy=10.0):
    """Helper to create a Location object for testing."""
    loc = Location(
        device_id=device_id,
        latitude=lat,
        longitude=lon,
        altitude=0,
        horizontal_accuracy=accuracy,
        speed=0.0,
        timestamp=ts,
        notes=notes,
    )
    return loc


def _t(minutes, seconds=0):
    return _BASE + datetime.timedelta(minutes=minutes, seconds=seconds)


class TestStateMachine:
    """Tests for the core state machine logic."""

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_detects_simple_visit(self, mock_geocode, db, test_user, test_device):
        """Points at same location for > 5 min should create a visit."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        locs = [
            _make_location(37.7615, -122.4240, _t(0), test_device.id),
            _make_location(37.7615, -122.4241, _t(3), test_device.id),
            _make_location(37.7616, -122.4240, _t(6), test_device.id),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 1
        assert visits[0].is_open is True
        assert state.state == "stationary"

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_departure_finalizes_visit(self, mock_geocode, db, test_user, test_device):
        """Moving far away should finalize the open visit."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        locs = [
            _make_location(37.7615, -122.4240, _t(0), test_device.id),
            _make_location(37.7615, -122.4241, _t(3), test_device.id),
            _make_location(37.7616, -122.4240, _t(6), test_device.id),
            # Depart — far away
            _make_location(37.7700, -122.4100, _t(8), test_device.id),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 1
        assert visits[0].is_open is False
        assert state.state == "moving"

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_lifecycle_points_extend_visit(self, mock_geocode, db, test_user, test_device):
        """Lifecycle events near anchor should extend visit duration."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        locs = [
            _make_location(37.7615, -122.4240, _t(0), test_device.id),
            _make_location(37.7615, -122.4241, _t(3), test_device.id),
            _make_location(37.7616, -122.4240, _t(6), test_device.id),
            # Lifecycle events during sleep — slightly offset position
            _make_location(37.7617, -122.4238, _t(30), test_device.id, notes="Geofence exit (bg: true)"),
            _make_location(37.7614, -122.4242, _t(60), test_device.id, notes="Geofence exit (bg: true)"),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 1
        # Visit should extend to 60 minutes (the last lifecycle point)
        assert visits[0].duration_seconds >= 3500

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_lifecycle_far_away_ends_visit(self, mock_geocode, db, test_user, test_device):
        """Lifecycle event far from anchor should finalize the visit."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        locs = [
            _make_location(37.7615, -122.4240, _t(0), test_device.id),
            _make_location(37.7615, -122.4241, _t(3), test_device.id),
            _make_location(37.7616, -122.4240, _t(6), test_device.id),
            # Lifecycle point very far away (user left)
            _make_location(37.7800, -122.4000, _t(30), test_device.id, notes="Geofence exit (bg: true)"),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 1
        assert visits[0].is_open is False

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_short_stay_not_detected(self, mock_geocode, db, test_user, test_device):
        """Stay shorter than min_visit_duration_s should not create a visit."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        locs = [
            _make_location(37.7615, -122.4240, _t(0), test_device.id),
            _make_location(37.7615, -122.4241, _t(2), test_device.id),
            _make_location(37.7616, -122.4240, _t(4), test_device.id),
            # Leave after 4 min (below 5 min threshold)
            _make_location(37.7700, -122.4100, _t(5), test_device.id),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 0

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_bad_accuracy_points_skipped(self, mock_geocode, db, test_user, test_device):
        """Real GPS points with bad accuracy should be filtered out."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        locs = [
            _make_location(37.7615, -122.4240, _t(0), test_device.id),
            _make_location(37.7615, -122.4241, _t(3), test_device.id),
            # Bad accuracy point far away — should be skipped
            _make_location(37.7800, -122.4000, _t(4), test_device.id, accuracy=500.0),
            _make_location(37.7616, -122.4240, _t(6), test_device.id),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 1
        assert state.state == "stationary"

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_overnight_sparse_data(self, mock_geocode, db, test_user, test_device):
        """Overnight stay with sparse geofence data should be detected as one visit."""
        state = DeviceState(device_id=test_device.id, state="unknown")
        db.add(state)
        db.flush()

        evening = datetime.datetime(2024, 1, 15, 22, 0, 0)
        morning = datetime.datetime(2024, 1, 16, 7, 0, 0)

        locs = [
            # Arrive home evening
            _make_location(47.3823, 8.4966, evening, test_device.id),
            _make_location(47.3824, 8.4967, evening + datetime.timedelta(minutes=2), test_device.id),
            _make_location(47.3823, 8.4965, evening + datetime.timedelta(minutes=5), test_device.id),
            # Sparse geofence events overnight
            _make_location(47.3825, 8.4968, evening + datetime.timedelta(hours=2), test_device.id, notes="Geofence exit"),
            _make_location(47.3822, 8.4964, evening + datetime.timedelta(hours=5), test_device.id, notes="Geofence exit"),
            # Wake up and leave
            _make_location(47.3823, 8.4966, morning, test_device.id),
            _make_location(47.3700, 8.5100, morning + datetime.timedelta(minutes=5), test_device.id),
        ]
        for loc in locs:
            db.add(loc)
        db.flush()

        thresholds = {"visit_radius_m": 50.0, "min_visit_duration_s": 300, "place_snap_radius_m": 75.0, "max_horizontal_accuracy_m": 100.0}
        visits = _run_state_machine(
            db, state, locs, test_user.id, test_device.id,
            50.0, 150.0, 300, 100.0, thresholds,
        )

        assert len(visits) == 1
        assert visits[0].is_open is False
        # Should span from 22:00 to ~07:00 (9 hours)
        assert visits[0].duration_seconds >= 32000  # at least 8.9 hours


# =====================================================================
# Full pipeline tests
# =====================================================================

class TestProcessDeviceLocations:
    @patch("processing.reverse_geocode", return_value="123 Test St, San Francisco, CA")
    def test_full_pipeline(self, mock_geocode, db, test_user, populated_device):
        visits = process_device_locations(db, populated_device.id, test_user.id)

        assert len(visits) == 3
        assert all(v.place_id is not None for v in visits)

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_incremental_processing(self, mock_geocode, db, test_user, populated_device):
        visits1 = process_device_locations(db, populated_device.id, test_user.id)
        assert len(visits1) == 3

        # Second run (no new data) should find nothing new
        visits2 = process_device_locations(db, populated_device.id, test_user.id)
        assert len(visits2) == 0

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_places_are_created(self, mock_geocode, db, test_user, populated_device):
        process_device_locations(db, populated_device.id, test_user.id)
        places = db.query(Place).filter(Place.user_id == test_user.id).all()
        assert len(places) == 3

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_place_visit_counts(self, mock_geocode, db, test_user, populated_device):
        process_device_locations(db, populated_device.id, test_user.id)
        places = db.query(Place).filter(Place.user_id == test_user.id).all()
        total_visits = sum(p.visit_count for p in places)
        assert total_visits == 3

    def test_empty_device(self, db, test_user, test_device):
        visits = process_device_locations(db, test_device.id, test_user.id)
        assert len(visits) == 0


# =====================================================================
# Reprocessing tests
# =====================================================================

class TestReprocessAll:
    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_reprocess_produces_visits(self, mock_geocode, db, test_user, populated_device):
        result = reprocess_all(db, test_user.id)
        assert result["visits_created"] == 3
        assert result["places_created"] == 3

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_reprocess_cleans_old_data(self, mock_geocode, db, test_user, populated_device):
        # First process
        process_device_locations(db, populated_device.id, test_user.id)
        assert db.query(Visit).count() == 3

        # Reprocess should rebuild, not duplicate
        result = reprocess_all(db, test_user.id)
        assert db.query(Visit).count() == 3

    @patch("processing.reverse_geocode", return_value="Test Address")
    def test_reprocess_closes_all_visits(self, mock_geocode, db, test_user, populated_device):
        result = reprocess_all(db, test_user.id)
        open_visits = db.query(Visit).filter(Visit.is_open == True).count()
        assert open_visits == 0
