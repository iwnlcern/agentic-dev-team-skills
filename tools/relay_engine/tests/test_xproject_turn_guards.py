"""Exercise the Engine guard sites through Platform's live writer turn."""

from contextlib import closing
from dataclasses import replace
import sqlite3
from unittest import mock

from relay_engine import api_writer, errors, queries, schema, store_layout, submit
from relay_engine.tests import test_api_writer as writer_fixture
from relay_engine.tests import test_submit as submit_fixture


P, Q = writer_fixture.P, writer_fixture.Q
PLANNER = replace(submit_fixture.PLANNER, project_id=P)
IMPLEMENTER = replace(submit_fixture.IMPL, project_id=P)


class Directory(submit_fixture.FakeDirectory):
    def resolve(self, address, sender):
        return replace(super().resolve(address, sender), project_id=sender.project_id)


class TurnGuardTests(writer_fixture.ProjectWriterFixture):
    def seeded_writer(self, *, receiver=False, trace=None):
        """Keep a single project per fixture ledger and seed the Engine rows in a writer job."""
        writer = self.writer_pair() if receiver else self.writer()
        self.enterContext(mock.patch.dict(schema.TABLE_OWNERS, {"fixture_owned": "authority"}))

        def seed(conn):
            conn.execute(
                "INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                " VALUES (?, ?, 'v3-engine', 'pair', 1, 'operator')",
                (PLANNER.team_instance_id, P),
            ).close()
            for seat in (PLANNER, IMPLEMENTER):
                conn.execute(
                    "INSERT INTO seats (id, team_instance_id, role, state) VALUES (?, ?, ?, 'occupied')",
                    (seat.seat_id, seat.team_instance_id, seat.role),
                ).close()
                conn.execute(
                    "INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                    " VALUES (?, ?, ?, 'v', 1)",
                    ("o-" + seat.seat_id, seat.seat_id, "k-" + seat.seat_id),
                ).close()
            conn.execute("CREATE TABLE fixture_owned (label TEXT PRIMARY KEY)").close()
            conn.execute("CREATE TABLE fixture_other (label TEXT PRIMARY KEY)").close()
            if trace is not None:
                conn.set_trace_callback(trace.append)
            return api_writer.JobOutcome(True, None)

        self.assert_committed(writer.submit(P, self.job(seed)))
        return writer

    def ctx(self, **kwargs):
        return submit.SubmitContext(
            PLANNER, None, lambda: 1_700_000_000_000, Directory(), "turn-test", **kwargs,
        )

    def assert_committed(self, outcome):
        self.assertIsNone(outcome.fault, "writer fault")
        self.assertTrue(outcome.commit)

    def committed_turn(self, writer, probe, *, after_open=True):
        def run(turn):
            self.assertIsInstance(turn.sender, api_writer.TurnConnection)
            if after_open:
                turn.open_sender_writes()
            return api_writer.JobOutcome(True, probe(turn))

        outcome = writer.submit_transfer(api_writer.TransferJob(P, run))
        self.assert_committed(outcome)
        return outcome.value

    @staticmethod
    def rows(conn, sql, parameters=()):
        with closing(conn.execute(sql, parameters)) as cursor:
            return cursor.fetchall()

    def durable(self, sql, parameters=()):
        # Fixture witness tables are intentionally outside the managed schema.
        path = store_layout.ledger_path(self.root, P)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            return self.rows(conn, sql, parameters)

    @staticmethod
    def insert_other(conn, label):
        conn.execute("INSERT INTO fixture_other VALUES (?)", (label,)).close()

    def assert_insert_denied(self, conn, label):
        with self.assertRaises(sqlite3.Error) as caught:
            self.insert_other(conn, label)
        self.assertEqual(caught.exception.sqlite_errorname, "SQLITE_AUTH", label)

    def assert_writer_policy_restored(self, conn, label):
        try:
            self.insert_other(conn, label)
        except sqlite3.Error as error:
            self.fail(f"writer-only insert {label}: {error.sqlite_errorname}")

    def test_turn_sender_check_read_only(self):
        """Deliberate post-open diagnostic, not the production snapshot replay order."""
        writer = self.seeded_writer()

        def check_read(conn):
            self.assertEqual(self.rows(conn, "SELECT id FROM projects"), [(P,)])

        for label, callback in (("read", check_read),
                                ("insert", lambda c: self.insert_other(c, "blocked check"))):
            with self.subTest(check=label):
                def probe(turn):
                    ctx = self.ctx(sender_check=callback)
                    if label == "read":
                        self.assertIsNone(submit._run_sender_check(turn.sender, ctx))
                    else:
                        with self.assertRaises(sqlite3.Error) as caught:
                            submit._run_sender_check(turn.sender, ctx)
                        self.assertEqual(caught.exception.sqlite_errorname, "SQLITE_AUTH")
                    self.assert_writer_policy_restored(turn.sender, label)

                self.committed_turn(writer, probe)
        self.assertEqual(self.durable("SELECT label FROM fixture_other ORDER BY label"),
                         [("insert",), ("read",)])

    def test_turn_hook_layer_deny_lets_the_turn_commit(self):
        """Production order, hook after sender writes open."""
        writer = self.seeded_writer()
        ctx = self.ctx(after_admit_writes=frozenset({"fixture_owned"}))

        def probe(turn):
            with submit._hook_frame(turn.sender, ctx, "fixture.hook"):
                turn.sender.execute("INSERT INTO fixture_owned VALUES ('hook allowed')").close()
                self.assert_insert_denied(turn.sender, "hook blocked")

        self.committed_turn(writer, probe)
        self.assertEqual(self.durable("SELECT label FROM fixture_owned"), [("hook allowed",)])
        self.assertEqual(self.durable("SELECT label FROM fixture_other"), [])

    def test_turn_system_filing_lift_keeps_hook_progress(self):
        """Production order, nested system filing inside the post-open hook."""
        trace = []
        writer = self.seeded_writer(trace=trace)
        ctx = self.ctx()
        real_guard = api_writer.TurnConnection.guard
        ticks = 0

        def counted_guard(proxy, authorizer, progress=None):
            def tick():
                nonlocal ticks
                ticks += 1
                return progress()

            return real_guard(proxy, authorizer, progress=tick if progress is not None else None)

        class InjectedFilingFailure(Exception):
            pass

        def try_filing(turn, label, *, fail):
            start = len(trace)
            with submit._hook_frame(turn.sender, ctx, "fixture.hook"):
                before = ticks
                data = submit_fixture.draft(subject=label, bundle=label)
                if fail:
                    with self.assertRaises(InjectedFilingFailure):
                        submit.file_system_relay(turn.sender, ctx, data)
                    admitted = None
                else:
                    admitted = submit.file_system_relay(turn.sender, ctx, data)
                delta = ticks - before
                self.assert_insert_denied(turn.sender, "after lift " + label)
            self.assertGreater(delta, 0, f"progress ticks inside {label} filing")
            return admitted, [s for s in trace[start:] if s.endswith(" engine_system_filing")]

        with mock.patch.object(api_writer.TurnConnection, "guard", counted_guard):
            admitted, statements = self.committed_turn(
                writer, lambda turn: try_filing(turn, "system-success", fail=False),
            )
            self.assertEqual(statements, ["SAVEPOINT engine_system_filing",
                                          "RELEASE engine_system_filing"])
            self.assertEqual(self.durable("SELECT id, sender_special, subject FROM relays"),
                             [(admitted.relay_id, "system", "system-success")])
            self.assertEqual(self.durable("SELECT relay_id FROM bodies"), [(admitted.relay_id,)])
            self.assertEqual(self.durable("SELECT relay_id, seat_id FROM recipients"),
                             [(admitted.relay_id, PLANNER.seat_id)])

            tables = (*submit_fixture.TABLES, "snapshots", "foreign_edges")
            before = {table: self.durable(f"SELECT * FROM {table}") for table in tables}
            injected = []

            def fail_at_write_transitions(conn, _sender, _transitions, relay_id, _created):
                self.assertIsInstance(conn, api_writer.TurnConnection)
                self.assertEqual(self.rows(conn, "SELECT subject FROM relays WHERE id=?", (relay_id,)),
                                 [("system-rollback",)])
                self.assertEqual(self.rows(conn, "SELECT relay_id FROM bodies WHERE relay_id=?",
                                           (relay_id,)), [(relay_id,)])
                injected.append(relay_id)
                raise InjectedFilingFailure("after relay and body inserts at _write_transitions")

            with mock.patch.object(submit, "_write_transitions", side_effect=fail_at_write_transitions):
                _, statements = self.committed_turn(
                    writer, lambda turn: try_filing(turn, "system-rollback", fail=True),
                )
            self.assertEqual(len(injected), 1)
            self.assertEqual(statements, ["SAVEPOINT engine_system_filing",
                                          "ROLLBACK TO engine_system_filing",
                                          "RELEASE engine_system_filing"])
            self.assertEqual({table: self.durable(f"SELECT * FROM {table}") for table in tables}, before)
            self.assertEqual(self.durable("SELECT label FROM fixture_other"), [])

    def test_turn_foreign_release_read_only(self):
        """Four post-open sender diagnostics and one production-order receiver release."""
        writer = self.seeded_writer(receiver=True)

        def reading(conn, project):
            return self.rows(conn, "SELECT id FROM projects") == [(project,)]

        def catches_deny(conn, _project):
            self.assert_insert_denied(conn, "caught release insert")
            return True

        def lets_deny_escape(conn, _project):
            try:
                self.insert_other(conn, "escaping release insert")
            except sqlite3.Error as error:
                escaping.append(error)
                raise
            return True

        escaping = []
        for label, callback, refusal in (
                ("read", reading, None),
                ("caught denial", catches_deny, ValueError),
                ("not true", lambda _conn, _project: 1, ValueError),
                ("escaped denial", lets_deny_escape, sqlite3.Error)):
            with self.subTest(case=label):
                def probe(turn):
                    def release():
                        return queries._check_foreign_release(turn.sender, P, callback, in_turn=True)

                    if refusal is None:
                        self.assertIsNone(release())
                    elif refusal is ValueError:
                        with self.assertRaisesRegex(refusal, "^foreign read is not released$"):
                            release()
                    else:
                        with self.assertRaises(refusal) as caught:
                            release()
                        self.assertEqual(caught.exception.sqlite_errorname, "SQLITE_AUTH")
                        self.assertIs(caught.exception, escaping[-1])
                    self.assert_writer_policy_restored(turn.sender, label)

                self.committed_turn(writer, probe)

        def receiver_probe(turn):
            def read_receiver(conn):
                self.assertIsInstance(conn, api_writer.TurnConnection)
                queries._check_foreign_release(conn, Q, reading, in_turn=True)
                return "released"

            self.assertEqual(turn.read_receiver(Q, "required", read_receiver), "released")
            turn.open_sender_writes()

        self.committed_turn(writer, receiver_probe, after_open=False)
        self.assertEqual(self.durable("SELECT label FROM fixture_other ORDER BY label"),
                         [("caught denial",), ("escaped denial",), ("not true",), ("read",)])

    def test_turn_guards_restore_writer_policy_after_raise(self):
        """Deliberate post-open diagnostics for restoration after either Engine frame raises."""
        writer = self.seeded_writer()
        sender_error = errors.error_for("E-STORE-BUSY")
        hook_error = RuntimeError("fixture hook raised")

        def check(_conn):
            raise sender_error

        def sender_probe(turn):
            ctx = self.ctx(sender_check=check)
            with self.assertRaises(errors.EngineError) as caught:
                submit._run_sender_check(turn.sender, ctx)
            self.assertIs(caught.exception, sender_error)
            self.assertNotIn(turn.sender, submit._CHECK_FRAMES)
            self.assert_writer_policy_restored(turn.sender, "after sender raise")

        def hook_probe(turn):
            with self.assertRaises(RuntimeError) as caught:
                with submit._hook_frame(turn.sender, self.ctx(), "fixture.hook"):
                    raise hook_error
            self.assertIs(caught.exception, hook_error)
            self.assertNotIn(turn.sender, submit._HOOK_FRAMES)
            self.assert_writer_policy_restored(turn.sender, "after hook raise")

        for probe in (sender_probe, hook_probe):
            with self.subTest(frame=probe.__name__):
                self.committed_turn(writer, probe)
        self.assertEqual(self.durable("SELECT label FROM fixture_other ORDER BY label"),
                         [("after hook raise",), ("after sender raise",)])
