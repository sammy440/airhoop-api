"""
Sessions: an opaque bearer token per device.

Guests get one token at sign-up (users.token_hash). Signing in with Google
links that guest to a Google account; every later Google sign-in (e.g. on a
new device) issues another token (auth_tokens), so devices don't log each
other out. Only token hashes are stored.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from .db import get_session
from .models import AuthToken, User


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_guest(session: Session, nickname: str, avatar: dict | None = None) -> tuple[User, str]:
    token = secrets.token_urlsafe(32)
    user = User(nickname=nickname, token_hash=hash_token(token), avatar=avatar)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user, token


def issue_token(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(AuthToken(user_id=user.id, token_hash=hash_token(token)))
    return token


def user_from_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    h = hash_token(token)
    user = session.exec(select(User).where(User.token_hash == h)).first()
    if user is None:
        user = session.exec(select(User).join(AuthToken, AuthToken.user_id == User.id).where(AuthToken.token_hash == h)).first()
    return user


class GoogleTokenError(Exception):
    pass


def verify_google_credential(credential: str, client_id: str) -> str:
    """Checks a Google Identity Services ID token (signature, audience, expiry) and returns the account ID."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except ValueError as e:
        raise GoogleTokenError(str(e)) from e
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com") or not claims.get("sub"):
        raise GoogleTokenError("Not a Google account token")
    return str(claims["sub"])


def current_user(
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    user = user_from_token(session, token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid guest token", headers={"WWW-Authenticate": "Bearer"})
    return user


CurrentUser = Annotated[User, Depends(current_user)]
SessionDep = Annotated[Session, Depends(get_session)]
