"""Snapshot-bound migration checks, cutover, and rollback."""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import uuid

from relay_engine.jcs import jcs_encode
from relay_engine.paths import TempWrite
from relay_engine.render import atomic_create, detect_arity
from relay_engine import reconcile


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def rollback_detail(archive, phase):
    if phase not in {"inert", "restored"}:
        raise ValueError("rollback phase mismatch")
    return jcs_encode({"archive": archive, "phase": phase}).decode("utf-8")


def _parse_detail(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("rollback detail mismatch") from exc
    if (not isinstance(parsed, dict) or set(parsed) != {"archive", "phase"}
            or rollback_detail(parsed["archive"], parsed["phase"]) != value):
        raise ValueError("rollback detail mismatch")
    return parsed


def pending_rollback(ledger):
    """Return the latest incomplete inert rollback, if any."""
    rows = ledger.execute(
        "SELECT seq,digest,detail FROM migration_events "
        "WHERE event='rollback' ORDER BY seq").fetchall()
    latest = None
    restored = []
    for seq, digest, detail in rows:
        parsed = _parse_detail(detail)
        if parsed["phase"] == "inert":
            latest = (seq, digest, parsed["archive"])
        elif parsed["phase"] == "restored":
            restored.append((seq, digest, parsed["archive"]))
    if latest is None:
        return None
    seq, digest, archive = latest
    if any(row_seq > seq and row_digest == digest and row_archive == archive
           for row_seq, row_digest, row_archive in restored):
        return None
    return {"seq": seq, "digest": digest, "archive": archive}


def complete_pending_rollback(ledger, root):
    """Idempotently restore INDEX bytes for an interrupted rollback."""
    pending = pending_rollback(ledger)
    if pending is None:
        return None
    archived = root.open_read(pending["archive"])
    if _digest(archived) != pending["digest"]:
        raise ValueError("rollback archive digest mismatch")
    try:
        live = root.open_read("INDEX.md")
    except FileNotFoundError:
        live = None
    if live is None or _digest(live) != pending["digest"]:
        with TempWrite(root, "INDEX.md") as write:
            write.write(archived)
            write.rename_replace()
    ledger.execute("BEGIN IMMEDIATE")
    try:
        seq = ledger.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM migration_events"
        ).fetchone()[0]
        ledger.execute(
            "INSERT INTO migration_events(seq,event,digest,detail) "
            "VALUES(?,'rollback',?,?)",
            (seq, pending["digest"],
             rollback_detail(pending["archive"], "restored")))
    except BaseException:
        ledger.execute("ROLLBACK")
        raise
    else:
        ledger.execute("COMMIT")
    return pending


def _mkdirs(root, rel):
    current = os.dup(root.dirfd)
    try:
        for part in rel.split("/"):
            try:
                os.mkdir(part, 0o700, dir_fd=current)
                os.fsync(current)
            except FileExistsError:
                pass
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current)
            os.close(current)
            current = following
    finally:
        os.close(current)


def _live_manifest(root, paths, include_index):
    entries = []
    for rel in sorted(paths + (["INDEX.md"] if include_index else [])):
        body = root.open_read(rel)
        entries.append({"path": rel, "sha256": _digest(body),
                        "size": len(body)})
    return entries


def _manifest_digest(entries):
    return _digest(jcs_encode(entries))


def check(ledger, root):
    """Snapshot live legacy bytes and emit an immutable cutover receipt."""
    paths, inventoried = reconcile.admissible_paths(root)
    try:
        index = root.open_read("INDEX.md")
    except FileNotFoundError:
        index = None
    entries = _live_manifest(root, paths, index is not None)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scratch = ".engine/archive/scratch-%s-%s" % (
        stamp, uuid.uuid4().hex[:12])
    _mkdirs(root, scratch + "/snapshot")
    for entry in entries:
        rel = entry["path"]
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent:
            _mkdirs(root, scratch + "/snapshot/" + parent)
        atomic_create(root, scratch + "/snapshot/" + rel,
                      root.open_read(rel))
    manifest = jcs_encode(entries)
    atomic_create(root, scratch + "/snapshot/manifest.json", manifest)
    candidates, malformed = reconcile.prepare_candidates(
        ledger, root, paths)
    indexed = set()
    if index is not None:
        indexed = set(re.findall(
            r"(?:^|\|)\s*([^|\s]+\.md)\s*(?=\||$)",
            index.decode("utf-8", "replace"), re.MULTILINE))
    path_set = [candidate[0] for candidate in candidates]
    census = {
        "ingested": len(candidates),
        "unindexed_ingested_flagged": len(set(path_set) - indexed),
        "index_only_preserved_in_addendum": len(indexed - set(path_set)),
        "duplicates": 0,
        "malformed_inventoried": len(malformed),
        "same_stamp_collisions": len(path_set) - len({
            candidate[1] for candidate in candidates}),
        "inventory": sorted(inventoried), "malformed": sorted(malformed),
    }
    verdict = "green" if not malformed else "red"
    receipt = {
        "v": 1, "scratch": scratch,
        "snapshot_manifest_sha256": _manifest_digest(entries),
        "snapshot_manifest": entries, "verdict": verdict,
        "divergences": [], "census": census,
        "census_sha256": _digest(jcs_encode(census)),
        "scratch_row_count": len(candidates),
        "path_set_sha256": _digest(jcs_encode(path_set)),
    }
    atomic_create(root, scratch + "/receipt.json", jcs_encode(receipt))
    return {"receipt": scratch + "/receipt.json", "verdict": verdict,
            "census": census}


