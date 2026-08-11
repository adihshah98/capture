import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union


def _sync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    """Write `text` to `path` so a reader or a crash never sees a partial file."""
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _sync_dir(directory)


def atomic_write_json(path: Union[str, Path], value: Any, **dumps_kwargs) -> None:
    atomic_write_text(path, json.dumps(value, **dumps_kwargs))
