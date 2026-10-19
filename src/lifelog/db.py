"""Kết nối Postgres. Mỗi `with connect()` là một giao dịch: xong thì commit, lỗi thì rollback."""
from __future__ import annotations

from contextlib import contextmanager
from importlib import resources
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import get_settings


@contextmanager
def connect(*, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row, autocommit=autocommit)
    try:
        if autocommit:
            yield conn
        else:
            with conn.transaction():
                yield conn
    finally:
        conn.close()


def init_db() -> None:
    sql = resources.files("lifelog").joinpath("schema.sql").read_text(encoding="utf-8")
    with connect() as conn:
        conn.execute(sql)
