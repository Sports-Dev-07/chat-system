"""Authentication: Google Sign-In (ID token) + dev login for local testing. JWT sessions."""
import os
import time

import httpx
import jwt
from fastapi import Header, HTTPException

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production")
JWT_TTL = 60 * 60 * 24 * 7  # 7 days
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
DEV_MODE = os.environ.get("DEV_MODE", "true").lower() == "true"


# main.py registers this to check the user's current token_version in the DB
version_checker = None


def make_token(user_id: str, token_version: int = 0) -> str:
    return jwt.encode({"sub": user_id, "tv": token_version, "exp": int(time.time()) + JWT_TTL},
                      JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if version_checker and not version_checker(payload["sub"], payload.get("tv", 0)):
        raise HTTPException(status_code=401, detail="Session revoked — please sign in again")
    return payload["sub"]


def current_user_id(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return decode_token(authorization[7:])


async def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google ID token via Google's tokeninfo endpoint.
    Returns {email, name, picture}."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token}
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Google token verification failed")
    data = resp.json()
    if GOOGLE_CLIENT_ID and data.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token audience mismatch")
    if data.get("email_verified") not in ("true", True):
        raise HTTPException(status_code=401, detail="Google email not verified")
    return {
        "email": data["email"],
        "name": data.get("name", data["email"].split("@")[0]),
        "picture": data.get("picture", ""),
    }
