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


def create_database_engine(
    database_url: str,
    *,
    connect_timeout_seconds: int | None = None,
) -> Engine:
    normalized_url = sqlalchemy_database_url(database_url)
    options: dict = {"pool_pre_ping": True}
    if normalized_url.startswith("postgresql+psycopg://"):
        # Supabase transaction/session poolers can reuse server connections;
        # disable psycopg's prepared-statement cache to avoid name collisions.
        options["connect_args"] = {"prepare_threshold": None}
        if connect_timeout_seconds is not None:
            options["connect_args"]["connect_timeout"] = connect_timeout_seconds
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


def configure_database_timeouts(
    engine: Engine,
    *,
    statement_timeout_seconds: int,
    lock_timeout_seconds: int,
) -> None:
    """Apply transaction-scoped PostgreSQL timeouts to every engine transaction."""
    if engine.dialect.name != "postgresql":
        return

    statement_timeout_ms = statement_timeout_seconds * 1000
    lock_timeout_ms = lock_timeout_seconds * 1000

    def set_transaction_timeouts(connection) -> None:
        connection.exec_driver_sql(
            f"SET LOCAL statement_timeout = {statement_timeout_ms}"
        )
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = {lock_timeout_ms}")

    event.listen(engine, "begin", set_transaction_timeouts)
