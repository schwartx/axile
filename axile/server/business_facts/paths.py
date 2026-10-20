"""业务观察来源定位；路径不承担业务实体身份。"""


def escape_pointer_token(value: str) -> str:
    """按 RFC 6901 转义单个对象键，不改变原始身份。"""
    return value.replace("~", "~0").replace("/", "~1")


def join_pointer(path: str, token: str | int) -> str:
    """为根或既有 JSON pointer 追加一个键或序号。"""
    split_pointer(path)
    return f"{path}/{escape_pointer_token(str(token))}"


def split_pointer(path: str) -> tuple[str, ...]:
    """拆解 JSON pointer，并拒绝无法解释的转义。"""
    if not path:
        return ()
    if not path.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    tokens = path[1:].split("/")
    for token in tokens:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 == len(token) or token[index + 1] not in "01":
                    raise ValueError("Invalid JSON pointer escape")
                index += 1
            index += 1
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in tokens)
