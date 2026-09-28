"""Alembic 迁移环境。

复用 bdp.db 的引擎解析（SQLite 路径归一、连接参数），
保证迁移与运行时连的是同一个库；元数据来自 bdp.models，
为后续 autogenerate 增量迁移做准备。
"""

from logging.config import fileConfig

from alembic import context

from bdp import models  # noqa: F401  确保全部模型注册到 metadata
from bdp.config import settings
from bdp.db import _resolve_sqlite_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移目标：与运行时同一份元数据
target_metadata = models.Base.metadata

# 连接串不以 alembic.ini 为准：统一走 BDP_DATABASE_URL（与 bdp.db 同源解析）
config.set_main_option("sqlalchemy.url", _resolve_sqlite_url(settings.database_url))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),  # SQLite 不支持多数 ALTER，用批模式
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from bdp.db import engine

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
