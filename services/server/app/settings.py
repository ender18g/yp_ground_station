"""Persistent application settings for the YP Ground Station."""
import json
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
    "mob_track_seconds": 120.0,
    "mob_swath_m": 20.0,
    "mob_altitude_m": 30.0,
    "mob_corridor_half_width_m": 50.0,
    "mob_takeoff_altitude_m": 30.0,
    "mob_climb_speed_ms": 8.0,
    "yp_role_vehicle_id": None,
}


def get_application_settings() -> dict[str, Any]:
    """Return persisted modal settings merged with application defaults."""
    session = get_db_session()
    try:
        record = session.query(ApplicationSettings).first()
        if not record:
            record = ApplicationSettings(values_json=json.dumps(APPLICATION_SETTING_DEFAULTS))
            session.add(record)
            session.commit()
            stored: dict[str, Any] = {}
        else:
            try:
                stored = json.loads(record.values_json or "{}")
            except json.JSONDecodeError:
                stored = {}
        return {**APPLICATION_SETTING_DEFAULTS, **stored}
    finally:
        session.close()


def update_application_settings(payload: dict[str, Any]) -> tuple[bool, str]:
    """Persist validated values used by the Settings modal."""
    session = get_db_session()
    try:
        record = session.query(ApplicationSettings).first()
        if not record:
            record = ApplicationSettings(values_json=json.dumps(APPLICATION_SETTING_DEFAULTS))
            session.add(record)
            stored = dict(APPLICATION_SETTING_DEFAULTS)
        else:
            try:
                stored = {**APPLICATION_SETTING_DEFAULTS, **json.loads(record.values_json or "{}")}
            except json.JSONDecodeError:
                stored = dict(APPLICATION_SETTING_DEFAULTS)

        for key, value in payload.items():
            if key not in APPLICATION_SETTING_DEFAULTS:
                continue
            if key == "show_yp_range_rings":
                stored[key] = bool(value)
            elif key == "yp_role_vehicle_id":
                stored[key] = str(value).strip() if value and str(value).strip() else None
            else:
                numeric_value = float(value)
                if numeric_value <= 0:
                    raise ValueError(f"{key} must be greater than zero")
                stored[key] = numeric_value

        record.values_json = json.dumps(stored)
        session.commit()
        return True, "Application settings updated"
    except (TypeError, ValueError) as error:
        session.rollback()
        return False, f"Invalid application settings: {error}"
    except Exception as error:
        session.rollback()
        return False, f"Error updating application settings: {error}"
    finally:
        session.close()


def get_deconfliction_settings() -> dict[str, Any]:
    """Return the current deconfliction settings, creating defaults if absent."""
    session = get_db_session()
    try:
        settings = session.query(DeconflictionSettings).first()
        if not settings:
            settings = DeconflictionSettings()
            session.add(settings)
            session.commit()

        try:
            radius_per_type = json.loads(settings.radius_per_type_json or "{}")
        except json.JSONDecodeError:
            radius_per_type = {}

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
            settings.enabled = bool(payload["enabled"])
        if "global_radius_m" in payload:
            value = float(payload["global_radius_m"])
            if value > 0:
                settings.global_radius_m = value
        if "radius_per_type" in payload:
            settings.radius_per_type_json = json.dumps(payload["radius_per_type"])
        if "orbit_radius_m" in payload:
            value = float(payload["orbit_radius_m"])
            if value > 0:
                settings.orbit_radius_m = value
        if "max_pause_duration_s" in payload:
            value = float(payload["max_pause_duration_s"])
            if value > 0:
                settings.max_pause_duration_s = value

        session.commit()
        return True, "Deconfliction settings updated"
    except (TypeError, ValueError) as error:
        session.rollback()
        return False, f"Invalid deconfliction settings: {error}"
    except Exception as error:
        session.rollback()
        return False, f"Error updating settings: {error}"
    finally:
        session.close()
