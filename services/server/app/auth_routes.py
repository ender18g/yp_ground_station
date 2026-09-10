"""Authentication and user administration HTTP endpoints."""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from app.auth import (
    User, create_access_token, create_user, delete_user, get_current_user,
    get_db_session, list_users, record_login, require_permission,
    set_user_permissions, update_user_password, update_user_permissions,
    verify_password,
)

AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
router = APIRouter()


@router.post("/api/auth/login")
async def login(payload: dict[str, Any] = Body(default={})) -> JSONResponse:
    """Authenticate a user and return a JWT token."""
    username: str = str(payload.get("username") or "").strip()
    password: str = str(payload.get("password") or "").strip()

    if not username or not password:
        return JSONResponse({"error": "username and password are required"}, status_code=400)

    session = get_db_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user or not verify_password(password, user.password_hash):
            return JSONResponse({"error": "invalid credentials"}, status_code=401)

        if not user.active:
            return JSONResponse({"error": "account is disabled"}, status_code=401)

        # Record login time
        record_login(username)

        # Create the token only in an HttpOnly cookie; it must not be exposed to JavaScript or URLs.
        token = create_access_token(username)
        response = JSONResponse({
            "ok": True,
            "token_type": "bearer",
            "user": {
                "username": user.username,
                "permissions": sorted([p.permission for p in user.permissions])
            }
        })
        response.set_cookie("auth_token", token, httponly=True, secure=AUTH_COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24)
        return response
    finally:
        session.close()


@router.post("/api/auth/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("auth_token", httponly=True, samesite="lax")
    return response


@router.get("/api/auth/me")
async def get_current_user_info(authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Return the current authenticated user's information."""
    if not authorization:
        return JSONResponse({"error": "missing authorization header"}, status_code=401)

    # Extract token from "Bearer <token>"
    token = None
    if authorization.startswith("Bearer "):
        token = authorization[7:]

    user = get_current_user(token)
    if not user:
        return JSONResponse({"error": "invalid or expired token"}, status_code=401)

    return JSONResponse({
        "username": user.username,
        "active": user.active,
        "permissions": sorted([p.permission for p in user.permissions]),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
    })


@router.post("/api/auth/users")
async def create_new_user(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Create a new user account (admin only)."""
    authorization_error = require_permission(authorization, "manage_users")
    if authorization_error:
        return authorization_error

    username: str = str(payload.get("username") or "").strip()
    password: str = str(payload.get("password") or "").strip()
    permission_level: str = str(payload.get("permission_level") or "view_only").strip()

    if not username or not password:
        return JSONResponse({"error": "username and password are required"}, status_code=400)

    success, message = create_user(username, password, permission_level)
    if not success:
        return JSONResponse({"error": message}, status_code=400)

    return JSONResponse({"ok": True, "message": message})


@router.get("/api/auth/users")
async def list_all_users(authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """List all users (admin only)."""
    authorization_error = require_permission(authorization, "manage_users")
    if authorization_error:
        return authorization_error

    users_list = list_users()
    return JSONResponse({"users": users_list})


@router.delete("/api/auth/users/{username}")
async def delete_user_endpoint(username: str, authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Delete a user account (admin only)."""
    authorization_error = require_permission(authorization, "manage_users")
    if authorization_error:
        return authorization_error

    success, message = delete_user(username)
    if not success:
        return JSONResponse({"error": message}, status_code=400)

    return JSONResponse({"ok": True, "message": message})


@router.put("/api/auth/users/{username}/permissions")
async def update_permissions_endpoint(
    username: str,
    payload: dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None)
) -> JSONResponse:
    """Update a user's permission level (admin only)."""
    authorization_error = require_permission(authorization, "manage_users")
    if authorization_error:
        return authorization_error

    permissions = payload.get("permissions")
    if isinstance(permissions, list):
        if not all(isinstance(permission, str) for permission in permissions):
            return JSONResponse({"error": "permissions must be a list of strings"}, status_code=400)
        success, message = set_user_permissions(username, set(permissions))
    else:
        permission_level: str = str(payload.get("permission_level") or "").strip()
        if not permission_level:
            return JSONResponse({"error": "permission_level or permissions is required"}, status_code=400)
        success, message = update_user_permissions(username, permission_level)
    if not success:
        return JSONResponse({"error": message}, status_code=400)

    return JSONResponse({"ok": True, "message": message})


@router.put("/api/auth/users/{username}/password")
async def update_password_endpoint(
    username: str,
    payload: dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None)
) -> JSONResponse:
    """Update a user's password (admin only or self)."""
    if not authorization:
        return JSONResponse({"error": "missing authorization header"}, status_code=401)

    token = authorization[7:] if authorization.startswith("Bearer ") else None
    user = get_current_user(token)
    if not user:
        return JSONResponse({"error": "invalid or expired token"}, status_code=401)

    # Allow user to change their own password, or admin to change any password
    if user.username != username and not user.has_permission("manage_users"):
        return JSONResponse({"error": "insufficient permissions"}, status_code=403)

    new_password: str = str(payload.get("password") or "").strip()
    if not new_password:
        return JSONResponse({"error": "password is required"}, status_code=400)

    success, message = update_user_password(username, new_password)
    if not success:
        return JSONResponse({"error": message}, status_code=400)

    return JSONResponse({"ok": True, "message": message})
