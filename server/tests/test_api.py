"""Tests for REST API endpoints using the ASGI test client."""

import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db as original_get_db
from models import User, Device, Location, Place, Visit, Session as SessionModel, CurrentPosition  # noqa: F401
from auth import hash_password, create_token
from tests.gps_test_fixtures import GPS_TRACE, HOME_SEGMENT


# ---------------------------------------------------------------------------
# Test setup — override get_db using the original function reference as key
# ---------------------------------------------------------------------------

@pytest.fixture
def app_and_db():
    """Create a test FastAPI app with an in-memory database."""
    # Use StaticPool so all threads/connections share the same in-memory DB
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)

    def test_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    from api import router

    app = FastAPI()
    # Use the original function reference (captured at api.py import time) as override key
    app.dependency_overrides[original_get_db] = test_get_db
    app.include_router(router)

    # Seed test user
    session = TestSession()
    user = User(username="testuser", email="test@example.com", password_hash=hash_password("testpass"))
    session.add(user)
    session.commit()
    session.refresh(user)
    token = create_token(user.id, user.username, session)
    session.close()

    client = TestClient(app)
    return client, token, TestSession


@pytest.fixture
def client(app_and_db):
    return app_and_db[0]


@pytest.fixture
def auth_headers(app_and_db):
    return {"Authorization": f"Bearer {app_and_db[1]}"}


# ---------------------------------------------------------------------------
# Auth endpoint tests
# ---------------------------------------------------------------------------

