"""NiceGUI web pages: login, registration, devices, map, visits, frequent places."""

import datetime
import os
from zoneinfo import ZoneInfo
from nicegui import ui, app
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import create_token, hash_password, verify_password, decode_token
from database import SessionLocal, DEFAULT_THRESHOLDS
from models import User, Device, Location, Visit, Place, Config, ReprocessingJob
from processing import reprocess_all


async def _ensure_timezone():
    """Detect browser timezone via JS and store in the user session."""
    if "timezone" not in app.storage.user:
        try:
            tz = await ui.run_javascript(
                "Intl.DateTimeFormat().resolvedOptions().timeZone",
                timeout=5.0,
            )
            if tz:
                app.storage.user["timezone"] = tz
        except TimeoutError:
            pass


def _fmt(dt: datetime.datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a naive-UTC datetime in the browser's local timezone."""
    if dt is None:
        return "-"
    tz_name = app.storage.user.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except KeyError:
        tz = ZoneInfo("UTC")
    utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return utc_dt.astimezone(tz).strftime(fmt)


def get_session_user() -> tuple[Session, User | None]:
    """Return a DB session and the currently logged-in user (or None)."""
    db = SessionLocal()
    token = app.storage.user.get("token")
    if not token:
        return db, None
    payload = decode_token(token, db)
    if payload is None:
        return db, None
    user = db.query(User).filter(User.id == payload["sub"]).first()
    return db, user


def _admin_user_selector(db, current_user):
    """If admin, show a user picker and return (selected_user_id, selector). Else (user.id, None)."""
    if not current_user.is_admin:
        return current_user.id, None
    all_users = db.query(User).order_by(User.username).all()
    user_options = {u.id: u.username for u in all_users}
    selector = ui.select(
        options=user_options,
        label="View as user",
        value=current_user.id,
    ).classes("w-64 q-mb-md").props('outlined dense')
    return current_user.id, selector


def _selected_uid(user_selector, fallback_user) -> int:
    """Get the effective user ID from the admin selector, always as int."""
    if user_selector and user_selector.value is not None:
        return int(user_selector.value)
    return fallback_user.id


def _nav_link(icon: str, label: str, href: str):
    """Render a navigation link with an icon."""
    with ui.element("a").props(f'href="{href}"').classes(
        "flex items-center gap-3 q-pa-sm q-pl-md no-underline text-dark"
        " rounded-borders cursor-pointer hover:bg-blue-2"
    ).style("text-decoration: none; transition: background 0.15s"):
        ui.icon(icon).classes("text-blue-8")
        ui.label(label)


def _nav_drawer(user=None):
    """Shared left-drawer navigation."""
    with ui.left_drawer().classes("bg-blue-1"):
        ui.label("Locationz").classes("text-h6 q-pa-sm q-mb-sm")
        _nav_link("dashboard", "Dashboard", "/")
        _nav_link("phone_iphone", "Devices", "/devices")
        _nav_link("map", "Map", "/map")
        _nav_link("my_location", "Positions", "/positions")
        _nav_link("place", "Visits", "/visits")
        _nav_link("star", "Frequent Places", "/places")
        ui.separator().classes("q-my-sm")
        _nav_link("settings", "Settings", "/settings")
        if user and user.is_admin:
            _nav_link("admin_panel_settings", "Admin", "/admin")
            _nav_link("article", "Logs", "/logs")


def _header(user):
    """Shared header with logout."""
    def logout():
        app.storage.user.clear()
        ui.navigate.to("/login")

    with ui.header().classes("items-center justify-between"):
        ui.label("Locationz").classes("text-h6")
        with ui.row().classes("items-center"):
            ui.label(f"Logged in as {user.username}")
            ui.button("Logout", on_click=logout).props("flat color=white")


def _format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_min = minutes % 60
    if hours < 24:
        return f"{hours}h {remaining_min}m"
    days = hours // 24
    remaining_hrs = hours % 24
    return f"{days}d {remaining_hrs}h"


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------
@ui.page("/login")
async def login_page():
    await _ensure_timezone()

    def do_login():
        db = SessionLocal()
        user = db.query(User).filter(User.username == username.value).first()
        if user and verify_password(password.value, user.password_hash):
            token = create_token(user.id, user.username, db)
            app.storage.user["token"] = token
            app.storage.user["username"] = user.username
            ui.navigate.to("/")
        else:
            ui.notify("Invalid username or password", type="negative")
        db.close()

    with ui.column().classes("absolute-center items-center"):
        ui.label("Locationz").classes("text-h4 q-mb-md")
        ui.label("Sign in to your account").classes("text-subtitle1 q-mb-lg")
        with ui.card().classes("w-80"):
            username = ui.input("Username").classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")
            ui.button("Login", on_click=do_login).classes("w-full q-mt-md")
            with ui.row().classes("w-full justify-center q-mt-sm"):
                ui.label("No account?")
                ui.link("Register", "/register")


# ---------------------------------------------------------------------------
# Registration page
# ---------------------------------------------------------------------------
@ui.page("/register")
async def register_page():
    await _ensure_timezone()

    def do_register():
        if not username.value or not email.value or not password.value:
            ui.notify("All fields are required", type="warning")
            return
        if password.value != confirm.value:
            ui.notify("Passwords do not match", type="warning")
            return
        db = SessionLocal()
        existing = db.query(User).filter(
            (User.username == username.value) | (User.email == email.value)
        ).first()
        if existing:
            ui.notify("Username or email already taken", type="negative")
            db.close()
            return
        user = User(
            username=username.value,
            email=email.value,
            password_hash=hash_password(password.value),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_token(user.id, user.username, db)
        app.storage.user["token"] = token
        app.storage.user["username"] = user.username
        db.close()
        ui.navigate.to("/")

    with ui.column().classes("absolute-center items-center"):
        ui.label("Create Account").classes("text-h4 q-mb-md")
        with ui.card().classes("w-80"):
            username = ui.input("Username").classes("w-full")
            email = ui.input("Email").classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")
            confirm = ui.input("Confirm Password", password=True, password_toggle_button=True).classes("w-full")
            ui.button("Register", on_click=do_register).classes("w-full q-mt-md")
            with ui.row().classes("w-full justify-center q-mt-sm"):
                ui.label("Already have an account?")
                ui.link("Login", "/login")


# ---------------------------------------------------------------------------
# Dashboard (home)
# ---------------------------------------------------------------------------
@ui.page("/")
async def dashboard_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Dashboard").classes("text-h5 q-mb-md")
        _, user_selector = _admin_user_selector(db, user)

        content = ui.column().classes("w-full")

        def render_dashboard():
            content.clear()
            inner_db = SessionLocal()
            uid = _selected_uid(user_selector, user)

            device_count = inner_db.query(Device).filter(Device.user_id == uid).count()
            location_count = (
                inner_db.query(Location).join(Device).filter(Device.user_id == uid).count()
            )
            visit_count = (
                inner_db.query(Visit).join(Device).filter(Device.user_id == uid).count()
            )
            place_count = inner_db.query(Place).filter(Place.user_id == uid).count()

            with content:
                with ui.row().classes("q-gutter-md"):
                    with ui.card().classes("w-48"):
                        ui.label("Devices").classes("text-subtitle2 text-grey")
                        ui.label(str(device_count)).classes("text-h4")
                    with ui.card().classes("w-48"):
                        ui.label("Location Points").classes("text-subtitle2 text-grey")
                        ui.label(str(location_count)).classes("text-h4")
                    with ui.card().classes("w-48"):
                        ui.label("Visits").classes("text-subtitle2 text-grey")
                        ui.label(str(visit_count)).classes("text-h4")
                    with ui.card().classes("w-48"):
                        ui.label("Known Places").classes("text-subtitle2 text-grey")
                        ui.label(str(place_count)).classes("text-h4")

                # Recent locations
                ui.label("Recent Activity").classes("text-h6 q-mt-lg q-mb-sm")
                recent = (
                    inner_db.query(Location)
                    .join(Device)
                    .filter(Device.user_id == uid)
                    .order_by(Location.received_at.desc())
                    .limit(10)
                    .all()
                )
                if recent:
                    rows = [
                        {
                            "device": loc.device.name,
                            "lat": f"{loc.latitude:.6f}",
                            "lon": f"{loc.longitude:.6f}",
                            "time": _fmt(loc.timestamp),
                            "received": _fmt(loc.received_at),
                            "notes": loc.notes or "",
                        }
                        for loc in recent
                    ]
                    columns = [
                        {"name": "device", "label": "Device", "field": "device"},
                        {"name": "lat", "label": "Latitude", "field": "lat"},
                        {"name": "lon", "label": "Longitude", "field": "lon"},
                        {"name": "time", "label": "Device Time", "field": "time"},
                        {"name": "received", "label": "Received", "field": "received"},
                        {"name": "notes", "label": "Notes", "field": "notes", "align": "left"},
                    ]
                    ui.table(columns=columns, rows=rows).classes("w-full")
                else:
                    ui.label("No location data yet. Connect a device to start tracking.").classes("text-grey")

            inner_db.close()

        if user_selector:
            user_selector.on_value_change(lambda _: render_dashboard())
        render_dashboard()

        commit_sha = os.environ.get("COMMIT_SHA", "")[:8]
        if commit_sha:
            ui.label(f"Build: {commit_sha}").classes("text-caption text-grey q-mt-lg")

    db.close()


# ---------------------------------------------------------------------------
# Device management page
# ---------------------------------------------------------------------------
@ui.page("/devices")
async def devices_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Device Management").classes("text-h5 q-mb-md")
        _, user_selector = _admin_user_selector(db, user)

        with ui.card().classes("q-mb-lg w-96"):
            ui.label("Register New Device").classes("text-h6 q-mb-sm")
            device_name = ui.input("Device Name (e.g. John's iPhone)").classes("w-full")
            device_id = ui.input("Device Identifier (unique)").classes("w-full")

            def add_device():
                if not device_name.value or not device_id.value:
                    ui.notify("Both fields are required", type="warning")
                    return
                inner_db = SessionLocal()
                existing = inner_db.query(Device).filter(Device.identifier == device_id.value).first()
                if existing:
                    ui.notify("Device identifier already registered", type="negative")
                    inner_db.close()
                    return
                target_uid = _selected_uid(user_selector, user)
                device = Device(name=device_name.value, identifier=device_id.value, user_id=target_uid)
                inner_db.add(device)
                inner_db.commit()
                inner_db.close()
                render_devices()

            ui.button("Add Device", on_click=add_device).classes("q-mt-sm")

        devices_container = ui.column().classes("w-full")

        def render_devices():
            devices_container.clear()
            inner_db = SessionLocal()
            uid = _selected_uid(user_selector, user)
            devices = inner_db.query(Device).filter(Device.user_id == uid).all()

            with devices_container:
                target_user = inner_db.query(User).filter(User.id == uid).first()
                label = f"Devices for {target_user.username}" if target_user and target_user.id != user.id else "Your Devices"
                ui.label(label).classes("text-h6 q-mb-sm")

                if devices:
                    for d in devices:
                        loc_count = inner_db.query(Location).filter(Location.device_id == d.id).count()
                        visit_count = inner_db.query(Visit).filter(Visit.device_id == d.id).count()
                        with ui.card().classes("w-full q-mb-sm"):
                            with ui.row().classes("items-center justify-between w-full"):
                                with ui.column():
                                    ui.label(d.name).classes("text-subtitle1 text-bold")
                                    ui.label(f"ID: {d.identifier}").classes("text-caption text-grey")
                                    ui.label(f"{loc_count} points | {visit_count} visits").classes("text-caption")
                                    if d.last_seen:
                                        ui.label(f"Last seen: {_fmt(d.last_seen, '%Y-%m-%d %H:%M')}").classes(
                                            "text-caption text-grey"
                                        )

                                def make_delete(did):
                                    def delete():
                                        ddb = SessionLocal()
                                        dev = ddb.query(Device).filter(Device.id == did).first()
                                        if dev:
                                            ddb.delete(dev)
                                            ddb.commit()
                                        ddb.close()
                                        render_devices()
                                    return delete

                                ui.button("Delete", on_click=make_delete(d.id)).props("flat color=red")
                else:
                    ui.label("No devices registered.").classes("text-grey")

            inner_db.close()

        if user_selector:
            user_selector.on_value_change(lambda _: render_devices())
        render_devices()

    db.close()


# ---------------------------------------------------------------------------
# Map page — debug UI with point navigation
# ---------------------------------------------------------------------------
@ui.page("/map")
async def map_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Location Map").classes("text-h5 q-mb-md")
        _, user_selector = _admin_user_selector(db, user)

        uid = _selected_uid(user_selector, user)
        devices = db.query(Device).filter(Device.user_id == uid).all()
        device_options = {d.id: d.name for d in devices}
        selected_device = ui.select(
            options=device_options,
            label="Select Device",
            value=devices[0].id if devices else None,
        ).classes("w-64 q-mb-md")

        # Day navigation state
        today = datetime.date.today()
        day_state = {"date": today}
        map_container = ui.column().classes("w-full")

        def refresh_devices():
            inner_db = SessionLocal()
            uid = _selected_uid(user_selector, user)
            devs = inner_db.query(Device).filter(Device.user_id == uid).all()
            inner_db.close()
            selected_device.options = {d.id: d.name for d in devs}
            selected_device.value = devs[0].id if devs else None
            render_map()

        def render_map():
            map_container.clear()
            if not selected_device.value:
                with map_container:
                    ui.label("No location data available.").classes("text-grey")
                return

            tz_name = app.storage.user.get("timezone", "UTC")
            try:
                tz = ZoneInfo(tz_name)
            except KeyError:
                tz = ZoneInfo("UTC")

            # Convert selected day to UTC range
            local_start = datetime.datetime.combine(day_state["date"], datetime.time.min, tzinfo=tz)
            local_end = local_start + datetime.timedelta(days=1)
            utc_start = local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            utc_end = local_end.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

            inner_db = SessionLocal()
            locations = (
                inner_db.query(Location)
                .filter(
                    Location.device_id == selected_device.value,
                    Location.timestamp >= utc_start,
                    Location.timestamp < utc_end,
                )
                .order_by(Location.timestamp.asc())
                .all()
            )

            points = [
                {
                    "idx": i,
                    "lat": loc.latitude,
                    "lon": loc.longitude,
                    "alt": loc.altitude,
                    "speed": loc.speed,
                    "course": loc.course,
                    "h_acc": loc.horizontal_accuracy,
                    "v_acc": loc.vertical_accuracy,
                    "ts": loc.timestamp,
                    "received": loc.received_at,
                    "batch": loc.batch_id,
                    "notes": loc.notes,
                }
                for i, loc in enumerate(locations)
            ]
            inner_db.close()

            n = len(points)
            tracked_layers = []

            def speed_color(spd):
                if spd is None:
                    return "#999999"
                if spd < 1:
                    return "#2196F3"  # blue — stationary
                if spd < 3:
                    return "#4CAF50"  # green — walking
                if spd < 15:
                    return "#FF9800"  # orange — driving
                return "#F44336"      # red — fast

            def acc_radius(h_acc):
                """Point radius proportional to GPS error. Bigger = worse accuracy."""
                if h_acc is None:
                    return 4
                return max(3, min(18, h_acc / 3))

            with map_container:
                # --- Day navigation bar ---
                with ui.row().classes("w-full items-center justify-center q-gutter-sm q-mb-sm"):
                    def nav_day(delta):
                        day_state["date"] += datetime.timedelta(days=delta)
                        render_map()

                    ui.button(icon="navigate_before", on_click=lambda: nav_day(-1)).props("flat dense")
                    d = day_state["date"]
                    if d == today:
                        day_label = "Today"
                    elif d == today - datetime.timedelta(days=1):
                        day_label = "Yesterday"
                    else:
                        day_label = d.strftime("%a %d %b %Y")
                    ui.button(day_label, on_click=lambda: (day_state.update(date=today), render_map())).props("flat").classes("text-h6")
                    ui.button(icon="navigate_next", on_click=lambda: nav_day(1)).props("flat dense").bind_enabled_from(
                        globals(), lambda: day_state["date"] < today,
                    ) if day_state["date"] < today else ui.button(icon="navigate_next").props("flat dense disabled")
                    ui.label(f"{n} points").classes("text-caption text-grey q-ml-md")

                if not points:
                    ui.label("No location data for this day.").classes("text-grey q-pa-lg")
                    return

                # --- Map ---
                center_lat = sum(p["lat"] for p in points) / n
                center_lon = sum(p["lon"] for p in points) / n
                m = ui.leaflet(center=(center_lat, center_lon), zoom=14).classes("w-full").style("height: 600px")

                # --- Legend ---
                with ui.row().classes("q-gutter-sm items-center q-mt-xs"):
                    for color, label in [
                        ("#2196F3", "Stationary"),
                        ("#4CAF50", "Walking"),
                        ("#FF9800", "Driving"),
                        ("#F44336", "Fast"),
                        ("#999999", "Unknown"),
                    ]:
                        ui.html(f'<span style="display:inline-block;width:12px;height:12px;'
                                f'border-radius:50%;background:{color};margin-right:2px"></span>').classes("q-ml-sm")
                        ui.label(label).classes("text-caption")
                    ui.label("(point size = GPS error)").classes("text-caption text-grey q-ml-md")

                # --- Render all points for this day ---
                # Polyline connecting all points
                if len(points) >= 2:
                    path = [[pt["lat"], pt["lon"]] for pt in points]
                    m.generic_layer(
                        name="polyline",
                        args=[path, {"color": "#4285F4", "weight": 2, "opacity": 0.4}],
                    )

                # Circle markers — size reflects accuracy error
                for pt in points:
                    color = speed_color(pt["speed"])
                    radius = acc_radius(pt["h_acc"])
                    layer = m.generic_layer(
                        name="circleMarker",
                        args=[
                            [pt["lat"], pt["lon"]],
                            {
                                "radius": radius,
                                "color": color,
                                "fillColor": color,
                                "fillOpacity": 0.7,
                                "weight": 1,
                            },
                        ],
                    )
                    spd_str = f'{pt["speed"]:.1f} m/s' if pt["speed"] is not None else "n/a"
                    acc_str = f'{pt["h_acc"]:.0f}m' if pt["h_acc"] is not None else "n/a"
                    ts_str = _fmt(pt["ts"])
                    tip = f'<b>#{pt["idx"]}</b><br>{ts_str}<br>Speed: {spd_str}<br>Acc: {acc_str}'
                    if pt["notes"]:
                        tip += f'<br><i>{pt["notes"]}</i>'
                    m.run_layer_method(layer.id, "bindTooltip", tip)

                # Auto-fit bounds
                if n >= 2:
                    lats = [p["lat"] for p in points]
                    lons = [p["lon"] for p in points]
                    m.run_map_method("fitBounds", [[min(lats), min(lons)], [max(lats), max(lons)]], {"padding": [30, 30]})

        if user_selector:
            user_selector.on_value_change(lambda _: refresh_devices())
        selected_device.on_value_change(lambda _: render_map())
        render_map()

    db.close()


# ---------------------------------------------------------------------------
# Positions page
# ---------------------------------------------------------------------------
@ui.page("/positions")
async def positions_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Positions").classes("text-h5 q-mb-md")
        ui.label(
            "Raw GPS positions recorded by the device."
        ).classes("text-caption text-grey q-mb-md")
        _, user_selector = _admin_user_selector(db, user)

        uid = _selected_uid(user_selector, user)
        devices = db.query(Device).filter(Device.user_id == uid).all()
        device_options = {d.id: d.name for d in devices}
        selected_device = ui.select(
            options=device_options,
            label="Select Device",
            value=devices[0].id if devices else None,
        ).classes("w-64 q-mb-md")

        content = ui.column().classes("w-full")

        def refresh_devices():
            inner_db = SessionLocal()
            uid = _selected_uid(user_selector, user)
            devs = inner_db.query(Device).filter(Device.user_id == uid).all()
            inner_db.close()
            selected_device.options = {d.id: d.name for d in devs}
            selected_device.value = devs[0].id if devs else None
            render_positions()

        def render_positions():
            content.clear()
            if not selected_device.value:
                with content:
                    ui.label("No position data available.").classes("text-grey")
                return
            inner_db = SessionLocal()
            locations = (
                inner_db.query(Location)
                .filter(Location.device_id == selected_device.value)
                .order_by(Location.timestamp.desc())
                .limit(2000)
                .all()
            )

            with content:
                if not locations:
                    ui.label("No positions recorded yet.").classes("text-grey")
                    inner_db.close()
                    return

                rows = [
                    {
                        "id": loc.id,
                        "time": _fmt(loc.timestamp),
                        "lat": round(loc.latitude, 6),
                        "lon": round(loc.longitude, 6),
                        "altitude": f"{loc.altitude:.1f}m" if loc.altitude is not None else "-",
                        "speed": f"{loc.speed:.1f} m/s" if loc.speed is not None else "-",
                        "course": f"{loc.course:.0f}\u00b0" if loc.course is not None else "-",
                        "h_acc": f"{loc.horizontal_accuracy:.0f}m" if loc.horizontal_accuracy is not None else "-",
                        "v_acc": f"{loc.vertical_accuracy:.0f}m" if loc.vertical_accuracy is not None else "-",
                        "received": _fmt(loc.received_at),
                        "batch": loc.batch_id or "-",
                        "notes": loc.notes or "-",
                    }
                    for loc in locations
                ]

                ui.aggrid({
                    "columnDefs": [
                        {"headerName": "#", "field": "id", "width": 80, "sortable": True, "filter": "agNumberColumnFilter"},
                        {"headerName": "Time", "field": "time", "width": 180, "sortable": True, "filter": True},
                        {"headerName": "Lat", "field": "lat", "width": 120, "sortable": True, "filter": "agNumberColumnFilter"},
                        {"headerName": "Lon", "field": "lon", "width": 120, "sortable": True, "filter": "agNumberColumnFilter"},
                        {"headerName": "Altitude", "field": "altitude", "width": 100, "sortable": True, "filter": True},
                        {"headerName": "Speed", "field": "speed", "width": 110, "sortable": True, "filter": True},
                        {"headerName": "Course", "field": "course", "width": 90, "sortable": True, "filter": True},
                        {"headerName": "H. Acc", "field": "h_acc", "width": 90, "sortable": True, "filter": True},
                        {"headerName": "V. Acc", "field": "v_acc", "width": 90, "sortable": True, "filter": True},
                        {"headerName": "Received", "field": "received", "width": 180, "sortable": True, "filter": True},
                        {"headerName": "Batch", "field": "batch", "width": 120, "sortable": True, "filter": True},
                        {"headerName": "Notes", "field": "notes", "width": 150, "sortable": True, "filter": True},
                    ],
                    "rowData": rows,
                    "defaultColDef": {"resizable": True},
                    "pagination": True,
                    "paginationPageSize": 50,
                }).classes("w-full").style("height: 600px")

            inner_db.close()

        if user_selector:
            user_selector.on_value_change(lambda _: refresh_devices())
        selected_device.on_value_change(lambda _: render_positions())
        render_positions()

    db.close()


# ---------------------------------------------------------------------------
# Visits page
# ---------------------------------------------------------------------------
@ui.page("/visits")
async def visits_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Visits").classes("text-h5 q-mb-md")
        ui.label(
            "Places where you stayed for at least 5 minutes, detected automatically from GPS data."
        ).classes("text-caption text-grey q-mb-md")
        _, user_selector = _admin_user_selector(db, user)

        uid = _selected_uid(user_selector, user)
        devices = db.query(Device).filter(Device.user_id == uid).all()
        device_options = {d.id: d.name for d in devices}
        selected_device = ui.select(
            options=device_options,
            label="Select Device",
            value=devices[0].id if devices else None,
        ).classes("w-64 q-mb-md")

        content = ui.column().classes("w-full")

        def refresh_devices():
            inner_db = SessionLocal()
            uid = _selected_uid(user_selector, user)
            devs = inner_db.query(Device).filter(Device.user_id == uid).all()
            inner_db.close()
            selected_device.options = {d.id: d.name for d in devs}
            selected_device.value = devs[0].id if devs else None
            render_visits()

        def render_visits():
            content.clear()
            if not selected_device.value:
                with content:
                    ui.label("No visit data available.").classes("text-grey")
                return
            inner_db = SessionLocal()
            visits = (
                inner_db.query(Visit)
                .filter(Visit.device_id == selected_device.value)
                .order_by(Visit.arrival.desc())
                .limit(200)
                .all()
            )

            with content:
                if not visits:
                    ui.label("No visits detected yet. Upload more location data.").classes("text-grey")
                    inner_db.close()
                    return

                # Map showing visit locations
                center = visits[0]
                m = ui.leaflet(center=(center.latitude, center.longitude), zoom=13).classes("w-full").style(
                    "height: 400px"
                )
                for v in visits:
                    m.marker(latlng=(v.latitude, v.longitude))

                # Visit table
                rows = [
                    {
                        "address": v.address or f"{v.latitude:.5f}, {v.longitude:.5f}",
                        "arrival": _fmt(v.arrival, "%Y-%m-%d %H:%M"),
                        "departure": _fmt(v.departure, "%H:%M"),
                        "duration": _format_duration(v.duration_seconds),
                        "place_id": v.place_id,
                    }
                    for v in visits
                ]
                columns = [
                    {"name": "address", "label": "Location", "field": "address", "align": "left"},
                    {"name": "arrival", "label": "Arrived", "field": "arrival"},
                    {"name": "departure", "label": "Left", "field": "departure"},
                    {"name": "duration", "label": "Duration", "field": "duration"},
                ]
                ui.table(columns=columns, rows=rows).classes("w-full q-mt-md")

            inner_db.close()

        if user_selector:
            user_selector.on_value_change(lambda _: refresh_devices())
        selected_device.on_value_change(lambda _: render_visits())
        render_visits()

    db.close()


# ---------------------------------------------------------------------------
# Frequent Places page
# ---------------------------------------------------------------------------
@ui.page("/places")
async def places_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Frequent Places").classes("text-h5 q-mb-md")
        ui.label(
            "Locations you visit repeatedly, ranked by number of visits."
        ).classes("text-caption text-grey q-mb-md")
        _, user_selector = _admin_user_selector(db, user)

        content = ui.column().classes("w-full")

        def render_places():
            content.clear()
            inner_db = SessionLocal()
            uid = _selected_uid(user_selector, user)

            places = (
                inner_db.query(Place)
                .filter(Place.user_id == uid)
                .order_by(Place.visit_count.desc())
                .all()
            )

            with content:
                if not places:
                    ui.label("No places detected yet. Visit detection runs automatically when locations are uploaded.").classes(
                        "text-grey"
                    )
                    inner_db.close()
                    return

                # Map with all known places
                m = ui.leaflet(center=(places[0].latitude, places[0].longitude), zoom=12).classes("w-full").style(
                    "height: 400px"
                )
                for p in places:
                    m.marker(latlng=(p.latitude, p.longitude))

                # Table
                rows = [
                    {
                        "name": p.name or "-",
                        "address": p.address or f"{p.latitude:.5f}, {p.longitude:.5f}",
                        "visits": p.visit_count,
                        "total_time": _format_duration(p.total_duration_seconds),
                        "avg_time": _format_duration(p.total_duration_seconds // p.visit_count) if p.visit_count else "-",
                        "pid": p.id,
                    }
                    for p in places
                ]
                columns = [
                    {"name": "name", "label": "Name", "field": "name", "align": "left"},
                    {"name": "address", "label": "Address", "field": "address", "align": "left"},
                    {"name": "visits", "label": "Visits", "field": "visits"},
                    {"name": "total_time", "label": "Total Time", "field": "total_time"},
                    {"name": "avg_time", "label": "Avg Duration", "field": "avg_time"},
                ]
                ui.table(columns=columns, rows=rows).classes("w-full q-mt-md")

                # Inline rename
                ui.label("Rename a place").classes("text-h6 q-mt-lg q-mb-sm")
                place_options = {p.id: (p.name or p.address or f"Place #{p.id}") for p in places}
                sel_place = ui.select(options=place_options, label="Select Place").classes("w-64")
                new_name = ui.input("New Name").classes("w-64")

                def rename_place():
                    if not sel_place.value or not new_name.value:
                        ui.notify("Select a place and enter a name", type="warning")
                        return
                    rdb = SessionLocal()
                    place = rdb.query(Place).filter(Place.id == sel_place.value).first()
                    if place:
                        place.name = new_name.value
                        rdb.commit()
                        ui.notify(f"Renamed to '{new_name.value}'", type="positive")
                    rdb.close()
                    render_places()

                ui.button("Rename", on_click=rename_place).classes("q-mt-sm")

            inner_db.close()

        if user_selector:
            user_selector.on_value_change(lambda _: render_places())
        render_places()

    db.close()


# ---------------------------------------------------------------------------
# Settings page — change password
# ---------------------------------------------------------------------------
@ui.page("/settings")
async def settings_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Settings").classes("text-h5 q-mb-md")

        with ui.card().classes("w-96"):
            ui.label("Change Password").classes("text-h6 q-mb-sm")
            current_pw = ui.input("Current Password", password=True, password_toggle_button=True).classes("w-full")
            new_pw = ui.input("New Password", password=True, password_toggle_button=True).classes("w-full")
            confirm_pw = ui.input("Confirm New Password", password=True, password_toggle_button=True).classes("w-full")

            def do_change_password():
                if not current_pw.value or not new_pw.value:
                    ui.notify("All fields are required", type="warning")
                    return
                if new_pw.value != confirm_pw.value:
                    ui.notify("New passwords do not match", type="warning")
                    return
                inner_db = SessionLocal()
                u = inner_db.query(User).filter(User.id == user.id).first()
                if not verify_password(current_pw.value, u.password_hash):
                    ui.notify("Current password is incorrect", type="negative")
                    inner_db.close()
                    return
                u.password_hash = hash_password(new_pw.value)
                inner_db.commit()
                inner_db.close()
                current_pw.value = ""
                new_pw.value = ""
                confirm_pw.value = ""
                ui.notify("Password changed successfully", type="positive")

            ui.button("Change Password", on_click=do_change_password).classes("q-mt-md")

    db.close()


# ---------------------------------------------------------------------------
# Admin page — user management + algorithm tuning (admin only)
# ---------------------------------------------------------------------------

# Threshold labels for the admin UI
_THRESHOLD_LABELS = {
    "max_horizontal_accuracy_m": ("Max GPS Accuracy (m)", "Discard GPS points with accuracy worse than this"),
    "visit_radius_m": ("Visit Radius (m)", "Max distance from anchor to count as stationary (50m = P95 of real scatter)"),
    "min_visit_duration_s": ("Min Visit Duration (s)", "Minimum seconds to count as a visit (300 = 5 min)"),
    "place_snap_radius_m": ("Place Snap Radius (m)", "Snap visit to existing place if within this distance"),
}


@ui.page("/admin")
async def admin_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return
    if not user.is_admin:
        ui.navigate.to("/")
        return

    _header(user)
    _nav_drawer(user)

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Administration").classes("text-h5 q-mb-md")

        with ui.tabs().classes("w-full") as tabs:
            users_tab = ui.tab("Users", icon="people")
            algo_tab = ui.tab("Algorithm", icon="tune")

        with ui.tab_panels(tabs, value=users_tab).classes("w-full"):
            # ------ Users tab ------
            with ui.tab_panel(users_tab):
                _render_users_tab(user)

            # ------ Algorithm tab ------
            with ui.tab_panel(algo_tab):
                _render_algorithm_tab(user)

    db.close()


def _render_users_tab(current_user):
    """Users management tab content."""
    users_container = ui.column().classes("w-full")

    def render_users():
        users_container.clear()
        inner_db = SessionLocal()
        all_users = inner_db.query(User).order_by(User.id).all()

        with users_container:
            for u in all_users:
                with ui.card().classes("w-full q-mb-sm"):
                    with ui.row().classes("items-center justify-between w-full"):
                        with ui.column():
                            with ui.row().classes("items-center q-gutter-sm"):
                                ui.label(u.username).classes("text-subtitle1 text-bold")
                                if u.is_admin:
                                    ui.badge("admin", color="blue")
                                if not u.is_active:
                                    ui.badge("disabled", color="red")
                            ui.label(f"{u.email}").classes("text-caption text-grey")
                            ui.label(
                                f"Created: {_fmt(u.created_at, '%Y-%m-%d %H:%M')}"
                            ).classes("text-caption text-grey")

                        with ui.row().classes("q-gutter-sm"):
                            def make_toggle_active(uid, currently_active):
                                def toggle():
                                    tdb = SessionLocal()
                                    target = tdb.query(User).filter(User.id == uid).first()
                                    if target:
                                        target.is_active = not currently_active
                                        tdb.commit()
                                    tdb.close()
                                    render_users()
                                return toggle

                            def make_toggle_admin(uid, currently_admin):
                                def toggle():
                                    tdb = SessionLocal()
                                    target = tdb.query(User).filter(User.id == uid).first()
                                    if target:
                                        target.is_admin = not currently_admin
                                        tdb.commit()
                                    tdb.close()
                                    render_users()
                                return toggle

                            def make_delete(uid):
                                def delete():
                                    if uid == current_user.id:
                                        ui.notify("Cannot delete yourself", type="warning")
                                        return
                                    tdb = SessionLocal()
                                    target = tdb.query(User).filter(User.id == uid).first()
                                    if target:
                                        tdb.delete(target)
                                        tdb.commit()
                                    tdb.close()
                                    render_users()
                                return delete

                            if u.id != current_user.id:
                                label = "Disable" if u.is_active else "Enable"
                                ui.button(label, on_click=make_toggle_active(u.id, u.is_active)).props("flat")
                                admin_label = "Remove Admin" if u.is_admin else "Make Admin"
                                ui.button(admin_label, on_click=make_toggle_admin(u.id, u.is_admin)).props("flat")
                                ui.button("Delete", on_click=make_delete(u.id)).props("flat color=red")

            # Reset password section
            ui.separator().classes("q-my-md")
            ui.label("Reset User Password").classes("text-h6 q-mb-sm")
            user_options = {u.id: u.username for u in all_users}
            if user_options:
                sel_user = ui.select(options=user_options, label="Select User").classes("w-64")
                reset_pw = ui.input("New Password", password=True, password_toggle_button=True).classes("w-64")

                def do_reset():
                    if not sel_user.value or not reset_pw.value:
                        ui.notify("Select a user and enter a password", type="warning")
                        return
                    tdb = SessionLocal()
                    target = tdb.query(User).filter(User.id == sel_user.value).first()
                    if target:
                        target.password_hash = hash_password(reset_pw.value)
                        tdb.commit()
                        ui.notify(f"Password reset for {target.username}", type="positive")
                        reset_pw.value = ""
                    tdb.close()

                ui.button("Reset Password", on_click=do_reset).classes("q-mt-sm")

        inner_db.close()

    render_users()


def _render_algorithm_tab(current_user):
    """Algorithm thresholds and data regeneration tab."""
    # --- Threshold editor ---
    ui.label("Detection Thresholds").classes("text-h6 q-mb-sm")
    ui.label(
        "These parameters control how GPS data is filtered and how visits and places are detected."
    ).classes("text-caption text-grey q-mb-md")

    inner_db = SessionLocal()
    config_rows = inner_db.query(Config).all()
    current_values = {r.key: r.value for r in config_rows}
    inner_db.close()

    inputs = {}
    with ui.card().classes("w-full q-mb-lg"):
        for key in _THRESHOLD_LABELS:
            label, hint = _THRESHOLD_LABELS[key]
            val = float(current_values.get(key, DEFAULT_THRESHOLDS[key]))
            inp = ui.number(label, value=val).classes("w-full").tooltip(hint)
            inputs[key] = inp

        with ui.row().classes("q-mt-md q-gutter-sm"):
            def save_thresholds():
                tdb = SessionLocal()
                for key, inp in inputs.items():
                    row = tdb.query(Config).filter(Config.key == key).first()
                    if row:
                        row.value = str(inp.value)
                    else:
                        tdb.add(Config(key=key, value=str(inp.value)))
                tdb.commit()
                tdb.close()
                ui.notify("Thresholds saved", type="positive")

            def reset_defaults():
                tdb = SessionLocal()
                for key, default_val in DEFAULT_THRESHOLDS.items():
                    row = tdb.query(Config).filter(Config.key == key).first()
                    if row:
                        row.value = default_val
                tdb.commit()
                tdb.close()
                for key, inp in inputs.items():
                    inp.value = float(DEFAULT_THRESHOLDS[key])
                ui.notify("Reset to defaults", type="info")

            ui.button("Save Thresholds", on_click=save_thresholds).props("color=primary")
            ui.button("Reset to Defaults", on_click=reset_defaults).props("flat")

    # --- Regeneration ---
    ui.separator().classes("q-my-md")
    ui.label("Data Regeneration").classes("text-h6 q-mb-sm")
    ui.label(
        "Delete all detected visits and places, then reprocess all location data "
        "using the current thresholds. This may take a while for large datasets."
    ).classes("text-caption text-grey q-mb-md")

    journal_container = ui.column().classes("w-full")

    def render_journal():
        journal_container.clear()
        jdb = SessionLocal()
        jobs = (
            jdb.query(ReprocessingJob)
            .filter(ReprocessingJob.user_id == current_user.id)
            .order_by(ReprocessingJob.started_at.desc())
            .limit(10)
            .all()
        )

        with journal_container:
            if jobs:
                rows = []
                for j in jobs:
                    rows.append({
                        "id": j.id,
                        "status": j.status,
                        "started": _fmt(j.started_at),
                        "finished": _fmt(j.finished_at),
                        "visits": j.visits_created,
                        "places": j.places_created,
                        "error": j.error_message or "",
                    })
                columns = [
                    {"name": "status", "label": "Status", "field": "status"},
                    {"name": "started", "label": "Started", "field": "started"},
                    {"name": "finished", "label": "Finished", "field": "finished"},
                    {"name": "visits", "label": "Visits", "field": "visits"},
                    {"name": "places", "label": "Places", "field": "places"},
                    {"name": "error", "label": "Error", "field": "error"},
                ]
                ui.table(columns=columns, rows=rows).classes("w-full")
            else:
                ui.label("No regeneration jobs yet.").classes("text-grey")

        jdb.close()

    def do_regenerate():
        import datetime as dt

        jdb = SessionLocal()
        job = ReprocessingJob(
            user_id=current_user.id,
            status="running",
            started_at=dt.datetime.utcnow(),
        )
        jdb.add(job)
        jdb.commit()
        jdb.refresh(job)
        job_id = job.id
        jdb.close()

        render_journal()
        ui.notify("Regeneration started...", type="info")

        try:
            rdb = SessionLocal()
            result = reprocess_all(rdb, current_user.id)

            job = rdb.query(ReprocessingJob).filter(ReprocessingJob.id == job_id).first()
            job.status = "completed"
            job.finished_at = dt.datetime.utcnow()
            job.visits_created = result["visits_created"]
            job.places_created = result["places_created"]
            rdb.commit()
            rdb.close()

            ui.notify(
                f"Regeneration complete: {result['visits_created']} visits, "
                f"{result['places_created']} places",
                type="positive",
            )
        except Exception as e:
            rdb = SessionLocal()
            job = rdb.query(ReprocessingJob).filter(ReprocessingJob.id == job_id).first()
            if job:
                job.status = "failed"
                job.finished_at = dt.datetime.utcnow()
                job.error_message = str(e)
                rdb.commit()
            rdb.close()
            ui.notify(f"Regeneration failed: {e}", type="negative")

        render_journal()

    ui.button("Regenerate All Data", on_click=do_regenerate).props("color=negative icon=refresh")
    ui.label("").classes("q-mb-md")
    render_journal()


# ---------------------------------------------------------------------------
# Logs page — admin only
# ---------------------------------------------------------------------------
@ui.page("/logs")
async def logs_page():
    await _ensure_timezone()
    db, user = get_session_user()
    if user is None:
        ui.navigate.to("/login")
        return
    if not user.is_admin:
        ui.navigate.to("/")
        return

    _header(user)
    _nav_drawer(user)

    LOG_DIR = os.environ.get("LOG_DIR", "/data" if os.path.isdir("/data") else ".")
    LOG_FILE = os.path.join(LOG_DIR, "locationz.log")

    with ui.column().classes("q-pa-md w-full"):
        ui.label("Server Logs").classes("text-h5 q-mb-md")

        log_area = ui.textarea("").classes("w-full font-mono").props(
            "readonly outlined autogrow"
        ).style("min-height: 500px; font-size: 12px;")

        def load_logs(tail_lines=200):
            try:
                with open(LOG_FILE, "r") as f:
                    lines = f.readlines()
                log_area.value = "".join(lines[-tail_lines:])
            except FileNotFoundError:
                log_area.value = "Log file not found."

        with ui.row().classes("q-gutter-sm q-mb-md"):
            ui.button("Refresh", on_click=lambda: load_logs()).props("icon=refresh")
            ui.button("Last 50", on_click=lambda: load_logs(50)).props("flat")
            ui.button("Last 200", on_click=lambda: load_logs(200)).props("flat")
            ui.button("Last 500", on_click=lambda: load_logs(500)).props("flat")
            ui.button("All", on_click=lambda: load_logs(100000)).props("flat")

        load_logs()

    db.close()
