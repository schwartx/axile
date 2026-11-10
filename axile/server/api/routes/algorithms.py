"""交易算法注册表查询路由."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from axile.executor.algorithms.core.base import AlgorithmMetadata, list_algorithms_metadata
from axile.server.algorithm_discovery import AlgorithmDirectory, discover_directory

router = APIRouter(prefix="/algorithms", tags=["algorithms"])


@router.get("/discover", response_model=AlgorithmDirectory)
def discover_algorithms(path: str | None = None) -> AlgorithmDirectory:
    """返回当前层目录和算法声明，浏览期间不加载或执行用户脚本。"""
    try:
        return discover_directory(Path.cwd() if path is None else Path(path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="目录不存在") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="路径不是目录") from exc
    except OSError as exc:
        raise HTTPException(status_code=403, detail="无法读取该目录") from exc


# 内置算法所在包前缀；据此区分内置与用户自定义算法。
_BUILTIN_MODULE_PREFIX = "axile.executor.algorithms.defaults"


class AlgorithmPublic(BaseModel):
    """
    单个已注册算法的对外元数据.

    Attributes
    ----------
    name : str
        算法名称（即账户 ``algorithm.method`` 引用的值）。
    description : str
        中文说明；自定义算法暂无说明时为空串。
    params_schema : dict[str, object]
        参数模型生成的 JSON Schema。字段 ``title`` / ``description`` 提供界面文案；
        可选扩展 ``x-enum-labels`` 提供枚举显示名，``x-order`` 控制顺序，
        ``x-unit`` / ``x-display-scale`` / ``x-display-step`` 控制数值显示。
        ``x-control`` 选择 choice/cards/stepper/numberflow/presets/slider 控件；
        ``x-option-descriptions`` 提供各选项说明，``x-presets`` 为原单位快捷值，
        ``x-slider-min`` / ``x-slider-max`` 为显示单位滑块边界（不约束精确输入）。
        这些扩展不改变参数取值、默认值或验证约束；显示缩放后写回须还原原单位。
    channels : list[str] | None
        支持的交易渠道值列表；``None`` 表示全渠道通用。
    slots : list[str] | None
        适用槽位（``trade`` 主交易 / ``empty`` 清仓）；``None`` 表示两槽通用，
        空列表表示两槽都不适用。
    builtin : bool
        是否为内置默认算法；``False`` 表示来自用户算法目录。
    """

    name: str
    label: str
    description: str
    default_params: dict[str, object]
    params_schema: dict[str, object]
    channels: list[str] | None
    slots: list[str] | None
    builtin: bool


def _to_public(meta: AlgorithmMetadata) -> AlgorithmPublic:
    """
    将注册表元数据转换为对外可序列化模型.

    Parameters
    ----------
    meta : AlgorithmMetadata
        注册表内的算法元数据。

    Returns
    -------
    AlgorithmPublic
        面向前端的算法元数据。
    """
    module = getattr(meta.func, "__module__", "") or ""
    return AlgorithmPublic(
        name=meta.name,
        label=meta.label,
        description=meta.description,
        default_params=meta.default_params,
        params_schema=meta.params_schema,
        channels=None if meta.channels is None else sorted(meta.channels),
        slots=None if meta.slots is None else sorted(meta.slots),
        builtin=module.startswith(_BUILTIN_MODULE_PREFIX),
    )


@router.get("", response_model=list[AlgorithmPublic])
def list_registered_algorithms() -> list[AlgorithmPublic]:
    """
    返回全部已注册算法的元数据.

    Returns
    -------
    list[AlgorithmPublic]
        按名称排序的算法元数据列表，含内置与用户自定义算法。

    Notes
    -----
    渠道与槽位过滤交由前端按当前账户上下文完成；本接口只提供结构化事实。
    """
    algorithms = [_to_public(meta) for meta in list_algorithms_metadata()]
    algorithms.sort(key=lambda a: a.name)
    return algorithms


class LoadAlgorithmFile(BaseModel):
    """用户明确选择加载的算法文件。"""

    directory: str
    file: str


@router.post("/load", response_model=list[AlgorithmPublic])
def load_algorithm_file(request: LoadAlgorithmFile) -> list[AlgorithmPublic]:
    """显式加载当前层声明文件，返回其实际注册结果。"""
    import sys

    from axile.executor.algorithms.core.loader import load_algorithm_modules
    from axile.server.app import _directory_modules

    try:
        preview = discover_directory(Path(request.directory))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法读取目录：{exc}") from exc
    declarations = [item for item in preview.discovery.algorithms if item.file == request.file]
    if not declarations:
        raise HTTPException(status_code=400, detail="该文件未发现算法注册入口")
    preview.discovery.algorithms = declarations
    preview.discovery.issues = [issue for issue in preview.discovery.issues if issue.file == request.file]
    preview.discovery.can_use = not preview.discovery.issues
    try:
        modules = _directory_modules(preview)
        list_algorithms_metadata()
        load_algorithm_modules(modules)
        expected = (Path(preview.path) / request.file).resolve()
        result = []
        for meta in list_algorithms_metadata():
            module = sys.modules.get(getattr(meta.func, "__module__", ""))
            origin = getattr(module, "__file__", None)
            if origin and Path(origin).resolve() == expected:
                result.append(_to_public(meta))
        if not result:
            raise ValueError("文件未成功注册算法")
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"加载失败：{exc}") from exc