class TestAuthEndpoints:
    def test_register(self, client):
        resp = client.post("/api/register", json={
            "username": "newuser",
            "email": "new@example.com",
            "password": "newpass123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["username"] == "newuser"

    def test_register_duplicate_username(self, client):
        resp = client.post("/api/register", json={
            "username": "testuser",
            "email": "another@example.com",
            "password": "pass",
        })
        assert resp.status_code == 409

    def test_login_success(self, client):
        resp = client.post("/api/login", json={"username": "testuser", "password": "testpass"})
        assert resp.status_code == 200
        assert "token" in resp.json()

    def test_login_wrong_password(self, client):
        resp = client.post("/api/login", json={"username": "testuser", "password": "wrong"})
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = client.post("/api/login", json={"username": "nobody", "password": "pass"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Device endpoint tests
# ---------------------------------------------------------------------------

class TestDeviceEndpoints:
    def test_create_device(self, client, auth_headers):
        resp = client.post("/api/devices", json={
            "name": "My iPhone",
            "identifier": "iphone-001",
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My iPhone"
        assert data["id"] > 0

    def test_list_devices(self, client, auth_headers):
        client.post("/api/devices", json={"name": "D1", "identifier": "d1"}, headers=auth_headers)
        client.post("/api/devices", json={"name": "D2", "identifier": "d2"}, headers=auth_headers)
        resp = client.get("/api/devices", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_delete_device(self, client, auth_headers):
        create_resp = client.post("/api/devices", json={"name": "D1", "identifier": "del-d1"}, headers=auth_headers)
        device_id = create_resp.json()["id"]
        del_resp = client.delete(f"/api/devices/{device_id}", headers=auth_headers)
        assert del_resp.status_code == 204

    def test_delete_nonexistent_device(self, client, auth_headers):
        resp = client.delete("/api/devices/9999", headers=auth_headers)
        assert resp.status_code == 404

    def test_unauthenticated_access(self, client):
        resp = client.get("/api/devices")
        assert resp.status_code == 422  # missing header


# ---------------------------------------------------------------------------
# Location upload tests
# ---------------------------------------------------------------------------

class TestLocationEndpoints:
    def _create_device(self, client, auth_headers):
        resp = client.post("/api/devices", json={"name": "Test", "identifier": "loc-test"}, headers=auth_headers)
        return resp.json()["id"]

    @patch("api.process_device_locations", return_value=[])
    def test_upload_batch(self, mock_proc, client, auth_headers):
        device_id = self._create_device(client, auth_headers)
        locations = [
            {
                "latitude": pt["latitude"],
                "longitude": pt["longitude"],
                "altitude": pt.get("altitude"),
                "horizontal_accuracy": pt.get("horizontal_accuracy"),
                "speed": pt.get("speed"),
                "timestamp": pt["timestamp"].isoformat(),
            }
            for pt in HOME_SEGMENT
        ]
        resp = client.post("/api/locations", json={
            "device_id": device_id,
            "locations": locations,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["received"] == len(HOME_SEGMENT)
        assert "batch_id" in data

    @patch("api.process_device_locations", return_value=[])
    def test_get_locations(self, mock_proc, client, auth_headers):
        device_id = self._create_device(client, auth_headers)
        locations = [
            {
                "latitude": pt["latitude"],
                "longitude": pt["longitude"],
                "timestamp": pt["timestamp"].isoformat(),
            }
            for pt in HOME_SEGMENT[:3]
        ]
        client.post("/api/locations", json={
            "device_id": device_id,
            "locations": locations,
        }, headers=auth_headers)

        resp = client.get(f"/api/locations/{device_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 3


# ---------------------------------------------------------------------------
# Visit and Place endpoint tests
# ---------------------------------------------------------------------------

class TestVisitEndpoints:
    def _setup_device_with_data(self, client, auth_headers):
        resp = client.post("/api/devices", json={"name": "VP", "identifier": "vp-test"}, headers=auth_headers)
        device_id = resp.json()["id"]
        locations = [
            {
                "latitude": pt["latitude"],
                "longitude": pt["longitude"],
                "altitude": pt.get("altitude"),
                "horizontal_accuracy": pt.get("horizontal_accuracy"),
                "speed": pt.get("speed"),
                "timestamp": pt["timestamp"].isoformat(),
            }
            for pt in GPS_TRACE
        ]
        return device_id, locations

    @patch("processing.reverse_geocode", return_value="Test Address, SF")
    def test_visits_endpoint(self, mock_geocode, client, auth_headers):
        device_id, locations = self._setup_device_with_data(client, auth_headers)
        client.post("/api/locations", json={
            "device_id": device_id,
            "locations": locations,
        }, headers=auth_headers)

        resp = client.get(f"/api/visits/{device_id}", headers=auth_headers)
        assert resp.status_code == 200
        visits = resp.json()
        assert len(visits) == 3
        assert all("arrival" in v for v in visits)
        assert all("departure" in v for v in visits)
        assert all(v["duration_seconds"] >= 300 for v in visits)

    @patch("processing.reverse_geocode", return_value="Test Address, SF")
    def test_places_endpoint(self, mock_geocode, client, auth_headers):
        device_id, locations = self._setup_device_with_data(client, auth_headers)
        client.post("/api/locations", json={
            "device_id": device_id,
            "locations": locations,
        }, headers=auth_headers)

        resp = client.get("/api/places", headers=auth_headers)
        assert resp.status_code == 200
        places = resp.json()
        assert len(places) == 3
        assert all(p["visit_count"] >= 1 for p in places)

    @patch("processing.reverse_geocode", return_value="Test Address, SF")
    def test_frequent_places_endpoint(self, mock_geocode, client, auth_headers):
        device_id, locations = self._setup_device_with_data(client, auth_headers)
        client.post("/api/locations", json={"device_id": device_id, "locations": locations}, headers=auth_headers)

        resp = client.post(f"/api/visits/{device_id}/reprocess", headers=auth_headers)
        assert resp.status_code == 200

        resp = client.get("/api/places/frequent", headers=auth_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_visits_wrong_device(self, client, auth_headers):
        resp = client.get("/api/visits/9999", headers=auth_headers)
        assert resp.status_code == 404

    def test_overnight_visit_shows_on_both_days(self, client, auth_headers, app_and_db):
        """A visit that spans midnight should be returned by either day's window.

        Regression: previously the filter was `arrival in [start, end)`, which
        hid overnight stays from the morning's view. The filter is now an
        overlap test, so the same visit appears under both days it touches.
        """
        _, _, TestSession = app_and_db
        resp = client.post("/api/devices", json={"name": "OV", "identifier": "ov-test"}, headers=auth_headers)
        device_id = resp.json()["id"]

        # Insert an overnight visit directly (no need to run the full pipeline)
        session = TestSession()
        place = Place(user_id=1, latitude=47.3823, longitude=8.4966, address="Home")
        session.add(place)
        session.flush()
        visit = Visit(
            device_id=device_id,
            place_id=place.id,
            latitude=47.3823,
            longitude=8.4966,
            arrival=datetime.datetime(2024, 6, 17, 22, 0, 0),
            departure=datetime.datetime(2024, 6, 18, 7, 30, 0),
            duration_seconds=34_200,
            address="Home",
            is_open=False,
        )
        session.add(visit)
        session.commit()
        session.close()

        # Day 1 (2024-06-17): visit arrived this day → must be returned
        resp1 = client.get(
            f"/api/visits/{device_id}?start_date=2024-06-17T00:00:00&end_date=2024-06-18T00:00:00",
            headers=auth_headers,
        )
        assert resp1.status_code == 200
        assert len(resp1.json()) == 1

        # Day 2 (2024-06-18): visit departed this day → must ALSO be returned
        resp2 = client.get(
            f"/api/visits/{device_id}?start_date=2024-06-18T00:00:00&end_date=2024-06-19T00:00:00",
            headers=auth_headers,
        )
        assert resp2.status_code == 200
        assert len(resp2.json()) == 1

        # A day with no overlap should return nothing
        resp3 = client.get(
            f"/api/visits/{device_id}?start_date=2024-06-19T00:00:00&end_date=2024-06-20T00:00:00",
            headers=auth_headers,
        )
        assert resp3.status_code == 200
        assert resp3.json() == []


# ---------------------------------------------------------------------------
# Session / auth robustness tests
# ---------------------------------------------------------------------------

class TestSessionAuth:
    def test_login_creates_db_session(self, app_and_db):
        """Login stores a token in the sessions table."""
        client, _, TestSession = app_and_db
        resp = client.post("/api/login", json={"username": "testuser", "password": "testpass"})
        assert resp.status_code == 200
        token = resp.json()["token"]
        # Verify the token exists in DB
        session = TestSession()
        row = session.query(SessionModel).filter(SessionModel.token == token).first()
        assert row is not None
        assert row.user_id is not None
        session.close()

    def test_logout_revokes_token(self, app_and_db):
        """Logout deletes the session from DB so the token is no longer valid."""
        client, token, TestSession = app_and_db
        headers = {"Authorization": f"Bearer {token}"}
        # Token works
        resp = client.get("/api/devices", headers=headers)
        assert resp.status_code == 200
        # Logout
        resp = client.post("/api/logout", headers=headers)
        assert resp.status_code == 200
        # Token no longer works
        resp = client.get("/api/devices", headers=headers)
        assert resp.status_code == 401

    def test_register_returns_valid_token(self, client):
        """Register returns a token that can be used immediately."""
        resp = client.post("/api/register", json={
            "username": "sessionuser",
            "email": "session@example.com",
            "password": "pass123",
        })
        token = resp.json()["token"]
        resp = client.get("/api/devices", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_expired_token_rejected(self, app_and_db):
        """Manually expire a session token and confirm it is rejected."""
        client, _, TestSession = app_and_db
        resp = client.post("/api/login", json={"username": "testuser", "password": "testpass"})
        token = resp.json()["token"]
        # Manually expire it
        session = TestSession()
        row = session.query(SessionModel).filter(SessionModel.token == token).first()
        row.expires_at = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        session.commit()
        session.close()
        # Now it should be rejected
        resp = client.get("/api/devices", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Position endpoint tests
# ---------------------------------------------------------------------------

class TestPositionEndpoints:
    def _create_device(self, client, auth_headers, name="PosDevice", identifier="pos-001"):
        resp = client.post("/api/devices", json={"name": name, "identifier": identifier}, headers=auth_headers)
        return resp.json()["id"]

    def test_upsert_position(self, client, auth_headers):
        device_id = self._create_device(client, auth_headers)
        ts = datetime.datetime.utcnow().isoformat()
        resp = client.post("/api/positions", json={
            "positions": [{"device_id": device_id, "latitude": 47.0, "longitude": 8.0, "timestamp": ts}],
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["upserted"] == 1

        # Upsert again with new coords
        ts2 = (datetime.datetime.utcnow() + datetime.timedelta(seconds=10)).isoformat()
        resp = client.post("/api/positions", json={
            "positions": [{"device_id": device_id, "latitude": 47.1, "longitude": 8.1, "timestamp": ts2}],
        }, headers=auth_headers)
        assert resp.json()["upserted"] == 1

        # Get positions — should have exactly one entry for this device
        resp = client.get("/api/positions", headers=auth_headers)
        positions = resp.json()
        matching = [p for p in positions if p["device_id"] == device_id]
        assert len(matching) == 1
        assert abs(matching[0]["latitude"] - 47.1) < 0.01

    def test_get_all_positions_cross_user(self, app_and_db):
        """All users can see all positions."""
        client, token1, TestSession = app_and_db
        headers1 = {"Authorization": f"Bearer {token1}"}

        # Create second user
        resp = client.post("/api/register", json={
            "username": "user2", "email": "user2@example.com", "password": "pass2",
        })
        token2 = resp.json()["token"]
        headers2 = {"Authorization": f"Bearer {token2}"}

        # Each user creates a device and uploads position
        d1 = client.post("/api/devices", json={"name": "D1", "identifier": "pos-u1"}, headers=headers1).json()["id"]
        d2 = client.post("/api/devices", json={"name": "D2", "identifier": "pos-u2"}, headers=headers2).json()["id"]

        ts = datetime.datetime.utcnow().isoformat()
        client.post("/api/positions", json={
            "positions": [{"device_id": d1, "latitude": 1.0, "longitude": 2.0, "timestamp": ts}],
        }, headers=headers1)
        client.post("/api/positions", json={
            "positions": [{"device_id": d2, "latitude": 3.0, "longitude": 4.0, "timestamp": ts}],
        }, headers=headers2)

        # User1 should see both positions
        resp = client.get("/api/positions", headers=headers1)
        positions = resp.json()
        device_ids = {p["device_id"] for p in positions}
        assert d1 in device_ids
        assert d2 in device_ids

    def test_staleness_flag(self, app_and_db):
        """Position older than TTL should be marked stale."""
        client, token, TestSession = app_and_db
        headers = {"Authorization": f"Bearer {token}"}

        device_id = client.post("/api/devices", json={"name": "Stale", "identifier": "stale-001"}, headers=headers).json()["id"]
        old_ts = (datetime.datetime.utcnow() - datetime.timedelta(seconds=600)).isoformat()
        client.post("/api/positions", json={
            "positions": [{"device_id": device_id, "latitude": 1.0, "longitude": 2.0, "timestamp": old_ts}],
        }, headers=headers)

        resp = client.get("/api/positions", headers=headers)
        matching = [p for p in resp.json() if p["device_id"] == device_id]
        assert len(matching) == 1
        assert matching[0]["is_stale"] is True

    def test_relay_dedup(self, app_and_db):
        """Relay only updates position if timestamp is newer."""
        client, token, TestSession = app_and_db
        headers = {"Authorization": f"Bearer {token}"}

        # Create two devices for same user
        d_target = client.post("/api/devices", json={"name": "Target", "identifier": "relay-target"}, headers=headers).json()["id"]
        d_relay = client.post("/api/devices", json={"name": "Relay", "identifier": "relay-src"}, headers=headers).json()["id"]

        # Upload direct position
        ts1 = datetime.datetime.utcnow().isoformat()
        client.post("/api/positions", json={
            "positions": [{"device_id": d_target, "latitude": 10.0, "longitude": 20.0, "timestamp": ts1}],
        }, headers=headers)

        # Relay an OLDER timestamp — should be ignored
        ts_old = (datetime.datetime.utcnow() - datetime.timedelta(seconds=60)).isoformat()
        resp = client.post("/api/positions/relay", json={
            "relayed_by_device_id": d_relay,
            "positions": [{"device_id": d_target, "latitude": 99.0, "longitude": 99.0, "timestamp": ts_old}],
        }, headers=headers)
        assert resp.json()["relayed"] == 0

        # Verify position unchanged
        resp = client.get("/api/positions", headers=headers)
        matching = [p for p in resp.json() if p["device_id"] == d_target]
        assert abs(matching[0]["latitude"] - 10.0) < 0.01

        # Relay a NEWER timestamp — should update
        ts_new = (datetime.datetime.utcnow() + datetime.timedelta(seconds=60)).isoformat()
        resp = client.post("/api/positions/relay", json={
            "relayed_by_device_id": d_relay,
            "positions": [{"device_id": d_target, "latitude": 50.0, "longitude": 60.0, "timestamp": ts_new}],
        }, headers=headers)
        assert resp.json()["relayed"] == 1

        resp = client.get("/api/positions", headers=headers)
        matching = [p for p in resp.json() if p["device_id"] == d_target]
        assert abs(matching[0]["latitude"] - 50.0) < 0.01
        assert matching[0]["relayed_by_device_id"] == d_relay
