"""Explicit suite-tier lineage enumeration; excluded from default test discovery."""
import hashlib
import itertools
import json
import sqlite3
import sys
import unittest

from relay_engine import errors
from relay_engine.tests import test_slips as _fixtures
from relay_engine.tests.test_slips import (
    IMPL, SECOND, PLANNER, draft, LineageStore, lineage_model, lineage_holder_words,
    LINEAGE_FULL_ROUNDS, LINEAGE_SUITE_ROUNDS, LINEAGE_SUITE_UNPRUNED,
    LINEAGE_NAMED_WORDS, lineage_canonical, lineage_word_count,
)


def lineage_space(self, rounds, *, canonical=False, shard=None, words=None):
    """Enumerate words over `rounds`, two rounds deep, each built through `submit` on a
    clone of the store, then an IMPL and `act` at every assignment of p's and q's contents.
    `shard` is (index, count) over the first round's option index.  Returns the record."""
    files = {name: self.file(name + '.md', b'') for name in ('p', 'q')}
    contents = {name: (name.encode() + b' X\n', name.encode() + b' Y\n') for name in files}
    def write(state):
        for name, index in state.items():
            files[name].write_bytes(contents[name][index])
    def clone(conn):
        copy = sqlite3.connect(':memory:')
        conn.backup(copy)
        copy.execute('PRAGMA foreign_keys = ON')
        return copy
    def build(conn, last, state, round_):
        self.conn = clone(conn)
        edit, (kind, refs), response = round_
        state = dict(state)
        if edit:
            state[edit] ^= 1
        write(state)
        paths = [files[name] for name in refs]
        request = last if kind == 'R' else self.requested(kind, last, *paths)
        if response is None:
            return self.conn, request, state
        names = [files[name] for name in response[-1]]
        if response[0] == 'V':
            reply = self.submit(draft(kind='DESIGN-REVIEW', needs='review', parent=request.relay_id,
                                      to=PLANNER.address, refs=self.refs(*names)), IMPL)
        elif response[1] == 'RV':
            # A plain review: `KIND: REVIEW` with a verdict, under a request that is no verification request.
            reply = self.submit(draft(kind='REVIEW', verdict=response[2], parent=request.relay_id,
                                      to=PLANNER.address, cc=self.issuers, refs=self.refs(*names)),
                                IMPL if response[0] == 'H' else SECOND)
        else:
            reply = self.reviewed(request, response[2], *names,
                                  kind={'PR': 'plan', 'DR': 'design'}[response[1]],
                                  sender=IMPL if response[0] == 'H' else SECOND)
        return self.conn, reply, state
    holder = IMPL.seat_id
    record = {'admitted': 0, 'refused': 0, 'violating': 0, 'refusal_codes': {}, 'violations': []}
    assignments = list(itertools.product((0, 1), repeat=2))
    def leaf(conn, last, state, word):
        # Consumes `conn`, a clone made for this word alone.
        self.conn = conn
        try:
            write(state)
            impl = self.issue(last)
            issued = self.slip(impl) is not None
            answers = {}
            for index, (cp, cq) in enumerate(assignments):
                write({'p': cp, 'q': cq})
                answers[(cp, cq)] = self.act(branch='lineage-%d' % index).outcome == 'allow'
            store = LineageStore(self.conn, impl.relay_id)
            model_issues, expected = lineage_model(store, holder)
            digests = {name: tuple(hashlib.sha256(data).hexdigest() for data in contents[name]) for name in files}
            def at(cp, cq):
                return {str(files['p']): digests['p'][cp], str(files['q']): digests['q'][cq]}
            model = {key: model_issues and all(at(*key)[path] == digest
                                               for path, digest in expected.items() if path in at(*key))
                     for key in assignments}
            holder_words = lineage_holder_words(store, holder, issued,
                                                [at(*key) for key in assignments if answers[key]])
            o1 = issued != model_issues or answers != model
            if o1 or holder_words:
                record['violating'] += 1
                record['violations'].append({'word': word, 'issued': issued, 'act': sorted(k for k, v in answers.items() if v),
                                             'o1_issues': model_issues, 'o1_allow': sorted(k for k, v in model.items() if v),
                                             'o1': o1, 'o2': holder_words})
            record['admitted'] += 1
        finally:
            self.conn.close()
    def refused(error, extensions):
        record['refused'] += extensions
        record['refusal_codes'][error.code] = record['refusal_codes'].get(error.code, 0) + extensions
    base = self.conn
    start = self.based()
    state0 = {'p': 0, 'q': 0}
    write(state0)
    prefix = clone(base)
    try:
        if words is not None:
            for word in words:
                conn, last, state = clone(prefix), start, state0
                for round_ in word:
                    previous = conn
                    try:
                        conn, last, state = build(previous, last, state, round_)
                    except errors.EngineError as error:
                        self.conn.close()
                        refused(error, 1)
                        conn = None
                        break
                    finally:
                        previous.close()
                if conn is not None:
                    leaf(conn, last, state, word)
            return record
        if shard is None or shard[0] == 0:
            leaf(clone(prefix), start, state0, ())
        for index, first in enumerate(rounds):
            if shard is not None and index % shard[1] != shard[0]:
                continue
            keep_first = not canonical or lineage_canonical((first,))
            seconds = [second for second in rounds if not canonical or lineage_canonical((first, second))]
            try:
                state1_conn, last1, state1 = build(prefix, start, state0, first)
            except errors.EngineError as error:
                self.conn.close()
                refused(error, int(keep_first) + len(seconds))
                continue
            try:
                if keep_first:
                    leaf(clone(state1_conn), last1, state1, (first,))
                for second in seconds:
                    try:
                        conn2, last2, state2 = build(state1_conn, last1, state1, second)
                    except errors.EngineError as error:
                        self.conn.close()
                        refused(error, 1)
                        continue
                    leaf(conn2, last2, state2, (first, second))
            finally:
                state1_conn.close()
    finally:
        prefix.close()
        self.conn = base
    return record



