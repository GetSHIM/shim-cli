"""A password in any `scheme://user:password@host` is a credential. The corpus
grades each scheme; this pins which entity owns a database URI's userinfo."""

from __future__ import annotations

from shim_cli.guard import BUILT_IN_TYPES, evaluate

_WITHOUT_DB_URI = tuple(entity for entity in BUILT_IN_TYPES if entity != "DB_URI")


def test_a_database_uri_stays_db_uri_s_when_db_uri_is_turned_off() -> None:
    text = (
        "postgresql+asyncpg://app:s3cr3tPass@db.internal/app "
        "amqp://app:s3cr3tPass@rabbit.internal//"
    )

    decision = evaluate(text, _WITHOUT_DB_URI)

    assert decision.redacted_text == (
        "postgresql+asyncpg://app:s3cr3tPass@db.internal/app "
        "amqp://<SECRET_1>@rabbit.internal//"
    )
