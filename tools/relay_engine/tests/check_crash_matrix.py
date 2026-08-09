#!/usr/bin/env python3
"""Append and verify the exact current-run crash-state matrix."""

from contextlib import contextmanager
import os
from pathlib import Path
import sys


EXPECTED = {
    ("after-relay-insert", "relay_engine.tests.test_ledger."
     "TestAdmissionFaults.test_after_relay_insert"),
    ("after-cycle-events", "relay_engine.tests.test_ledger."
     "TestAdmissionFaults.test_after_cycle_events"),
    ("after-supersession-edges", "relay_engine.tests.test_ledger."
     "TestAdmissionFaults.test_after_supersession_edges"),
    ("pre-commit", "relay_engine.tests.test_ledger."
     "TestAdmissionFaults.test_pre_commit"),
    ("post-commit-pre-response", "relay_engine.tests.test_ledger."
     "TestAdmissionFaults.test_post_commit_pre_response"),
    ("pre-file-render", "relay_engine.tests.test_crash_states."
     "TestCrashStates.test_pre_file_render"),
    ("mid-file-write", "relay_engine.tests.test_crash_states."
     "TestCrashStates.test_mid_file_write"),
    ("pre-index-render", "relay_engine.tests.test_crash_states."
     "TestCrashStates.test_pre_index_render"),
    ("mid-index-write", "relay_engine.tests.test_crash_states."
     "TestCrashStates.test_mid_index_write"),
    ("mid-commit", "relay_engine.tests.test_crash_states."
     "TestCrashStates.test_mid_commit"),
    ("render-fail-loop", "relay_engine.tests.test_crash_states."
     "TestCrashStates.test_render_fail_loop"),
    ("registration-txn", "relay_engine.tests.test_seats."
     "TestSeats.test_registration_txn_crash"),
    ("commission-txn", "relay_engine.tests.test_commission."
     "TestCommission.test_commission_txn_crash"),
    ("cutover-pre", "relay_engine.tests.test_migrate."
     "TestMigrate.test_cutover_pre_crash"),
    ("cutover-post", "relay_engine.tests.test_migrate."
     "TestMigrate.test_cutover_post_crash"),
    ("rollback-inert-pre-restore", "relay_engine.tests.test_migrate."
     "TestMigrate.test_rollback_inert_pre_restore_crash"),
}


def _artifact():
    root = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
    return None if not root else Path(root, "v29-engine-crash-matrix.txt")


def append_result(point, test_id, verdict):
    run = os.environ.get("RELAY_ENGINE_MATRIX_RUN")
    artifact = _artifact()
    if not run or artifact is None:
        return
    line = f"{run} {point} {test_id} {verdict}\n".encode("ascii")
    fd = os.open(artifact, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                 os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("matrix append made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def matrix_case(point, test_id):
    try:
        yield
    except BaseException:
        append_result(point, test_id, "FAIL")
        raise
    else:
        append_result(point, test_id, "PASS")


def check(run):
    artifact = _artifact()
    if artifact is None or not artifact.is_file():
        return False
    rows = []
    for line in artifact.read_text(encoding="ascii").splitlines():
        fields = line.split(" ")
        if len(fields) == 4 and fields[0] == run:
            rows.append((fields[1], fields[2], fields[3]))
    return (len(rows) == len(EXPECTED) and
            {(point, test_id) for point, test_id, verdict in rows} == EXPECTED
            and all(verdict == "PASS" for _, _, verdict in rows))


def main(argv):
    if len(argv) != 2:
        return 2
    return 0 if check(argv[1]) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
