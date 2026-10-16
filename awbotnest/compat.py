"""Metadata compatibility for wrapping third-party callables."""

import functools


def wraps(wrapped):
    # Callable objects may expose None instead of function metadata. CPython
    # accepts None annotations, but compiled functions require a dictionary.
    # Preserve valid metadata and __wrapped__ so signature inspection still works.
    metadata_types = {"__annotations__": dict, "__type_params__": tuple}
    assigned = tuple(
        name for name in functools.WRAPPER_ASSIGNMENTS
        if name not in metadata_types
        or isinstance(getattr(wrapped, name, None), metadata_types[name])
    )
    return functools.wraps(wrapped, assigned=assigned)