class TestLineageTier(unittest.TestCase):
    """Enumerate relays over fixed identity rows, immediately verifying claims.

    No missing/NULL reviewer, reviewer reassignment, closed/reopened team,
    stood-down/vacant seat, operator waiver or authenticated identity lifecycle
    is built by this enumeration; its directory is the fixture directory.
    """
    def test_bounded_lineages_keep_the_property(self):
        self.assertEqual((len(LINEAGE_FULL_ROUNDS), len(LINEAGE_SUITE_ROUNDS)), (1431, 290))
        self.assertEqual(lineage_word_count(LINEAGE_FULL_ROUNDS, True), 1147318)
        for name, word in LINEAGE_NAMED_WORDS.items():
            self.assertTrue(all(round_ in LINEAGE_SUITE_ROUNDS for round_ in word), name)
            self.assertTrue(all(round_ in LINEAGE_FULL_ROUNDS for round_ in word) and lineage_canonical(word), name)
        fixture = _fixtures.TestErratum6('test_named_lineages_keep_the_property')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        record = lineage_space(fixture, LINEAGE_SUITE_ROUNDS)
        violating = {tuple(v['word']): v for v in record['violations']}
        named = {name: (None if word not in violating else
                        'O1+O2' if violating[word]['o1'] and violating[word]['o2'] else
                        'O1' if violating[word]['o1'] else 'O2')
                 for name, word in LINEAGE_NAMED_WORDS.items()}
        summary = {key: record[key] for key in ('admitted', 'refused', 'violating', 'refusal_codes')}
        summary['pruned'] = (lineage_word_count(LINEAGE_SUITE_UNPRUNED, False)
                             - lineage_word_count(LINEAGE_SUITE_ROUNDS, False))
        summary['named_flagged_by'] = named
        summary['o2_only'] = sum(1 for v in record['violations'] if v['o2'] and not v['o1'])
        print('LINEAGE-SUMMARY ' + json.dumps(summary, sort_keys=True), file=sys.stderr)
        for violation in record['violations']:
            print('LINEAGE-VIOLATION ' + json.dumps(violation, sort_keys=True, default=repr), file=sys.stderr)
        self.assertEqual(record['admitted'] + record['refused'], lineage_word_count(LINEAGE_SUITE_ROUNDS, False))
        self.assertEqual(record['violations'], [], 'lineages violate the property: %s' % json.dumps(summary))
