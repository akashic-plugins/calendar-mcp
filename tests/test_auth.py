from __future__ import annotations

import json

from src import auth


def test_refreshed_credentials_are_persisted(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")

    class Credentials:
        valid = False
        expired = True
        refresh_token = "refresh"

        def refresh(self, request) -> None:
            self.valid = True

        def to_json(self) -> str:
            return json.dumps({"token": "new"})

    credentials = Credentials()
    monkeypatch.setattr(
        auth.Credentials,
        "from_authorized_user_file",
        lambda path, scopes: credentials,
    )
    monkeypatch.setattr(auth, "TOKEN_FILE", str(token_path))
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "client")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_SECRET", "secret")

    result = auth.get_credentials()

    assert result is credentials
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"token": "new"}


def test_missing_credentials_do_not_start_oauth(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "TOKEN_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "client")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_config",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected OAuth")),
    )

    assert auth.get_credentials() is None
