from __future__ import annotations

from sqlalchemy import Engine, create_engine


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
    return create_engine(normalized_url, **options)
