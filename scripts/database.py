from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool


def sqlalchemy_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url


def create_database_engine(database_url: str) -> Engine:
    normalized_url = sqlalchemy_database_url(database_url)
    options: dict = {"pool_pre_ping": True}
    if normalized_url.startswith("postgresql+psycopg://"):
        # Supabase transaction/session poolers can reuse server connections;
        # disable psycopg's prepared-statement cache to avoid name collisions.
        options["connect_args"] = {"prepare_threshold": None}
        parsed = urlsplit(normalized_url)
        if "pooler.supabase.com" in (parsed.hostname or "") or parsed.port == 6543:
            # PgBouncer already owns connection pooling. Avoid retaining client
            # connections whose server session can change between transactions.
            options["poolclass"] = NullPool

    engine = create_engine(normalized_url, **options)
    if normalized_url.startswith("postgresql+psycopg://"):
        def disable_prepared_statements(dbapi_connection, _connection_record) -> None:
            # Enforce the setting on every physical connection, including
            # connections used by SQLAlchemy schema inspection/reflection.
            dbapi_connection.prepare_threshold = None
            if hasattr(dbapi_connection, "prepared_max"):
                dbapi_connection.prepared_max = 0

        event.listen(engine, "connect", disable_prepared_statements)

    return engine
