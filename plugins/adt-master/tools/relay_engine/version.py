import hashlib
import os
from pathlib import Path
import stat


KIT_VERSION = "2.9.0"
ROSTER = (
    "relay",
    "relay_engine/README.md",
    "relay_engine/__init__.py",
    "relay_engine/cli.py",
    "relay_engine/client.py",
    "relay_engine/commission.py",
    "relay_engine/cycles.py",
    "relay_engine/daemon.py",
    "relay_engine/envelope.py",
    "relay_engine/errors.py",
    "relay_engine/jcs.py",
    "relay_engine/ledger.py",
    "relay_engine/migrate.py",
    "relay_engine/paths.py",
    "relay_engine/reconcile.py",
    "relay_engine/render.py",
    "relay_engine/rules.py",
    "relay_engine/seats.py",
    "relay_engine/strings.py",
    "relay_engine/supersede.py",
    "relay_engine/version.py",
)


class FingerprintError(RuntimeError):
    pass


def _inventory(tools_dir):
    members = set()
    launcher = tools_dir / "relay"
    try:
        launcher.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise FingerprintError("unreadable-member") from error
    else:
        members.add("relay")

    engine_dir = tools_dir / "relay_engine"
    try:
        engine_status = engine_dir.lstat()
    except FileNotFoundError:
        return members
    except OSError as error:
        raise FingerprintError("unreadable-member") from error
    if not stat.S_ISDIR(engine_status.st_mode):
        members.add("relay_engine")
        return members

    def unreadable(error):
        raise FingerprintError("unreadable-member") from error

    for directory, names, files in os.walk(
            engine_dir, topdown=True, onerror=unreadable,
            followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(tools_dir)
        retained_names = []
        for name in names:
            if name == "__pycache__":
                continue
            if (relative_directory.as_posix() == "relay_engine"
                    and name == "tests"):
                continue
            path = directory_path / name
            try:
                status = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise FingerprintError("unreadable-member") from error
            if stat.S_ISDIR(status.st_mode):
                retained_names.append(name)
            else:
                members.add(path.relative_to(tools_dir).as_posix())
        names[:] = retained_names

        for name in files:
            if name.endswith(".pyc"):
                continue
            path = directory_path / name
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise FingerprintError("unreadable-member") from error
            members.add(path.relative_to(tools_dir).as_posix())
    return members


def _read_regular(path):
    try:
        status = path.lstat()
    except OSError as error:
        raise FingerprintError("unreadable-member") from error
    if not stat.S_ISREG(status.st_mode):
        raise FingerprintError("non-regular-member")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FingerprintError("unreadable-member") from error
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise FingerprintError("non-regular-member")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except FingerprintError:
        raise
    except OSError as error:
        raise FingerprintError("unreadable-member") from error
    finally:
        os.close(descriptor)


def fingerprint(tools_dir: Path) -> str:
    tools_dir = Path(tools_dir)
    actual = _inventory(tools_dir)
    expected = set(ROSTER)
    if actual - expected:
        raise FingerprintError("extra-member")
    if expected - actual:
        raise FingerprintError("missing-member")

    digest = hashlib.sha256()
    digest.update(b"adt-engine-fp/1\0")
    for relative in sorted(ROSTER):
        content = _read_regular(tools_dir / relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()
