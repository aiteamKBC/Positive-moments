from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection


@contextmanager
def database_connection(database_url: str) -> Iterator[Connection]:
    with psycopg.connect(database_url) as connection:
        yield connection


@contextmanager
def readonly_database_connection(database_url: str) -> Iterator[Connection]:
    """Open a transaction that PostgreSQL itself enforces as read-only."""
    with psycopg.connect(database_url) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        yield connection
        connection.rollback()
