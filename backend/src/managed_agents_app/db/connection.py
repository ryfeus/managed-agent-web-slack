from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import aurora_dsql_psycopg as dsql
import psycopg
from psycopg import Connection
from psycopg.errors import SerializationFailure
from psycopg.rows import dict_row

from managed_agents_app.config import AppConfig


def connect(config: AppConfig, role: str | None = None) -> Connection[dict[str, object]]:
    if config.database_mode == "postgres":
        if not config.database_url:
            raise RuntimeError("Missing PostgreSQL URL")
        return psycopg.connect(config.database_url, row_factory=dict_row, connect_timeout=15)
    if not config.dsql_endpoint:
        raise RuntimeError("Missing DSQL endpoint")
    return cast(
        Connection[dict[str, object]],
        dsql.connect(
            host=config.dsql_endpoint,
            region=config.aws_region,
            user=role or config.dsql_role,
            dbname=config.dsql_database,
            connect_timeout=15,
            sslmode="require",
            row_factory=cast(Any, dict_row),
        ),
    )


def with_dsql_retry[T](fn: Callable[[], T], attempts: int = 5, base_delay_seconds: float = 0.020) -> T:
    import random
    import time

    for attempt in range(attempts):
        try:
            return fn()
        except SerializationFailure:
            if attempt == attempts - 1:
                raise
            maximum = base_delay_seconds * (2**attempt)
            time.sleep(maximum / 2 + random.random() * maximum)
    raise AssertionError("unreachable")
