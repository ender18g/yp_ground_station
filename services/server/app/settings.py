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
