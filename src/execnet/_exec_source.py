"""Normalize ``remote_exec`` sources (string / function / module) to code.

Shared by the sync coordinator ``Gateway`` and the trio-native
``AsyncGateway`` so both accept the same source kinds with identical
restrictions (pure functions taking ``channel`` first, no closures, no
non-builtin globals).
"""

from __future__ import annotations

import inspect
import linecache
import textwrap
import types
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._serialize import SendPayload


def normalize_exec_source(
    source: str | types.FunctionType | Callable[..., object] | types.ModuleType,
    kwargs: dict[str, SendPayload],
) -> tuple[str, str | None, str | None]:
    """Return ``(source, file_name, call_name)`` for a CHANNEL_EXEC payload."""
    call_name = None
    file_name = None
    if isinstance(source, types.ModuleType):
        file_name = inspect.getsourcefile(source)
        linecache.updatecache(file_name)  # type: ignore[arg-type]
        source = inspect.getsource(source)
    elif isinstance(source, types.FunctionType):
        call_name = source.__name__
        file_name = inspect.getsourcefile(source)
        source = _source_of_function(source)
    else:
        source = textwrap.dedent(str(source))

    if not call_name and kwargs:
        raise TypeError("can't pass kwargs to non-function remote_exec")
    return source, file_name, call_name


def _find_non_builtin_globals(source: str, codeobj: types.CodeType) -> list[str]:
    import ast
    import builtins

    vars = dict.fromkeys(codeobj.co_varnames)
    return [
        node.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name)
        and node.id not in vars
        and node.id not in builtins.__dict__
    ]


def _source_of_function(function: types.FunctionType | Callable[..., object]) -> str:
    if function.__name__ == "<lambda>":
        raise ValueError("can't evaluate lambda functions'")
    # XXX: we dont check before remote instantiation
    #      if arguments are used properly
    try:
        sig = inspect.getfullargspec(function)
    except AttributeError:
        args = inspect.getargspec(function)[0]
    else:
        args = sig.args
    if not args or args[0] != "channel":
        raise ValueError("expected first function argument to be `channel`")

    closure = function.__closure__
    codeobj = function.__code__

    if closure is not None:
        raise ValueError("functions with closures can't be passed")

    try:
        source = inspect.getsource(function)
    except OSError as e:
        raise ValueError("can't find source file for %s" % function) from e

    source = textwrap.dedent(source)  # just for inner functions

    used_globals = _find_non_builtin_globals(source, codeobj)
    if used_globals:
        raise ValueError("the use of non-builtin globals isn't supported", used_globals)

    leading_ws = "\n" * (codeobj.co_firstlineno - 1)
    return leading_ws + source
