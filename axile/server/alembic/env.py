"""Axile 数据库使用的 Alembic 环境配置."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from axile.common.config import settings
from axile.server.core.db import sqlite_url_at_startup
from axile.server.db.models import SQLModel

# 这是 Alembic 的配置对象，可提供
# 当前所用 .ini 文件中的配置值。
config = context.config

# 解析配置文件中的 Python 日志配置。
# 这一行会完成 logger 的基础初始化。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 在这里添加模型的 MetaData 对象
# 以支持自动生成迁移
# 例如：from myapp import mymodel
# 例如：target_metadata = mymodel.Base.metadata
target_metadata = SQLModel.metadata

# 其他由 env.py 按需使用的配置项，
# 也可以通过如下方式获取：
# my_important_option = config.get_main_option("自定义配置项")
# ……等等。


def get_url() -> str:
    """返回已配置的 SQLAlchemy 数据库 URL."""
    return str(sqlite_url_at_startup(str(settings.sqlalchemy_database_uri)))


def run_migrations_offline() -> None:
    """以离线模式运行迁移.

    该模式只使用数据库 URL 配置上下文，而不会创建 Engine，
    因此无需实际可用的 DBAPI 连接。

    在该模式下，对 ``context.execute()`` 的调用会直接写入迁移脚本输出。
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """使用已经打开的同步连接执行迁移."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """以在线模式运行迁移.

    该模式会创建 Engine，并将实际数据库连接绑定到 Alembic 上下文。
    """
    injected = config.attributes.get("connection")
    if injected is not None:
        do_run_migrations(injected)
        return

    connectable = create_async_engine(get_url(), echo=True, future=True)

    async with connectable.connect() as connection:
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            await connection.exec_driver_sql("PRAGMA synchronous=FULL")
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    # 显式关闭引擎，避免 aiosqlite 连接在 __del__ 中报错
    await connectable.dispose()


if config.attributes.get("connection") is not None:
    do_run_migrations(config.attributes["connection"])
elif context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
