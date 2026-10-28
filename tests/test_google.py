"""Sign in with Google: linking a guest, signing in on another device, privacy."""

import pytest
from sqlmodel import Session, select

from app.auth import GoogleTokenError
from app.models import AuthToken, User

from .conftest import auth, make_guest

AVATAR = {"gender": "female", "skin": "#8a5530", "hair": "afro", "hairColor": "#1a120c", "jersey": "#1f4fd1", "shorts": "#17110d", "shoes": "#f4f1ea", "number": 7}


@pytest.fixture
def google(client):
    """Google is configured, and a credential "cred-<sub>" verifies as account <sub>."""
    client.app.state.settings.google_client_id = "test-client.apps.googleusercontent.com"

    def verify(credential: str, client_id: str) -> str:
        assert client_id == "test-client.apps.googleusercontent.com"
        if not credential.startswith("cred-"):
            raise GoogleTokenError("bad signature")
        return credential.removeprefix("cred-")

    client.app.state.verify_google = verify
    yield
    client.app.state.settings.google_client_id = ""


def sign_in(client, sub, **body):
    return client.post("/auth/google", json={"credential": f"cred-{sub}-padding-to-length", **body})


def test_disabled_without_a_client_id(client):
    r = client.post("/auth/google", json={"credential": "x" * 40})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "google_disabled"


def test_bad_credentials_are_rejected(client, google):
    r = client.post("/auth/google", json={"credential": "forged-token-forged-token"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "bad_google_token"


def test_first_sign_in_links_this_devices_guest(client, google, engine):
    guest, guest_id = make_guest(client, "Dayo")
    r = sign_in(client, "acct1", guest_token=guest).json()
    assert r["account"] == "linked"
    assert r["user"]["id"] == guest_id and r["user"]["nickname"] == "Dayo" and r["user"]["google"] is True
    # Both the old guest token and the new one work.
    assert client.get("/auth/me", headers=auth(guest)).json()["google"] is True
    assert client.get("/auth/me", headers=auth(r["token"])).json()["id"] == guest_id
    with Session(engine) as s:
        assert s.exec(select(User)).one().google_sub == "acct1-padding-to-length"


def test_another_device_signs_in_to_the_same_player(client, google):
    guest, guest_id = make_guest(client, "Dayo")
    laptop = sign_in(client, "acct1", guest_token=guest).json()["token"]

    # Phone: has its own throwaway guest, signs in with the same Google account.
    phone_guest, phone_guest_id = make_guest(client, "Temp")
    r = sign_in(client, "acct1", guest_token=phone_guest).json()
    assert r["account"] == "existing" and r["user"]["id"] == guest_id
    assert r["token"] != laptop
    # Neither device is logged out.
    assert client.get("/auth/me", headers=auth(laptop)).json()["id"] == guest_id
    assert client.get("/auth/me", headers=auth(r["token"])).json()["id"] == guest_id


def test_new_player_without_a_guest(client, google):
    r = sign_in(client, "acct2", nickname="Ada", avatar=AVATAR).json()
    assert r["account"] == "new" and r["user"]["nickname"] == "Ada" and r["user"]["avatar"]["number"] == 7
    # Signed-in players can do everything guests can.
    assert client.post("/rooms", json={"mode": "shootout"}, headers=auth(r["token"])).status_code == 201

    nameless = sign_in(client, "acct3").json()
    assert nameless["user"]["nickname"].startswith("Player ")


def test_guest_already_linked_elsewhere_isnt_stolen(client, google):
    guest, guest_id = make_guest(client, "Dayo")
    sign_in(client, "acct1", guest_token=guest)
    # A different Google account on the same device: gets its own player, not Dayo.
    r = sign_in(client, "acct2", guest_token=guest).json()
    assert r["account"] == "new" and r["user"]["id"] != guest_id


def test_delete_removes_device_tokens(client, google, engine):
    r = sign_in(client, "acct1", nickname="Ada").json()
    assert client.delete("/auth/me", headers=auth(r["token"])).status_code == 204
    assert client.get("/auth/me", headers=auth(r["token"])).status_code == 401
    with Session(engine) as s:
        assert s.exec(select(AuthToken)).all() == []
