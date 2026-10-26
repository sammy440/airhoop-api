"""Guest sessions (README Week 6): a nickname plus an opaque bearer token."""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from .db import get_session
from .models import User


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_guest(session: Session, nickname: str, avatar: dict | None = None) -> tuple[User, str]:
    token = secrets.token_urlsafe(32)
    user = User(nickname=nickname, token_hash=hash_token(token), avatar=avatar)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user, token


def user_from_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    return session.exec(select(User).where(User.token_hash == hash_token(token))).first()


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
