"""Authentication and authorization module for TRIDENT YP Ground Station.

Provides JWT-based token authentication, user management, and permission checking
via SQLite backend.
"""
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

import jwt
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, selectinload, Session

# Configuration
DB_PATH = Path(os.getenv("AUTH_DB_PATH", "/data/auth.db"))
JWT_SECRET = os.getenv("JWT_SECRET", "yp-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "1440"))  # 24 hours

# Permission definitions: hierarchy and what each level includes
PERMISSION_LEVELS = {
    "view_only": [
        "read_telemetry",
        "read_vehicle_status",
    ],
    "waypoint_command": [
        "read_telemetry",
        "read_vehicle_status",
        "send_waypoint",
        "send_rtb",
        "set_vehicle_mode",
        "cancel_sar",
    ],
    "mission_planning": [
        "read_telemetry",
        "read_vehicle_status",
        "send_waypoint",
        "send_rtb",
        "set_vehicle_mode",
        "cancel_sar",
        "create_mission",
        "upload_mission",
        "search_grid",
    ],
    "man_overboard": [
        "read_telemetry",
        "read_vehicle_status",
        "send_waypoint",
        "send_rtb",
        "set_vehicle_mode",
        "cancel_sar",
        "create_mission",
        "upload_mission",
        "search_grid",
        "trigger_mob",
    ],
    "admin": [
        "read_telemetry",
        "read_vehicle_status",
        "send_waypoint",
        "send_rtb",
        "set_vehicle_mode",
        "cancel_sar",
        "create_mission",
        "upload_mission",
        "search_grid",
        "trigger_mob",
        "manage_sitl",
        "manage_users",
        "manage_settings",
        "manage_video_streams",
    ],
}
VALID_PERMISSIONS = frozenset(permission for level in PERMISSION_LEVELS.values() for permission in level)

# Database setup
Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=engine)


class User(Base):
    """User account with authentication credentials."""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    permissions = relationship("UserPermission", cascade="all, delete-orphan", back_populates="user")
    
    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission."""
        if not self.active:
            return False
        return any(p.permission == permission for p in self.permissions)
    
    def has_any_permission(self, permissions: Set[str]) -> bool:
        """Check if user has any of the given permissions."""
        if not self.active:
            return False
        user_perms = {p.permission for p in self.permissions}
        return bool(user_perms & permissions)
    
    def get_permissions(self) -> Set[str]:
        """Return set of all user permissions."""
        if not self.active:
            return set()
        return {p.permission for p in self.permissions}


class UserPermission(Base):
    """Permission assignment to a user."""
    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission", name="uq_user_permission"),)
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    permission = Column(String(50), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="permissions")


def init_database() -> None:
    """Initialize the SQLite database with schema. Creates default admin if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    
    # Create default admin user if no users exist
    session = SessionLocal()
    try:
        if session.query(User).count() == 0:
            admin_user = User(
                username="admin",
                password_hash=hash_password("admin"),
                active=True
            )
            session.add(admin_user)
            session.flush()
            
            # Assign all admin permissions
            for permission in PERMISSION_LEVELS["admin"]:
                perm = UserPermission(user_id=admin_user.id, permission=permission)
                session.add(perm)
            
            session.commit()
            print("[AUTH] Created default admin user (username=admin, password=admin). CHANGE THIS IMMEDIATELY!")
    finally:
        session.close()


def get_db_session() -> Session:
    """Get a new database session."""
    return SessionLocal()


def hash_password(password: str) -> str:
    """Hash a password using SHA256."""
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return f"{salt}${pwd_hash.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    try:
        salt, pwd_hash = password_hash.split("$")
        computed_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
        return hmac.compare_digest(computed_hash.hex(), pwd_hash)
    except (ValueError, AttributeError):
        return False


def create_access_token(username: str) -> str:
    """Create a JWT access token for a user."""
    now = datetime.now(timezone.utc)
    expiration = now + timedelta(minutes=JWT_EXPIRATION_MINUTES)
    
    payload = {
        "sub": username,
        "iat": now,
        "exp": expiration,
        "jti": secrets.token_urlsafe(16),  # JWT ID for revocation support
    }
    
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token. Returns token payload or None if invalid."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None


