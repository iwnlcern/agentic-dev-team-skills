import json
import os
import subprocess
import sys
import unittest

from relay_engine.jcs import (commissioned_by_value, frame_record,
                              jcs_encode, parse_record)


RECORD = {
    "v": 1,
    "parent_root_uuid": "123e4567-e89b-12d3-a456-426614174000",
    "commissioning_path": "master/relays/dispatch.md",
    "dispatch_content_digest": "a" * 64,
    "child_run_id": "v29-child",
}


class TestJCS(unittest.TestCase):
    def test_canonical_bytes_and_utf16_key_order(self):
        value = {"z": 1, "a": "line\nquote\"", "\U0001f600": 2,
                 "\ufffd": 3}
        self.assertEqual(
            jcs_encode(value),
            b'{"a":"line\\nquote\\\"","z":1,"\xf0\x9f\x98\x80":2,"\xef\xbf\xbd":3}',
        )

    def test_number_serialization_matches_jcs_boundaries(self):
        cases = ((-0.0, b"0"), (1.0, b"1"), (1e-7, b"1e-7"),
                 (1e-6, b"0.000001"), (1e20, b"100000000000000000000"),
                 (1e21, b"1e+21"), (4.5, b"4.5"))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(jcs_encode(value), expected)

    def test_non_json_values_are_rejected(self):
        for value in (float("nan"), float("inf"), {1: "bad"},
                      b"bytes", "\ud800", 9007199254740992,
                      -9007199254740992):
            with self.subTest(value=repr(value)), self.assertRaises(
                    (TypeError, ValueError)):
                jcs_encode(value)


class TestRecordCarrier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.golden = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "relay-engine-fixtures", "golden",
            "commissioning-record.golden")

    def test_golden_byte_equality(self):
        with open(self.golden, "rb") as source:
            expected = source.read()
        self.assertEqual(frame_record(RECORD), expected)

    def test_cross_process_encoding_is_byte_identical(self):
        program = (
            "import base64,json,sys;"
            "from relay_engine.jcs import frame_record;"
            "sys.stdout.write(base64.b64encode(frame_record(json.loads(sys.argv[1]))).decode())"
        )
        child = subprocess.run(
            [sys.executable, "-c", program,
             json.dumps(RECORD, separators=(",", ":"))],
            capture_output=True, text=True, env=dict(os.environ),
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        import base64
        self.assertEqual(base64.b64decode(child.stdout), frame_record(RECORD))

    def test_round_trip_hashes_carried_line_bytes(self):
        carried = frame_record(RECORD)
        fields, digest = parse_record(carried)
        self.assertEqual(fields, RECORD)
        self.assertEqual(
            digest,
            "fd7d3a367abbfa95b898d3db45a0179e50bd34a224340b567917f322ebb902b0",
        )

    def test_one_byte_tamper_and_bad_framing_refuse(self):
        carried = frame_record(RECORD)
        changed = carried.replace(b"v29-child", b"v29-chile", 1)
        for bad in (changed, carried[:-1], carried + b"extra\n",
                    carried.replace(b"fd7d", b"FD7D", 1)):
            with self.subTest(bad=bad[-12:]), self.assertRaises(ValueError):
                parse_record(bad)

    def test_commissioned_by_value_is_exact_jcs(self):
        self.assertEqual(
            commissioned_by_value(
                "123e4567-e89b-12d3-a456-426614174000",
                "master/relays/dispatch.md", "a" * 64,
                "fd7d3a367abbfa95b898d3db45a0179e50bd34a224340b567917f322ebb902b0",
            ),
            b'{"commissioning_path":"master/relays/dispatch.md","dispatch_content_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","parent_root_uuid":"123e4567-e89b-12d3-a456-426614174000","record_digest":"fd7d3a367abbfa95b898d3db45a0179e50bd34a224340b567917f322ebb902b0"}',
        )


if __name__ == "__main__":
    unittest.main()
