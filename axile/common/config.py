"""定义从运行环境加载的公共配置模型.

Notes
-----
主服务端配置**只有两个来源**：``config.toml``（由 Web 初始化 / 系统配置向导生成、维护，
是唯一可编辑真源）与代码默认值。刻意**不从进程环境变量读取任何配置值**，避免用户在向导
里改了却被 env var 静默覆盖。读取哪个 TOML 文件可由 ``AXILE_CONFIG_TOML`` 指定（仅为
「指向哪个文件」的启动期指针，本身不承载配置值）。主服务端 **不读取** ``.env``
（仿真服务端仍读取其自身的 ``.env``）。
"""

import os
from pathlib import Path
from typing import Annotated, Any, Literal

import tomlkit
from pydantic import UrlConstraints
from pydantic_core import MultiHostUrl
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# 应用级常量：不再作为可配置项，硬编码于此。
PROJECT_NAME = "AXILE"
API_V1_STR = "/api/v1"

# 配置文件环境变量：仅指定「读取哪个 TOML 文件」的启动期指针，**不承载任何配置值本身**。
# 默认 ``./config.toml``（由初始化 / 系统配置向导生成、维护，用户不应手改）；外部启动器可用它
# 指向另一份独立配置而不污染默认文件。
CONFIG_TOML_ENV = "AXILE_CONFIG_TOML"

# 向导托管的配置文件路径（由初始化 / 系统配置向导生成、维护，用户不应手改）。
CONFIG_TOML_PATH = Path(os.environ.get(CONFIG_TOML_ENV, "./config.toml"))

SqliteDsn = Annotated[
    MultiHostUrl,
    UrlConstraints(
        allowed_schemes=["sqlite+aiosqlite"],
    ),
]


class Settings(BaseSettings):
    """
    定义 Axile 主服务端使用的应用配置.

    Attributes
    ----------
    environment : Literal["local", "staging", "production"]
        当前运行环境标识。
    sqlalchemy_database_uri : SqliteDsn
        SQLite 文件地址，字段名保留旧配置兼容。
    exe_err_feishu_key : str
        执行异常通知使用的飞书机器人密钥。
    app_log_dir : Path
        应用日志目录。
    axile_log_rotation : str
        日志滚动策略。
    algorithm_modules : list[str]
        需要额外加载的算法模块列表（显式的可导入模块名，如 ``pkg.mod``）。
    algorithm_directories : list[str]
        用户算法目录列表；启动时逐个扫描目录内的 ``.py`` 模块并加载其中注册的算法。
        为空时回退到默认目录 ``user_algorithms``。

    Notes
    -----
    实例化时按 ``config.toml > 类默认值`` 的优先级读取配置，见
    :meth:`settings_customise_sources`；**不读取任何进程环境变量作为配置值**。所有字段
    均带默认值，因此在缺失 ``config.toml`` 时也能成功实例化，从而支持「未配置即进入初始化
    向导」的启动流程；是否完成初始化由 :func:`is_configured` 判定（以 ``config.toml`` 是否
        存在为准）。
    """

    model_config = SettingsConfigDict(
        # 主服务端配置改由向导托管的 config.toml 承载，不再读取 .env 或进程环境变量。
        toml_file=str(CONFIG_TOML_PATH),
        env_ignore_empty=True,
        extra="ignore",
    )
    environment: Literal["local", "staging", "production"] = "local"

    time_check_first_run: bool = False

    sqlalchemy_database_uri: SqliteDsn = MultiHostUrl("sqlite+aiosqlite:///./axile.db")
    exe_err_feishu_key: str = ""
    system_execution_notification_mode: Literal["default", "function"] = "default"
    system_execution_notification_code: str = ""
    app_log_dir: Path = Path("./logs")
    axile_log_rotation: str = "1 day"
    algorithm_modules: list[str] = []
    algorithm_directories: list[str] = []

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        定制配置来源与优先级.

        Returns
        -------
        tuple[PydanticBaseSettingsSource, ...]
            返回的元组按优先级从高到低排列：显式初始化参数、``config.toml``。刻意
            **不返回** ``env_settings`` 与 ``dotenv_settings``——主服务端不把任何进程
            环境变量或 ``.env`` 作为配置值来源，使 ``config.toml`` 成为唯一可编辑真源，
            向导写入的字段一定生效、不被 env var 静默覆盖。启动器若需另指配置文件，
            经 ``AXILE_CONFIG_TOML`` 指定路径即可（那是「读哪个文件」的指针，不是配置值）。
        """
        return (init_settings, TomlConfigSettingsSource(settings_cls))


settings = Settings()


def is_configured() -> bool:
    """
    判断主服务端是否已完成初始化配置.

    Returns
    -------
    bool
        当 ``config.toml`` 存在时返回 ``True``；否则返回 ``False``，此时服务端应进入
        初始化向导模式。

    Notes
    -----
    判据是「向导的产物是否已生成」——向导保存的终点动作即写出 :data:`CONFIG_TOML_PATH`
    （见 :func:`write_config_toml`），因此文件存在等价于「至少完成过一次初始化」。
    """
    return CONFIG_TOML_PATH.exists()


def write_config_toml(values: dict[str, Any]) -> None:
    """
    将初始化向导收集的配置写入 ``config.toml``.

    Parameters
    ----------
    values : dict[str, Any]
        以 :class:`Settings` 字段名（大写）为键的配置项；值须为 TOML 可序列化的
        原始类型（字符串或字符串列表）。

    Notes
    -----
    使用 tomlkit 写出，文件顶部保留告警注释以提示该文件由初始化向导托管、
    不应手工编辑。写入的键名与 :class:`Settings` 字段名保持一致（小写），
    以便 pydantic-settings 的 TOML 源在下次启动时正确匹配（键名大小写须与字段一致）。
    """
    document = tomlkit.document()
    document.add(tomlkit.comment(" 本文件由 axile 初始化向导生成并维护，请勿手工编辑。"))
    document.add(tomlkit.comment(" 如需修改配置，请通过 Web 初始化向导重新保存。"))
    for key, value in values.items():
        document[key] = value
    CONFIG_TOML_PATH.write_text(tomlkit.dumps(document), encoding="utf-8")


def update_config_toml_value(key: str, value: Any) -> None:
    """
    更新 ``config.toml`` 中的单个配置值.

    Parameters
    ----------
    key : str
        需要更新的配置键。
    value : Any
        TOML 可序列化的新值。

    Notes
    -----
    该函数用于无需重启即可生效的运行期配置。它保留配置文件中的其他字段与注释，
    避免窄接口通过整份配置覆写启动期设置。
    """
    document = tomlkit.parse(CONFIG_TOML_PATH.read_text(encoding="utf-8"))
    document[key] = value
    CONFIG_TOML_PATH.write_text(tomlkit.dumps(document), encoding="utf-8")


def update_config_toml_values(values: dict[str, Any]) -> None:
    """将一组运行期配置一起写入配置文件。"""
    document = tomlkit.parse(CONFIG_TOML_PATH.read_text(encoding="utf-8"))
    for key, value in values.items():
        document[key] = value
    CONFIG_TOML_PATH.write_text(tomlkit.dumps(document), encoding="utf-8")
