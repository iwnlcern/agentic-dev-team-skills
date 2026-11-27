"""Read the window-record tier for Platform's credential resolver."""

from collections.abc import Mapping
import os
from pathlib import Path

from relay_engine.adt_cli import WindowResolver, WindowSession

from . import window, window_record


def window_session(
    environ: Mapping[str, str],
    pid: int,
    *,
    home: Path,
    sessions_dir: Path | None = None,
) -> WindowSession | None:
    identity = window.window_identity(environ, pid, sessions_dir=sessions_dir)
    if identity is None:
        return None
    record = window_record.read(identity.key, home=home)
    if record is None or not record.connected:
        return None
    return WindowSession(record.key_path, record.session_id, record.ready_path)


def adapter_resolver() -> WindowSession | None:
    return window_session(os.environ, os.getpid(), home=Path.home())


def resolve_key_path(
    key: str | None, environ: Mapping[str, str], window: WindowResolver
) -> str | None:
    if key is not None:
        return key
    if "ADT_KEY_FILE" in environ:
        return environ["ADT_KEY_FILE"]
    session = window()
    return None if session is None else session.key_path