def _read_receipt(root, receipt_path):
    value = json.loads(root.open_read(receipt_path))
    if (not isinstance(value, dict) or value.get("v") != 1 or
            jcs_encode(value) != root.open_read(receipt_path)):
        raise ValueError("migration receipt representation mismatch")
    return value


def cutover(ledger, root, receipt_path, fault=None):
    """Consume one green receipt and atomically activate its imports."""
    receipt = _read_receipt(root, receipt_path)
    if receipt["verdict"] != "green" or receipt.get("divergences"):
        raise ValueError("migration receipt is not green")
    expected = receipt["snapshot_manifest"]
    relay_paths = [entry["path"] for entry in expected
                   if entry["path"] != "INDEX.md"]
    try:
        root.open_read("INDEX.md")
    except FileNotFoundError:
        has_index = False
    else:
        has_index = True
    current = _live_manifest(root, relay_paths, has_index)
    if current != expected or _manifest_digest(current) != receipt[
            "snapshot_manifest_sha256"]:
        raise ValueError("live content manifest changed")
    snapshot = receipt["scratch"] + "/snapshot/"
    candidates, malformed = reconcile.prepare_candidates(
        ledger, root, relay_paths)
    if malformed or len(candidates) != receipt["scratch_row_count"]:
        raise ValueError("snapshot candidate set mismatch")
    if _digest(jcs_encode([row[0] for row in candidates])) != receipt[
            "path_set_sha256"]:
        raise ValueError("snapshot path set mismatch")
    archive_path = None
    if has_index:
        index = root.open_read("INDEX.md")
        archive_path = ".engine/archive/index-%s.archive" % _digest(index)
        try:
            atomic_create(root, archive_path, index)
        except FileExistsError:
            if root.open_read(archive_path) != index:
                raise
    if fault == "cutover-pre":
        raise RuntimeError("cutover-pre")
    ledger.execute("BEGIN IMMEDIATE")
    try:
        if has_index:
            arity = detect_arity(root.open_read("INDEX.md"))
            ledger.execute(
                "INSERT OR REPLACE INTO meta(key,value) "
                "VALUES('index_arity',?)", (str(arity),))
        imported = reconcile.ingest_candidates(
            ledger, candidates, own_transaction=False)
        if len(imported) != receipt["scratch_row_count"]:
            raise ValueError("cutover import count mismatch")
        seq = ledger.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM migration_events"
        ).fetchone()[0]
        ledger.execute(
            "INSERT INTO migration_events(seq,event,digest,detail) "
            "VALUES(?,'cutover',?,?)",
            (seq, receipt["snapshot_manifest_sha256"], receipt_path))
    except BaseException:
        ledger.execute("ROLLBACK")
        raise
    else:
        ledger.execute("COMMIT")
    if fault == "cutover-post":
        raise RuntimeError("cutover-post")
    return {"imported": imported, "archive": archive_path,
            "epoch": "active"}


def rollback(ledger, root, archive_path, fault=None):
    archived = root.open_read(archive_path)
    digest = _digest(archived)
    ledger.execute("BEGIN IMMEDIATE")
    try:
        seq = ledger.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM migration_events"
        ).fetchone()[0]
        ledger.execute(
            "INSERT INTO migration_events(seq,event,digest,detail) "
            "VALUES(?,'rollback',?,?)",
            (seq, digest, rollback_detail(archive_path, "inert")))
    except BaseException:
        ledger.execute("ROLLBACK")
        raise
    else:
        ledger.execute("COMMIT")
    if fault == "rollback-inert-pre-restore":
        raise RuntimeError("rollback-inert-pre-restore")
    complete_pending_rollback(ledger, root)
    return {"epoch": "inert", "archive": archive_path,
            "digest": digest}
