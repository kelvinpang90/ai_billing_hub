"""Celery entry point: `celery -A app.worker worker` / `celery -A app.worker beat`.

与 API 进程不同，**worker 没有 broker 就没有存在意义**，所以这里配不上就直接
抛异常、进程起不来。API 那边相反：数据库没配也要能起，好让存活探针应答
（见 `app/main.py`）。两种处置不同是有意的。
"""

from __future__ import annotations

from app.core.celery_app import create_celery_app
from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
# worker 的日志要和 API 同一套格式与脱敏规则，否则集中日志里两半对不上。
configure_logging(settings.log_level)

celery_app = create_celery_app(settings)
