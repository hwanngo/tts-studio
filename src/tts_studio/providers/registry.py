from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Row
from uuid import uuid4

from tts_studio.providers.domain import ProviderProfile
from tts_studio.providers.service import validate_provider_input
from tts_studio.storage.db import Database


class ProviderNotFoundError(LookupError):
    pass


class ProviderInUseError(RuntimeError):
    pass


class ProviderRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        *,
        kind: str,
        label: str,
        base_url: str,
        model: str,
        api_key_env: str,
        provider_id: str | None = None,
    ) -> ProviderProfile:
        validate_provider_input(
            kind=kind, label=label, base_url=base_url, model=model, api_key_env=api_key_env
        )
        identifier = provider_id or str(uuid4())
        now = _utc_now()
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT INTO provider_profiles
                   (id, kind, label, base_url, model, api_key_env, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    kind,
                    label.strip(),
                    base_url.rstrip("/"),
                    model.strip(),
                    api_key_env,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE id = ?", (identifier,)
            ).fetchone()
        return _profile(_required(row))

    def get(self, provider_id: str) -> ProviderProfile:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE id = ?", (provider_id,)
            ).fetchone()
        return _profile(_required(row))

    def list(self) -> tuple[ProviderProfile, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_profiles ORDER BY created_at, id"
            ).fetchall()
        return tuple(_profile(row) for row in rows)

    def update(self, provider_id: str, **values: str) -> ProviderProfile:
        current = self.get(provider_id)
        merged = {
            "kind": current.kind,
            "label": current.label,
            "base_url": current.base_url,
            "model": current.model,
            "api_key_env": current.api_key_env,
            **values,
        }
        validate_provider_input(**merged)
        now = _utc_now()
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE provider_profiles
                   SET kind = ?, label = ?, base_url = ?, model = ?, api_key_env = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    merged["kind"],
                    merged["label"].strip(),
                    merged["base_url"].rstrip("/"),
                    merged["model"].strip(),
                    merged["api_key_env"],
                    now,
                    provider_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE id = ?", (provider_id,)
            ).fetchone()
        return _profile(_required(row))

    def delete(self, provider_id: str) -> None:
        with self._database.transaction() as connection:
            _required(
                connection.execute(
                    "SELECT id FROM provider_profiles WHERE id = ?", (provider_id,)
                ).fetchone()
            )
            active = connection.execute(
                "SELECT COUNT(*) FROM generation_jobs WHERE provider_id = ? AND state NOT IN ('completed', 'cancelled', 'failed')",
                (provider_id,),
            ).fetchone()[0]
            if active:
                raise ProviderInUseError(provider_id)
            connection.execute("DELETE FROM provider_profiles WHERE id = ?", (provider_id,))


def _profile(row: Row) -> ProviderProfile:
    return ProviderProfile(**dict(row))


def _required(row: Row | None) -> Row:
    if row is None:
        raise ProviderNotFoundError("provider profile was not found")
    return row


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
