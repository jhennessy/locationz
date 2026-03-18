"""SQLAlchemy models for users, devices, locations, visits, and places."""

import datetime
from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)

    devices = relationship("Device", back_populates="owner", cascade="all, delete-orphan")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    identifier = Column(String, unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen = Column(DateTime, nullable=True)

    owner = relationship("User", back_populates="devices")
    locations = relationship("Location", back_populates="device", cascade="all, delete-orphan")


class Location(Base):
    __tablename__ = "locations"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    altitude = Column(Float, nullable=True)
    horizontal_accuracy = Column(Float, nullable=True)
    vertical_accuracy = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    course = Column(Float, nullable=True)
    timestamp = Column(DateTime, nullable=False)
    received_at = Column(DateTime, default=datetime.datetime.utcnow)
    batch_id = Column(String, nullable=True, index=True)
    notes = Column(Text, nullable=True)

    device = relationship("Device", back_populates="locations")


class Place(Base):
    """A canonical location that the user has visited at least once.

    When a new visit is detected, it is snapped to the nearest existing Place
    within a threshold radius, or a new Place is created.
    """

    __tablename__ = "places"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    name = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    visit_count = Column(Integer, default=0)
    total_duration_seconds = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    owner = relationship("User")
    visits = relationship("Visit", back_populates="place", cascade="all, delete-orphan")


class Visit(Base):
    """A detected stay at a Place (duration >= 5 minutes)."""

    __tablename__ = "visits"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    place_id = Column(Integer, ForeignKey("places.id"), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    arrival = Column(DateTime, nullable=False)
    departure = Column(DateTime, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    address = Column(Text, nullable=True)
    is_open = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    device = relationship("Device")
    place = relationship("Place", back_populates="visits")


class DeviceState(Base):
    """Persists the visit-detection state machine across batch uploads."""

    __tablename__ = "device_states"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), unique=True, nullable=False)
    state = Column(String, nullable=False, default="unknown")  # stationary, moving, unknown
    anchor_latitude = Column(Float, nullable=True)
    anchor_longitude = Column(Float, nullable=True)
    arrived_at = Column(DateTime, nullable=True)
    last_confirmed_at = Column(DateTime, nullable=True)
    open_visit_id = Column(Integer, ForeignKey("visits.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    device = relationship("Device")
    open_visit = relationship("Visit", foreign_keys=[open_visit_id])


class Config(Base):
    """Key-value store for algorithm thresholds and other settings."""

    __tablename__ = "config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, nullable=False, index=True)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ReprocessingJob(Base):
    """Journal entry tracking a data regeneration run."""

    __tablename__ = "reprocessing_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String, nullable=False, default="running")  # running, completed, failed
    started_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    visits_created = Column(Integer, default=0)
    places_created = Column(Integer, default=0)

    user = relationship("User")


class Session(Base):
    """Database-backed session token — survives server restarts."""

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    device_info = Column(String, nullable=True)

    user = relationship("User")


class CurrentPosition(Base):
    """Live position for a device — upserted on each update, never historised."""

    __tablename__ = "current_positions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id"), unique=True, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    altitude = Column(Float, nullable=True)
    accuracy = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    timestamp = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    relayed_by_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)

    user = relationship("User")
    device = relationship("Device", foreign_keys=[device_id])
