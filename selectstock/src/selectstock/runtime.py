"""Detect stale imported Python functions without reloading a running download."""

import hashlib
import inspect
import sys
import types
from pathlib import Path


def _constant(value):
    if isinstance(value, types.CodeType):
        return _signature(value)
    if isinstance(value, frozenset):
        return ("frozenset", tuple(sorted((_constant(v) for v in value), key=repr)))
    if isinstance(value, tuple):
        return tuple(_constant(v) for v in value)
    return repr(value)


def _signature(code):
    return (
        code.co_code,
        tuple(_constant(v) for v in code.co_consts),
        code.co_names,
        code.co_varnames,
        code.co_argcount,
        code.co_kwonlyargcount,
        code.co_freevars,
        code.co_cellvars,
        code.co_flags,
    )


def _codes(code):
    result = {code.co_qualname: _signature(code)}
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            result.update(_codes(value))
    return result


def check_runtime():
    root = Path(__file__).parent
    stale = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("selectstock.") or not getattr(module, "__file__", None):
            continue
        path = Path(module.__file__)
        if path.suffix != ".py" or not path.exists():
            continue
        expected = _codes(compile(path.read_text(), str(path), "exec", dont_inherit=True))
        objects = list(vars(module).values())
        for obj in list(objects):
            if inspect.isclass(obj) and obj.__module__ == name:
                objects.extend(vars(obj).values())
        for obj in objects:
            if isinstance(obj, (staticmethod, classmethod)):
                obj = obj.__func__
            obj = inspect.unwrap(obj) if callable(obj) else obj
            if inspect.isfunction(obj) and obj.__module__ == name:
                if obj.__code__.co_qualname in expected and expected[obj.__code__.co_qualname] != _signature(
                    obj.__code__
                ):
                    stale.append(name)
                    break
    if stale:
        raise RuntimeError(
            "项目代码已更新，请点击 Restart 重启内核，再从第一格运行。旧模块：" + ", ".join(stale)
        )
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.read_bytes())
    import selectstock

    from . import __version__

    baseline = getattr(selectstock, "__source_digest__", None)
    if baseline is not None and baseline != digest.hexdigest():
        raise RuntimeError("项目代码已更新，请点击 Restart 重启内核，再从第一格运行。")

    result = f"selectstock {__version__} | 代码版本 {digest.hexdigest()[:12]} | {sys.executable}"
    print(result)
    return result
