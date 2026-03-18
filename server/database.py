"""Database setup and session management using SQLAlchemy + SQLite."""

import logging
import os

logger = logging.getLogger(__name__)

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATA_DIR = os.environ.get("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'locations.db')}")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables, run migrations, and seed the default admin user."""
    from models import User, Device, Location, Place, Visit, Config, ReprocessingJob, Session, CurrentPosition, DeviceState  # noqa: F401

    logger.info("Initializing database at %s", DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    _migrate()
    _seed_admin()
    _seed_config()
    _fix_stale_geofence_timestamps()


def _migrate():
    """Add any missing columns to existing tables."""
    insp = inspect(engine)
    if "users" in insp.get_table_names():
        columns = {c["name"] for c in insp.get_columns("users")}
        if "is_admin" not in columns:
            logger.info("Migrating: adding is_admin column to users table")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0"))

    if "locations" in insp.get_table_names():
        columns = {c["name"] for c in insp.get_columns("locations")}
        if "notes" not in columns:
            logger.info("Migrating: adding notes column to locations table")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE locations ADD COLUMN notes TEXT"))

    if "visits" in insp.get_table_names():
        columns = {c["name"] for c in insp.get_columns("visits")}
        if "is_open" not in columns:
            logger.info("Migrating: adding is_open column to visits table")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE visits ADD COLUMN is_open BOOLEAN DEFAULT 0"))


def _seed_admin():
    """Create the default admin user if it doesn't exist."""
    from auth import hash_password
    from models import User

    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            admin = User(
                username="admin",
                email="admin@localhost",
                password_hash=hash_password("admin"),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            logger.info("Default admin user created")
    finally:
        db.close()


# Default algorithm thresholds (must match processing.py module-level constants)
DEFAULT_THRESHOLDS = {
    "max_horizontal_accuracy_m": "100.0",
    "visit_radius_m": "50.0",
    "min_visit_duration_s": "300",
    "place_snap_radius_m": "75.0",
    "position_ttl_seconds": "300",
}


def _seed_config():
    """Insert default algorithm thresholds if not present."""
    from models import Config

    db = SessionLocal()
    try:
        for key, value in DEFAULT_THRESHOLDS.items():
            if not db.query(Config).filter(Config.key == key).first():
                db.add(Config(key=key, value=value))
        db.commit()
    finally:
        db.close()


def _fix_stale_geofence_timestamps():
    """One-time migration: fix geofence exit points that carry stale timestamps.

    The iOS app's recordStateChange used the CLLocation.timestamp from the last
    cached location for geofence exit events, which was often the *arrival*
    timestamp instead of the actual departure time.  This made exit+getting-fix
    point pairs appear to be at the same time as the preceding sleep event,
    collapsing multi-hour stays into 30-second clusters.

    For each stale geofence exit, we find the next real GPS fix (which has a
    correct CoreLocation timestamp) and copy its timestamp to the exit point
    and its companion "Getting fix" point.
    """
    from models import Config, Location, User

    db = SessionLocal()
    try:
        # Already done?
        if db.query(Config).filter(Config.key == "migration_stale_geofence_fixed").first():
            return

        # Find all geofence exit points and check if they're stale.
        # A geofence exit is stale when the next real GPS fix (notes IS NULL)
        # has a timestamp >60s newer than the exit point itself.
        geofence_exits = (
            db.query(Location)
            .filter(Location.notes.like("Geofence exit%"))
            .order_by(Location.id.asc())
            .all()
        )

        if not geofence_exits:
            logger.info("Stale geofence migration: no geofence exit points found, skipping")
            db.add(Config(key="migration_stale_geofence_fixed", value="done"))
            db.commit()
            return

        fixed_count = 0
        for exit_pt in geofence_exits:
            # Find the next real GPS point (no notes) on the same device
            next_gps = (
                db.query(Location)
                .filter(
                    Location.device_id == exit_pt.device_id,
                    Location.id > exit_pt.id,
                    Location.notes.is_(None),
                )
                .order_by(Location.id.asc())
                .first()
            )

            if next_gps is None:
                continue

            gap = (next_gps.timestamp - exit_pt.timestamp).total_seconds()
            if gap <= 60:
                # Not stale — timestamps are close, this exit is fine
                continue

            logger.info(
                "Fixing stale geofence exit id=%d device=%d: "
                "old_ts=%s → new_ts=%s (gap was %.0fs)",
                exit_pt.id, exit_pt.device_id,
                exit_pt.timestamp, next_gps.timestamp, gap,
            )
            exit_pt.timestamp = next_gps.timestamp
            fixed_count += 1

            # Also fix the companion "→ Getting fix" point (id + 1) if it has
            # the same stale timestamp
            getting_fix = db.query(Location).filter(Location.id == exit_pt.id + 1).first()
            if (
                getting_fix
                and getting_fix.device_id == exit_pt.device_id
                and getting_fix.notes
                and "Getting fix" in getting_fix.notes
            ):
                getting_fix.timestamp = next_gps.timestamp
                fixed_count += 1

        db.add(Config(key="migration_stale_geofence_fixed", value="done"))
        db.commit()
        logger.info("Stale geofence migration complete: fixed %d points", fixed_count)

        if fixed_count > 0:
            # Reprocess in a background thread so we don't block the event loop
            # (reprocess_all does synchronous DB work + Nominatim HTTP calls)
            import threading

            def _reprocess_background():
                from processing import reprocess_all
                bg_db = SessionLocal()
                try:
                    users = bg_db.query(User).all()
                    for user in users:
                        logger.info("Reprocessing user=%d after stale geofence fix", user.id)
                        reprocess_all(bg_db, user.id)
                    logger.info("Background reprocess after geofence fix complete")
                except Exception:
                    logger.exception("Background reprocess failed")
                finally:
                    bg_db.close()

            threading.Thread(target=_reprocess_background, daemon=True).start()
    finally:
        db.close()
