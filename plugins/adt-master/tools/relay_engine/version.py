import hashlib
import os
from pathlib import Path
import stat


KIT_VERSION = "2.9.7"
ROSTER = (
    "adt",
    "adt-operator-check",
    "relay",
    "relay_engine/README.md",
    "relay_engine/__init__.py",
    "relay_engine/addresses.py",
    "relay_engine/adt_cli.py",
    "relay_engine/api_bridge.py",
    "relay_engine/api_client.py",
    "relay_engine/api_compose.py",
    "relay_engine/api_definitions.py",
    "relay_engine/api_keys.py",
    "relay_engine/api_operator_bridge.py",
    "relay_engine/api_platform.py",
    "relay_engine/api_principals.py",
    "relay_engine/api_protocol.py",
    "relay_engine/api_registry.py",
    "relay_engine/api_relay_reads.py",
    "relay_engine/api_relay_reads_service.py",
    "relay_engine/api_schema.py",
    "relay_engine/api_settings.py",
    "relay_engine/api_settings_service.py",
    "relay_engine/api_service.py",
    "relay_engine/api_sessions.py",
    "relay_engine/api_submit.py",
    "relay_engine/api_wire.py",
    "relay_engine/api_writer.py",
    "relay_engine/approval_seal.py",
    "relay_engine/authority.py",
    "relay_engine/authority_bench.py",
    "relay_engine/catalogue.py",
    "relay_engine/checkout_store.py",
    "relay_engine/claims.py",
    "relay_engine/cli.py",
    "relay_engine/client.py",
    "relay_engine/commission.py",
    "relay_engine/console_page/index.html",
    "relay_engine/console_page/page.css",
    "relay_engine/console_page/page.js",
    "relay_engine/cycles.py",
    "relay_engine/daemon.py",
    "relay_engine/ed25519_verify.py",
    "relay_engine/envelope.py",
    "relay_engine/errors.py",
    "relay_engine/fileacl.py",
    "relay_engine/generated/api/api_3_0.rs",
    "relay_engine/generated/api/api_3_0.ts",
    "relay_engine/generated/api/manifest.json",
    "relay_engine/generated/api/schema-3.0.json",
    "relay_engine/guardrail.py",
    "relay_engine/header.py",
    "relay_engine/held_relays.py",
    "relay_engine/identity_import.py",
    "relay_engine/imports.py",
    "relay_engine/jcs.py",
    "relay_engine/judging.py",
    "relay_engine/ledger.py",
    "relay_engine/lifecycle_lock.py",
    "relay_engine/lifecycle_outbox.py",
    "relay_engine/lifecycle_paths.py",
    "relay_engine/lifecycle_state.py",
    "relay_engine/migrate.py",
    "relay_engine/operator_check_install.py",
    "relay_engine/operator_check_words.py",
    "relay_engine/operator_queue/__init__.py",
    "relay_engine/operator_queue/component.py",
    "relay_engine/operator_queue/ingest.py",
    "relay_engine/operator_queue/kinds.py",
    "relay_engine/operator_queue/liveness.py",
    "relay_engine/operator_queue/notice_text.py",
    "relay_engine/operator_queue/notifier.py",
    "relay_engine/operator_queue/operations.py",
    "relay_engine/operator_queue/records.py",
    "relay_engine/operator_queue/saga.py",
    "relay_engine/operator_terminal.py",
    "relay_engine/owner_start.py",
    "relay_engine/paths.py",
    "relay_engine/queries.py",
    "relay_engine/reconcile.py",
    "relay_engine/refs.py",
    "relay_engine/render.py",
    "relay_engine/restore_cancel.py",
    "relay_engine/roster.py",
    "relay_engine/rules.py",
    "relay_engine/schema.py",
    "relay_engine/seal_files.py",
    "relay_engine/seal_renderer.py",
    "relay_engine/seal_slots.py",
    "relay_engine/seal_types.py",
    "relay_engine/seal_worker.py",
    "relay_engine/seats.py",
    "relay_engine/slips.py",
    "relay_engine/store_backup.py",
    "relay_engine/store_create.py",
    "relay_engine/store_evidence.py",
    "relay_engine/store_import.py",
    "relay_engine/store_migrate.py",
    "relay_engine/store_layout.py",
    "relay_engine/store_steps.py",
    "relay_engine/strings.py",
    "relay_engine/submit.py",
    "relay_engine/supersede.py",
    "relay_engine/version.py",
    "relay_engine/xproject.py",
)


class FingerprintError(RuntimeError):
    pass


def _inventory(tools_dir):
    members = set()
    for name in ("relay", "adt", "adt-operator-check"):
        launcher = tools_dir / name
        try:
            launcher.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise FingerprintError("unreadable-member") from error
        else:
            members.add(name)

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
            path = directory_path / name
            try:
                status = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise FingerprintError("unreadable-member") from error
            if not stat.S_ISDIR(status.st_mode):
                raise FingerprintError("non-regular-member")
            if name == "__pycache__":
                continue
            if (relative_directory.as_posix() == "relay_engine"
                    and name == "tests"):
                continue
            retained_names.append(name)
        names[:] = retained_names

        for name in files:
            path = directory_path / name
            try:
                status = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise FingerprintError("unreadable-member") from error
            if not stat.S_ISREG(status.st_mode):
                raise FingerprintError("non-regular-member")
            if name.endswith(".pyc"):
                continue
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
