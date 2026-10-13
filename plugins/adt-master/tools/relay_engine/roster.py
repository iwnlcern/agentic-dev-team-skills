"""Declared names and notices; neither conveys acting authority."""
from pathlib import Path
import time

from relay_engine import errors, seats, strings, version
from relay_engine.envelope import parse_draft
from relay_engine.ledger import (_transaction, insert_engine_relay,
                                  record_judgment)
from relay_engine.paths import Root


def top_seat(ledger) -> str | None:
    if not ledger.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
        return None
    row = ledger.execute("SELECT value FROM meta WHERE key='top_seat'").fetchone()
    return row[0] if row else None


def designate_top_seat(ledger, address: str) -> None:
    if top_seat(ledger) is not None:
        return
    seats._validate(address)
    ledger.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('top_seat',?)", (address,))


def known(ledger) -> frozenset[str]:
    names = {'operator', 'orchestrator'}
    tables = {r[0] for r in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('roster', 'seat_events'):
        if table in tables:
            names.update(row[0].lower() for row in ledger.execute('SELECT address FROM ' + table))
    return frozenset(names)


def declare(ledger, root, entries, tag: str) -> list[dict]:
    top = top_seat(ledger)
    if top is None:
        raise errors.error_for('no-top-seat')
    if not seats.tag_is_current(ledger, root, top, tag):
        raise errors.error_for('E-KEY-MISMATCH')
    prepared = []
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise errors.error_for('E-HEADER', field='ADDRESS')
        address, role = entry
        if not isinstance(address, str) or seats._ADDRESS.fullmatch(address) is None:
            raise errors.error_for('E-HEADER', field='ADDRESS')
        if not isinstance(role, str) or seats._ROLE.fullmatch(role) is None:
            raise errors.error_for('E-HEADER', field='ROLE')
        prepared.append((address.lower(), role.lower()))
    results = []
    with _transaction(ledger):
        seq = ledger.execute('SELECT COALESCE(MAX(seq),0) FROM relays').fetchone()[0]
        for address, role in prepared:
            old = ledger.execute('SELECT role FROM roster WHERE address=?', (address,)).fetchone()
            occupied = ledger.execute('SELECT 1 FROM seat_events WHERE lower(address)=? LIMIT 1', (address,)).fetchone()
            if old or occupied:
                results.append({'address': address, 'role': old[0] if old else role, 'result': 'exists'})
            else:
                ledger.execute('INSERT INTO roster(address,role,declared_by,declared_at_seq) VALUES(?,?,?,?)',
                               (address, role, top, seq))
                results.append({'address': address, 'role': role, 'result': 'added'})
    return results


def notice_effect(ctx, findings) -> None:
    finding = next((f for f in findings if f.id == 'routing.unknown-address'), None)
    top = top_seat(ctx.ledger)
    if finding is None or top is None:
        return
    run = ctx.envelope.run_id
    body = (f'ROLE: Pair Planner\nPHASE: SITREP\nAUTHORITY: report-only\n'
            f'DISPATCH_ID: {run}-notice-{ctx.seq}\nRUN_ID: {run}\n'
            'CEREMONY_TIER: medium\nEVIDENCE_TARGET: E1\nHUMAN_GATE_REQUIRED: no\n'
            f'FROM: {run}-engine-notice.planner\nTO: {top}\n'
            f'IN_REPLY_TO: {ctx.rendered_path}\nSUBJECT: unknown addresses in admitted relay\n'
            'ACTIONS_GIT_REF: none — engine notice\nFINAL_GIT_STATUS_SHORT: none — engine notice\n\n'
            'This is an engine notice, not an instruction from any seat.\n'
            'Unknown addresses: ' + ', '.join(finding.params['addresses']) + '\n').encode()
    name = ctx.ledger.execute("SELECT value FROM meta WHERE key='canonical_root'").fetchone()[0]
    with Root(name) as root:
        notice = insert_engine_relay(ctx.ledger, root, parse_draft(body.decode()), body, clock=time.time)
    record_judgment(ctx.ledger, notice.seq, 'exempt',
                    version.fingerprint(Path(__file__).resolve().parents[1]), ())
    ctx.ledger.execute('INSERT INTO routed_notices(relay_seq,notice_seq) VALUES(?,?)', (ctx.seq, notice.seq))
