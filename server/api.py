"""REST API endpoints for the mobile apps (authentication, devices, location uploads, positions)."""

import datetime
import logging
import os
import uuid
from typing import Optional

import hashlib
import hmac

import requests as http_requests
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import create_token, decode_token, hash_password, verify_password, revoke_token, cleanup_expired_sessions
from database import get_db
from models import Device, Location, Place, User, Visit, CurrentPosition, Config
from processing import process_device_locations

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    user_id: int
    username: str


class DeviceCreate(BaseModel):
    name: str
    identifier: str


class DeviceResponse(BaseModel):
    id: int
    name: str
    identifier: str
    last_seen: Optional[str] = None

    class Config:
        from_attributes = True


class LocationPoint(BaseModel):
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    horizontal_accuracy: Optional[float] = None
    vertical_accuracy: Optional[float] = None
    speed: Optional[float] = None
    course: Optional[float] = None
    timestamp: str = Field(..., description="ISO 8601 timestamp from the device")
    notes: Optional[str] = None


class LocationBatch(BaseModel):
    device_id: int
    locations: list[LocationPoint]


class BatchResponse(BaseModel):
    received: int
    batch_id: str
    visits_detected: int = 0


class VisitResponse(BaseModel):
    id: int
    device_id: int
    place_id: int
    latitude: float
    longitude: float
    arrival: str
    departure: str
    duration_seconds: int
    address: Optional[str] = None
    is_open: bool = False

    class Config:
        from_attributes = True


