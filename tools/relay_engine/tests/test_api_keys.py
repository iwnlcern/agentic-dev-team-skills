"""Seat key secret, verifier and transactional test-only issuance (Slice A Task 1)."""

import base64
import hashlib
import hmac
import secrets
import sqlite3
import unittest
import uuid
from unittest import mock

from relay_engine import api_keys
from relay_engine import schema


class KeyTests(unittest.TestCase):
    def store(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        schema.init_v3(conn)
        conn.executescript("""
            INSERT INTO projects (id, name, created_at) VALUES ('p', 'p', 1);
            INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)
                VALUES ('t', 'p', 'test', 'pair', 1, 'operator');
            INSERT INTO seats (id, team_instance_id, role, state)
                VALUES ('s', 't', 'planner', 'vacant');
        """)
        return conn

    def test_secret_is_43_unpadded_base64url_from_32_bytes(self):
        # Break: token size/alphabet changes or a non-secure source replaces token_bytes.
        with mock.patch.object(secrets, "token_bytes", wraps=secrets.token_bytes) as source:
            value = api_keys.new_key_secret()
        source.assert_called_once_with(32)
        self.assertRegex(value, r"\A[A-Za-z0-9_-]{43}\Z")
        self.assertEqual(len(base64.urlsafe_b64decode(value + "=")), 32)
        self.assertNotEqual(value, api_keys.new_key_secret())

    def test_verifier_vector(self):
        # Break: missing domain/NULs, reversed inputs or digest encoded differently.
        key_id = "00000000-0000-7000-8000-000000000001"
        secret = "A" * 43
        expected = hashlib.sha256(
            b"adt-seat-key-v1\x00" + key_id.encode("ascii") + b"\x00" + secret.encode("ascii")
        ).hexdigest()
        self.assertEqual(api_keys.KEY_FORMAT, "adt-seat-key-v1")
        self.assertEqual(api_keys.key_verifier(key_id, secret), expected)

    def test_verifier_is_domain_separated(self):
        # Break: concatenation without NUL separation accepts these ambiguous boundaries.
        self.assertNotEqual(api_keys.key_verifier("ab", "c"),
                            api_keys.key_verifier("a", "bc"))
        self.assertNotEqual(api_keys.key_verifier("ab", "c"),
                            hashlib.sha256(b"ab\x00c").hexdigest())

    def test_verifier_matches_uses_constant_time_compare(self):
        key_id, secret = "test-id", "A" * 43
        stored = api_keys.key_verifier(key_id, secret)
        # Break: ordinary equality instead of the specified constant-time primitive.
        with mock.patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertTrue(api_keys.verifier_matches(stored, key_id, secret))
            compare.assert_called_once_with(stored, stored)
        with mock.patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertFalse(api_keys.verifier_matches(stored, key_id, "B" * 43))
            compare.assert_called_once_with(stored, api_keys.key_verifier(key_id, "B" * 43))

    def test_issue_opens_occupant_and_returns_secret_once(self):
        conn = self.store()
        conn.execute("BEGIN IMMEDIATE")
        key_id, secret = api_keys.issue_occupant_key(conn, "s", 1000)
        row = conn.execute("SELECT * FROM seat_occupants").fetchone()
        self.assertEqual(str(uuid.UUID(key_id)), key_id)
        self.assertEqual(uuid.UUID(key_id).version, 7)
        self.assertEqual(str(uuid.UUID(row[0])), row[0])
        self.assertEqual(row[1:], ("s", key_id, api_keys.key_verifier(key_id, secret), 1000, None))
        self.assertNotIn(secret, repr(row))
        self.assertEqual(conn.execute("SELECT state FROM seats WHERE id='s'").fetchone()[0],
                         "occupied")
        # Break: helper commits on its own instead of leaving the caller in control.
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(conn.execute("SELECT count(*) FROM seat_occupants").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT state FROM seats WHERE id='s'").fetchone()[0],
                         "vacant")

    def test_issue_refuses_stood_down_and_occupied_and_unknown(self):
        for state, seat_id, reason in (("stood-down", "s", "seat_stood_down"),
                                       ("vacant", "missing", "seat_unknown"),
                                       ("occupied", "s", "seat_occupied")):
            with self.subTest(reason=reason):
                conn = self.store()
                conn.execute("UPDATE seats SET state=? WHERE id='s'", (state,))
                if state == "occupied":
                    conn.execute("INSERT INTO seat_occupants VALUES ('old','s','old-key','v',1,NULL)")
                before = conn.execute("SELECT * FROM seat_occupants").fetchall()
                with self.assertRaises(api_keys.IssueRefused) as caught:
                    api_keys.issue_occupant_key(conn, seat_id, 1000)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(conn.execute("SELECT * FROM seat_occupants").fetchall(), before)

    def test_issue_outside_transaction_raises(self):
        conn = self.store()
        with self.assertRaises(ValueError):
            api_keys.issue_occupant_key(conn, "s", 1000)
        self.assertEqual(conn.execute("SELECT count(*) FROM seat_occupants").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT state FROM seats").fetchone()[0], "vacant")

    def test_refusal_messages_carry_no_secret(self):
        conn = self.store()
        conn.execute("BEGIN IMMEDIATE")
        _, secret = api_keys.issue_occupant_key(conn, "s", 1000)
        with self.assertRaises(api_keys.IssueRefused) as caught:
            api_keys.issue_occupant_key(conn, "s", 1001)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(str(caught.exception), "seat_occupied")


if __name__ == "__main__":
    unittest.main()
