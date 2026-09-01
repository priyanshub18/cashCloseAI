"""Run with ``python -m apps.worker.main`` after configuring REDIS_URL."""

from __future__ import annotations

import os

from redis import Redis
from rq import Queue, Worker


def main() -> None:
    connection = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    queue = Queue("cashclose", connection=connection, default_timeout=900)
    Worker([queue], connection=connection).work(with_scheduler=False)


if __name__ == "__main__":
    main()