class PlaceResponse(BaseModel):
    id: int
    latitude: float
    longitude: float
    name: Optional[str] = None
    address: Optional[str] = None
    visit_count: int
    total_duration_seconds: int

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class AdminUserResponse(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool
    is_admin: bool
    created_at: str

    class Config:
        from_attributes = True


class AdminUserUpdate(BaseModel):
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    new_password: Optional[str] = None


# --- Position schemas ---

class PositionPoint(BaseModel):
    device_id: int
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    speed: Optional[float] = None
    timestamp: str


class PositionBatch(BaseModel):
    positions: list[PositionPoint]


class RelayedPosition(BaseModel):
    device_id: int
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    speed: Optional[float] = None
    timestamp: str


class RelayBatch(BaseModel):
    relayed_by_device_id: int
    positions: list[RelayedPosition]


class PositionResponse(BaseModel):
    user_id: int
    username: str
    device_id: int
    device_name: str
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    speed: Optional[float] = None
    timestamp: str
    updated_at: str
    is_stale: bool
    relayed_by_device_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def get_current_user(authorization: str = Header(...), db: Session = Depends(get_db)) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    payload = decode_token(token, db)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(User).filter(User.id == payload["sub"]).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter((User.username == req.username) | (User.email == req.email)).first():
        raise HTTPException(status_code=409, detail="Username or email already exists")
    user = User(
        username=req.username,
        email=req.email,
        password_hash=hash_password(req.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("New user registered: %s (id=%d)", req.username, user.id)
    token = create_token(user.id, user.username, db)
    return TokenResponse(token=token, user_id=user.id, username=user.username)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if user is None or not verify_password(req.password, user.password_hash):
        logger.warning("Failed login attempt for username: %s", req.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    logger.info("User logged in: %s (id=%d)", user.username, user.id)
    # Clean up expired sessions on login
    cleanup_expired_sessions(db)
    token = create_token(user.id, user.username, db)
    return TokenResponse(token=token, user_id=user.id, username=user.username)


@router.post("/logout")
def logout(authorization: str = Header(...), db: Session = Depends(get_db)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    revoke_token(token, db)
    return {"status": "logged out"}


# ---------------------------------------------------------------------------
# Device endpoints
# ---------------------------------------------------------------------------

@router.get("/devices", response_model=list[DeviceResponse])
def list_devices(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    devices = db.query(Device).filter(Device.user_id == user.id).all()
    return [
        DeviceResponse(
            id=d.id,
            name=d.name,
            identifier=d.identifier,
            last_seen=d.last_seen.isoformat() if d.last_seen else None,
        )
        for d in devices
    ]


@router.post("/devices", response_model=DeviceResponse, status_code=201)
def create_device(req: DeviceCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(Device).filter(Device.identifier == req.identifier).first()
    if existing:
        raise HTTPException(status_code=409, detail="Device identifier already registered")
    device = Device(name=req.name, identifier=req.identifier, user_id=user.id)
    db.add(device)
    db.commit()
    db.refresh(device)
    logger.info("Device created: %s (id=%d) by user=%s", device.name, device.id, user.username)
    return DeviceResponse(id=device.id, name=device.name, identifier=device.identifier)


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == device_id, Device.user_id == user.id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete(device)
    db.commit()


# ---------------------------------------------------------------------------
# Location endpoints
# ---------------------------------------------------------------------------

@router.post("/locations", response_model=BatchResponse)
def upload_locations(batch: LocationBatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == batch.device_id, Device.user_id == user.id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found or not owned by user")

    batch_id = uuid.uuid4().hex[:12]
    now = datetime.datetime.utcnow()

    for pt in batch.locations:
        loc = Location(
            device_id=device.id,
            latitude=pt.latitude,
            longitude=pt.longitude,
            altitude=pt.altitude,
            horizontal_accuracy=pt.horizontal_accuracy,
            vertical_accuracy=pt.vertical_accuracy,
            speed=pt.speed,
            course=pt.course,
            timestamp=datetime.datetime.fromisoformat(pt.timestamp),
            received_at=now,
            batch_id=batch_id,
            notes=pt.notes,
        )
        db.add(loc)

    device.last_seen = now
    db.commit()

    logger.info(
        "Received %d locations from user=%s device=%d batch=%s",
        len(batch.locations), user.username, device.id, batch_id,
    )

    # Trigger visit detection pipeline
    new_visits = process_device_locations(db, device.id, user.id)

    return BatchResponse(received=len(batch.locations), batch_id=batch_id, visits_detected=len(new_visits))


@router.get("/locations/{device_id}")
def get_locations(
    device_id: int,
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.query(Device).filter(Device.id == device_id, Device.user_id == user.id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found or not owned by user")

    locations = (
        db.query(Location)
        .filter(Location.device_id == device_id)
        .order_by(Location.timestamp.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        {
            "id": loc.id,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "altitude": loc.altitude,
            "speed": loc.speed,
            "course": loc.course,
            "timestamp": loc.timestamp.isoformat(),
            "received_at": loc.received_at.isoformat(),
            "batch_id": loc.batch_id,
        }
        for loc in locations
    ]


# ---------------------------------------------------------------------------
# Visit endpoints
# ---------------------------------------------------------------------------

@router.get("/visits/{device_id}", response_model=list[VisitResponse])
def get_visits(
    device_id: int,
    limit: int = 100,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.query(Device).filter(Device.id == device_id, Device.user_id == user.id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found or not owned by user")

    query = db.query(Visit).filter(Visit.device_id == device_id)

    if start_date:
        try:
            start = datetime.datetime.fromisoformat(start_date)
            if start.tzinfo is not None:
                start = start.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            query = query.filter(Visit.arrival >= start)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format (use ISO 8601)")
    if end_date:
        try:
            end = datetime.datetime.fromisoformat(end_date)
            if end.tzinfo is not None:
                end = end.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            query = query.filter(Visit.arrival < end)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format (use ISO 8601)")

    visits = (
        query
        .order_by(Visit.arrival.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        VisitResponse(
            id=v.id,
            device_id=v.device_id,
            place_id=v.place_id,
            latitude=v.latitude,
            longitude=v.longitude,
            arrival=v.arrival.isoformat(),
            departure=v.departure.isoformat(),
            duration_seconds=v.duration_seconds,
            address=v.address,
            is_open=bool(v.is_open),
        )
        for v in visits
    ]


@router.post("/visits/{device_id}/reprocess")
def reprocess_visits(
    device_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete existing visits for a device and reprocess all locations."""
    device = db.query(Device).filter(Device.id == device_id, Device.user_id == user.id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found or not owned by user")

    db.query(Visit).filter(Visit.device_id == device_id).delete()
    db.commit()

    new_visits = process_device_locations(db, device.id, user.id)
    return {"reprocessed": True, "visits_detected": len(new_visits)}


# ---------------------------------------------------------------------------
# Place endpoints
# ---------------------------------------------------------------------------

@router.get("/places", response_model=list[PlaceResponse])
def get_places(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    places = (
        db.query(Place)
        .filter(Place.user_id == user.id)
        .order_by(Place.visit_count.desc())
        .all()
    )
    return [
        PlaceResponse(
            id=p.id,
            latitude=p.latitude,
            longitude=p.longitude,
            name=p.name,
            address=p.address,
            visit_count=p.visit_count,
            total_duration_seconds=p.total_duration_seconds,
        )
        for p in places
    ]


@router.get("/places/frequent", response_model=list[PlaceResponse])
def get_frequent_places(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the most frequently visited places, ordered by visit count."""
    places = (
        db.query(Place)
        .filter(Place.user_id == user.id, Place.visit_count >= 2)
        .order_by(Place.visit_count.desc())
        .limit(limit)
        .all()
    )
    return [
        PlaceResponse(
            id=p.id,
            latitude=p.latitude,
            longitude=p.longitude,
            name=p.name,
            address=p.address,
            visit_count=p.visit_count,
            total_duration_seconds=p.total_duration_seconds,
        )
        for p in places
    ]


@router.get("/places/{place_id}/visits", response_model=list[VisitResponse])
def get_place_visits(
    place_id: int,
    limit: int = 100,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all visits for a specific place."""
    place = db.query(Place).filter(Place.id == place_id, Place.user_id == user.id).first()
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")

    visits = (
        db.query(Visit)
        .filter(Visit.place_id == place_id)
        .order_by(Visit.arrival.desc())
        .limit(limit)
        .all()
    )
    return [
        VisitResponse(
            id=v.id,
            device_id=v.device_id,
            place_id=v.place_id,
            latitude=v.latitude,
            longitude=v.longitude,
            arrival=v.arrival.isoformat(),
            departure=v.departure.isoformat(),
            duration_seconds=v.duration_seconds,
            address=v.address,
            is_open=bool(v.is_open),
        )
        for v in visits
    ]


@router.put("/places/{place_id}/name")
def update_place_name(
    place_id: int,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    place = db.query(Place).filter(Place.id == place_id, Place.user_id == user.id).first()
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    place.name = body.get("name", place.name)
    db.commit()
    return {"id": place.id, "name": place.name}


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------

@router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(req.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    logger.info("Password changed for user=%s", user.username)
    return {"status": "password changed"}


# ---------------------------------------------------------------------------
# Position endpoints (live sharing)
# ---------------------------------------------------------------------------

def _get_position_ttl(db: Session) -> int:
    """Return position_ttl_seconds from config, default 300."""
    cfg = db.query(Config).filter(Config.key == "position_ttl_seconds").first()
    return int(cfg.value) if cfg else 300


@router.post("/positions")
def update_positions(batch: PositionBatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Upsert own device positions.  Does NOT trigger visit detection."""
    upserted = 0
    for pt in batch.positions:
        device = db.query(Device).filter(Device.id == pt.device_id, Device.user_id == user.id).first()
        if not device:
            continue
        ts = datetime.datetime.fromisoformat(pt.timestamp)
        existing = db.query(CurrentPosition).filter(CurrentPosition.device_id == pt.device_id).first()
        if existing:
            existing.latitude = pt.latitude
            existing.longitude = pt.longitude
            existing.altitude = pt.altitude
            existing.accuracy = pt.accuracy
            existing.speed = pt.speed
            existing.timestamp = ts
            existing.updated_at = datetime.datetime.utcnow()
            existing.relayed_by_device_id = None
        else:
            db.add(CurrentPosition(
                user_id=user.id,
                device_id=pt.device_id,
                latitude=pt.latitude,
                longitude=pt.longitude,
                altitude=pt.altitude,
                accuracy=pt.accuracy,
                speed=pt.speed,
                timestamp=ts,
                relayed_by_device_id=None,
            ))
        upserted += 1
    db.commit()
    return {"upserted": upserted}


@router.get("/positions", response_model=list[PositionResponse])
def get_all_positions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current positions for ALL users' devices (cross-user visibility)."""
    ttl = _get_position_ttl(db)
    now = datetime.datetime.utcnow()
    positions = db.query(CurrentPosition).all()
    results = []
    for p in positions:
        dev = db.query(Device).filter(Device.id == p.device_id).first()
        usr = db.query(User).filter(User.id == p.user_id).first()
        if not dev or not usr:
            continue
        age = (now - p.timestamp).total_seconds()
        results.append(PositionResponse(
            user_id=p.user_id,
            username=usr.username,
            device_id=p.device_id,
            device_name=dev.name,
            latitude=p.latitude,
            longitude=p.longitude,
            altitude=p.altitude,
            accuracy=p.accuracy,
            speed=p.speed,
            timestamp=p.timestamp.isoformat(),
            updated_at=p.updated_at.isoformat() if p.updated_at else p.timestamp.isoformat(),
            is_stale=age > ttl,
            relayed_by_device_id=p.relayed_by_device_id,
        ))
    return results


@router.post("/positions/relay")
def relay_positions(batch: RelayBatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Upload BLE-relayed peer positions.  Only updates if newer timestamp."""
    relay_device = db.query(Device).filter(
        Device.id == batch.relayed_by_device_id, Device.user_id == user.id
    ).first()
    if not relay_device:
        raise HTTPException(status_code=404, detail="Relay device not found or not owned by user")

    relayed = 0
    for pt in batch.positions:
        device = db.query(Device).filter(Device.id == pt.device_id).first()
        if not device:
            continue
        ts = datetime.datetime.fromisoformat(pt.timestamp)
        existing = db.query(CurrentPosition).filter(CurrentPosition.device_id == pt.device_id).first()
        if existing:
            if ts <= existing.timestamp:
                continue  # Only update if newer
            existing.latitude = pt.latitude
            existing.longitude = pt.longitude
            existing.altitude = pt.altitude
            existing.accuracy = pt.accuracy
            existing.speed = pt.speed
            existing.timestamp = ts
            existing.updated_at = datetime.datetime.utcnow()
            existing.relayed_by_device_id = batch.relayed_by_device_id
        else:
            db.add(CurrentPosition(
                user_id=device.user_id,
                device_id=pt.device_id,
                latitude=pt.latitude,
                longitude=pt.longitude,
                altitude=pt.altitude,
                accuracy=pt.accuracy,
                speed=pt.speed,
                timestamp=ts,
                relayed_by_device_id=batch.relayed_by_device_id,
            ))
        relayed += 1
    db.commit()
    return {"relayed": relayed}


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------

@router.get("/admin/users", response_model=list[AdminUserResponse])
def admin_list_users(admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.id).all()
    return [
        AdminUserResponse(
            id=u.id,
            username=u.username,
            email=u.email,
            is_active=u.is_active,
            is_admin=u.is_admin,
            created_at=u.created_at.isoformat() if u.created_at else "",
        )
        for u in users
    ]


@router.put("/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    req: AdminUserUpdate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if req.is_active is not None:
        target.is_active = req.is_active
    if req.is_admin is not None:
        target.is_admin = req.is_admin
    if req.new_password:
        target.password_hash = hash_password(req.new_password)
    db.commit()
    logger.info("Admin %s updated user %s (id=%d): active=%s admin=%s", admin.username, target.username, target.id, target.is_active, target.is_admin)
    return {"id": target.id, "username": target.username, "is_active": target.is_active, "is_admin": target.is_admin}


@router.delete("/admin/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Admin %s deleted user %s (id=%d)", admin.username, target.username, target.id)
    db.delete(target)
    db.commit()


# ---------------------------------------------------------------------------
# Data transfer (secured by DATA_SECRET env var)
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DATA_SECRET = os.environ.get("DATA_SECRET", "")
SKIP_FILES = {".DS_Store", "locationz.log"}


def _require_data_secret(x_data_secret: str = Header()):
    if not DATA_SECRET:
        raise HTTPException(status_code=503, detail="DATA_SECRET not configured on server")
    if not hmac.compare_digest(x_data_secret, DATA_SECRET):
        raise HTTPException(status_code=403, detail="Invalid secret")


@router.get("/data/status", dependencies=[Depends(_require_data_secret)])
def data_status():
    return {"ok": True, "data_dir": DATA_DIR}


@router.get("/data/checksums", dependencies=[Depends(_require_data_secret)])
def data_checksums():
    files = {}
    for root, _, filenames in os.walk(DATA_DIR):
        for name in filenames:
            if name in SKIP_FILES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, DATA_DIR)
            md5 = hashlib.md5()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    md5.update(chunk)
            files[rel] = {"md5": md5.hexdigest(), "size": os.path.getsize(full)}
    return {"files": files}


@router.post("/data/upload", dependencies=[Depends(_require_data_secret)])
async def data_upload(file: UploadFile, path: str = Header()):
    dest = os.path.realpath(os.path.join(DATA_DIR, path))
    if not dest.startswith(os.path.realpath(DATA_DIR)):
        raise HTTPException(status_code=400, detail=f"Bad path: {path}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    total = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
            total += len(chunk)
    logger.info("Data upload: %s (%d bytes)", path, total)
    return {"ok": True, "path": path, "bytes": total}


@router.get("/data/download", dependencies=[Depends(_require_data_secret)])
def data_download(path: str):
    dest = os.path.realpath(os.path.join(DATA_DIR, path))
    if not dest.startswith(os.path.realpath(DATA_DIR)):
        raise HTTPException(status_code=400, detail=f"Bad path: {path}")
    if not os.path.isfile(dest):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return FileResponse(dest, filename=os.path.basename(dest))
