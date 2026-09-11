"""Persistent application settings for the YP Ground Station."""
import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, Text

from app.auth import Base, get_db_session


class DeconflictionSettings(Base):
    """Global vehicle collision-avoidance settings."""
    __tablename__ = "deconfliction_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False, nullable=False)
    global_radius_m = Column(Float, default=10.0, nullable=False)
    radius_per_type_json = Column(Text, default="{}", nullable=False)
    orbit_radius_m = Column(Float, default=50.0, nullable=False)
    max_pause_duration_s = Column(Float, default=300.0, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ApplicationSettings(Base):
    """Persistent values used by the ground-station Settings modal."""
    __tablename__ = "application_settings"

    id = Column(Integer, primary_key=True)
    values_json = Column(Text, default="{}", nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


APPLICATION_SETTING_DEFAULTS: dict[str, Any] = {
    "trail_seconds": 45.0,
    "show_yp_range_rings": True,
    "message_retention_seconds": 600.0,
    "rtb_update_hz": 2.0,
    "rtb_stern_distance_m": 35.0,
    "rtb_altitude_m": 30.0,
    "rtb_yp_safe_distance_m": 20.0,
    "land_on_boat_hover_clearance_m": 0.5,
    "land_on_boat_descent_rate_ms": 0.5,
    "land_on_boat_pad_offset_m": -0.4,
    "land_on_boat_alignment_radius_m": 1.0,
    "mob_track_seconds": 120.0,
    "mob_swath_m": 20.0,
    "mob_altitude_m": 30.0,
    "mob_corridor_half_width_m": 50.0,
    "mob_takeoff_altitude_m": 30.0,
    "mob_climb_speed_ms": 8.0,
    "yp_role_vehicle_id": None,
    "rtk_source_type": "serial",
    "rtk_host_or_port": "/dev/ttyACM0",
    "rtk_network_port": 9000,
    "rtk_baudrate": 115200,
}


def _json_object(value: str) -> dict[str, Any]:
    """Treat missing, malformed, or non-object persisted JSON as empty settings."""
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _positive_number(key: str, value: Any) -> float:
    numeric = float(value)
    if isinstance(value, bool) or not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{key} must be a finite number greater than zero")
    return numeric


def normalize_application_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete update before committing or changing runtime state."""
    normalized = {}
    bounds = {
        "message_retention_seconds": (60, 30 * 24 * 60 * 60),
        "rtb_update_hz": (0.2, 20.0),
        "rtk_network_port": (1, 65535),
    }
    for key, value in payload.items():
        if key not in APPLICATION_SETTING_DEFAULTS:
            continue
        if key == "show_yp_range_rings":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
        elif key == "yp_role_vehicle_id":
            value = str(value).strip() if value and str(value).strip() else None
        elif key == "rtk_source_type":
            if value not in ("serial", "tcp", "udp", "disabled"):
                raise ValueError("rtk_source_type must be serial, tcp, udp, or disabled")
        elif key == "rtk_host_or_port":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("rtk_host_or_port must be a non-empty hostname or serial path")
            value = value.strip()
        elif key == "land_on_boat_pad_offset_m":
            value = float(value)
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
        else:
            value = _positive_number(key, value)
            if key in bounds:
                minimum, maximum = bounds[key]
                if not minimum <= value <= maximum:
                    raise ValueError(f"{key} must be between {minimum} and {maximum}")
            if key in ("rtk_network_port", "rtk_baudrate"):
                if not value.is_integer():
                    raise ValueError(f"{key} must be an integer")
                value = int(value)
        normalized[key] = value
    return normalized


def get_application_settings() -> dict[str, Any]:
    """Return persisted modal settings merged with application defaults."""
    with get_db_session() as session:
        record = session.query(ApplicationSettings).first()
        if not record:
            record = ApplicationSettings(values_json=json.dumps(APPLICATION_SETTING_DEFAULTS))
            session.add(record)
            session.commit()
        return {**APPLICATION_SETTING_DEFAULTS, **_json_object(record.values_json)}


def update_application_settings(payload: dict[str, Any]) -> tuple[bool, str]:
    """Persist settings atomically after applying the same validation as the API."""
    try:
        normalized = normalize_application_settings(payload)
    except (TypeError, ValueError, OverflowError) as error:
        return False, f"Invalid application settings: {error}"

    with get_db_session() as session:
        try:
            record = session.query(ApplicationSettings).first()
            if not record:
                record = ApplicationSettings(values_json="{}")
                session.add(record)
            stored = {**APPLICATION_SETTING_DEFAULTS, **_json_object(record.values_json), **normalized}
            record.values_json = json.dumps(stored)
            session.commit()
            return True, "Application settings updated"
        except Exception as error:
            session.rollback()
            return False, f"Error updating application settings: {error}"


def get_deconfliction_settings() -> dict[str, Any]:
    """Return the current deconfliction settings, creating defaults if absent."""
    session = get_db_session()
    try:
        settings = session.query(DeconflictionSettings).first()
        if not settings:
            settings = DeconflictionSettings()
            session.add(settings)
            session.commit()

        radius_per_type = _json_object(settings.radius_per_type_json)

        return {
            "id": settings.id,
            "enabled": settings.enabled,
            "global_radius_m": settings.global_radius_m,
            "radius_per_type": radius_per_type,
            "orbit_radius_m": settings.orbit_radius_m,
            "max_pause_duration_s": settings.max_pause_duration_s,
        }
    except Exception as error:
        print(f"[DECONFLICTION] Error loading settings: {error}")
        return {
            "enabled": False,
            "global_radius_m": 10.0,
            "radius_per_type": {},
            "orbit_radius_m": 50.0,
            "max_pause_duration_s": 300.0,
        }
    finally:
        session.close()


def update_deconfliction_settings(payload: dict[str, Any]) -> tuple[bool, str]:
    """Update the supplied deconfliction settings."""
    session = get_db_session()
    try:
        settings = session.query(DeconflictionSettings).first()
        if not settings:
            settings = DeconflictionSettings()
            session.add(settings)

        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("enabled must be a boolean")
            settings.enabled = payload["enabled"]
        for key in ("global_radius_m", "orbit_radius_m", "max_pause_duration_s"):
            if key in payload:
                setattr(settings, key, _positive_number(key, payload[key]))
        if "radius_per_type" in payload:
            radii = payload["radius_per_type"]
            if not isinstance(radii, dict):
                raise ValueError("radius_per_type must be an object")
            settings.radius_per_type_json = json.dumps({
                vehicle_type: _positive_number(f"radius_per_type.{vehicle_type}", radius)
                for vehicle_type, radius in radii.items()
            })

        session.commit()
        return True, "Deconfliction settings updated"
    except (TypeError, ValueError, OverflowError) as error:
        session.rollback()
        return False, f"Invalid deconfliction settings: {error}"
    except Exception as error:
        session.rollback()
        return False, f"Error updating settings: {error}"
    finally:
        session.close()