def get_current_user(token: Optional[str]) -> Optional[User]:
    """Get the current user from a JWT token."""
    if not token:
        return None
    
    payload = decode_token(token)
    if not payload:
        return None
    
    username = payload.get("sub")
    if not username:
        return None
    
    session = get_db_session()
    try:
        user = (
            session.query(User)
            .options(selectinload(User.permissions))
            .filter_by(username=username)
            .first()
        )
        if user:
            # WebSocket handlers retain this instance after the session closes.
            # Materialize the relationship so permission checks cannot lazy-load.
            list(user.permissions)
        return user
    finally:
        session.close()


def create_user(username: str, password: str, permission_level: str = "view_only") -> tuple[bool, str]:
    """Create a new user with specified permission level. Returns (success, message)."""
    if permission_level not in PERMISSION_LEVELS:
        return False, f"Invalid permission level: {permission_level}"
    
    session = get_db_session()
    try:
        # Check if user exists
        if session.query(User).filter_by(username=username).first():
            return False, f"User '{username}' already exists"
        
        # Create user
        user = User(
            username=username,
            password_hash=hash_password(password),
            active=True
        )
        session.add(user)
        session.flush()
        
        # Assign permissions
        for permission in PERMISSION_LEVELS[permission_level]:
            perm = UserPermission(user_id=user.id, permission=permission)
            session.add(perm)
        
        session.commit()
        return True, f"User '{username}' created with '{permission_level}' permissions"
    except Exception as e:
        session.rollback()
        return False, f"Error creating user: {str(e)}"
    finally:
        session.close()


def delete_user(username: str) -> tuple[bool, str]:
    """Delete a user account. Returns (success, message)."""
    session = get_db_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user:
            return False, f"User '{username}' not found"
        
        session.delete(user)
        session.commit()
        return True, f"User '{username}' deleted"
    except Exception as e:
        session.rollback()
        return False, f"Error deleting user: {str(e)}"
    finally:
        session.close()


def update_user_password(username: str, new_password: str) -> tuple[bool, str]:
    """Update a user's password. Returns (success, message)."""
    session = get_db_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user:
            return False, f"User '{username}' not found"
        
        user.password_hash = hash_password(new_password)
        session.commit()
        return True, f"Password updated for '{username}'"
    except Exception as e:
        session.rollback()
        return False, f"Error updating password: {str(e)}"
    finally:
        session.close()


def update_user_permissions(username: str, permission_level: str) -> tuple[bool, str]:
    """Update a user's permission level. Returns (success, message)."""
    if permission_level not in PERMISSION_LEVELS:
        return False, f"Invalid permission level: {permission_level}"
    
    session = get_db_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user:
            return False, f"User '{username}' not found"
        
        # Remove all existing permissions
        session.query(UserPermission).filter_by(user_id=user.id).delete()
        session.flush()
        
        # Assign new permissions
        for permission in PERMISSION_LEVELS[permission_level]:
            perm = UserPermission(user_id=user.id, permission=permission)
            session.add(perm)
        
        session.commit()
        return True, f"Permissions updated for '{username}' to '{permission_level}'"
    except Exception as e:
        session.rollback()
        return False, f"Error updating permissions: {str(e)}"
    finally:
        session.close()


def set_user_permissions(username: str, permissions: Set[str]) -> tuple[bool, str]:
    """Replace a user's permissions with a validated custom set."""
    invalid_permissions = permissions - VALID_PERMISSIONS
    if invalid_permissions:
        return False, f"Invalid permissions: {', '.join(sorted(invalid_permissions))}"

    session = get_db_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user:
            return False, f"User '{username}' not found"

        session.query(UserPermission).filter_by(user_id=user.id).delete()
        for permission in sorted(permissions):
            session.add(UserPermission(user_id=user.id, permission=permission))

        session.commit()
        return True, f"Permissions updated for '{username}'"
    except Exception as error:
        session.rollback()
        return False, f"Error updating permissions: {error}"
    finally:
        session.close()


def list_users() -> list[dict]:
    """List all users with their permissions."""
    session = get_db_session()
    try:
        users = session.query(User).all()
        result = []
        for user in users:
            permissions = sorted([p.permission for p in user.permissions])
            result.append({
                "username": user.username,
                "active": user.active,
                "permissions": permissions,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login": user.last_login.isoformat() if user.last_login else None,
            })
        return result
    finally:
        session.close()


def record_login(username: str) -> None:
    """Record the last login time for a user."""
    session = get_db_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if user:
            user.last_login = datetime.now(timezone.utc)
            session.commit()
    except Exception:
        pass  # Non-critical, don't fail login on this
    finally:
        session.close()
