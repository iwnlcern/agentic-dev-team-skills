import base64
import os
import tempfile
import unittest

from relay_engine import commission, errors, supersede
from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, establish_run_identity, init_schema
from relay_engine.paths import Root, ensure_engine_dir


def relay(run_id, dispatch, sender, to, *, extra="", subject="fixture"):
    return ("## relay\n\n"
            "ROLE: Planner\nPHASE: PLAN\nAUTHORITY: plan-only\n"
            "DISPATCH_ID: %s\nRUN_ID: %s\nCEREMONY_TIER: large\n"
            "EVIDENCE_TARGET: E2\nHUMAN_GATE_REQUIRED: no\n"
            "FROM: %s\nTO: %s\n%sSUBJECT: %s\n\nbody\n" % (
                dispatch, run_id, sender, to, extra, subject))


class TestAdoptedSupersession(unittest.TestCase):
    def setUp(self):
        self.parent_temp = tempfile.TemporaryDirectory()
        self.parent = Root(self.parent_temp.name)
        self.parent_engine = ensure_engine_dir(self.parent.dirfd)
        self.parent_ledger = init_schema(
            self.parent_engine, self.parent.path)
        dispatch_text = relay(
            "parent", "commission-child", "parent.planner",
            "parent.implementer")
        dispatched = admit(
            self.parent_ledger, self.parent, parse_draft(dispatch_text),
            dispatch_text.encode(), "dispatch-sid")
        self.record_result = commission.commission(
            self.parent_ledger, self.parent, dispatched.rendered_path,
            "child")
        ruling_text = relay(
            "parent", "operator-ruling", "operator", "child",
            extra="SUPERSEDES: child/target.md\n", subject="bind child")
        ruling = admit(
            self.parent_ledger, self.parent, parse_draft(ruling_text),
            ruling_text.encode(), "ruling-sid")
        self.bundle_result = supersede.export_ruling(
            self.parent_ledger, ruling.rendered_path, "child")

    def tearDown(self):
        self.parent_ledger.close()
        os.close(self.parent_engine)
        self.parent.close()
        self.parent_temp.cleanup()

    def test_adoption_binds_exact_target_and_replays(self):
        with tempfile.TemporaryDirectory() as child_name:
            child = Root(child_name)
            child_engine = ensure_engine_dir(child.dirfd)
            ledger = init_schema(child_engine, child.path)
            try:
                record = base64.b64decode(
                    self.record_result["record_b64"])
                establish_run_identity(ledger, "child", record)
                target_text = relay(
                    "child", "target", "child.planner",
                    "child.implementer")
                target = admit(
                    ledger, child, parse_draft(target_text),
                    target_text.encode(), None, origin="hand",
                    stamp="20260808-010000",
                    rendered_path="child/target.md")
                bundle = base64.b64decode(
                    self.bundle_result["bundle_b64"])
                adopted = supersede.adopt_ruling(ledger, child, bundle)
                self.assertTrue(adopted["applied"])
                self.assertFalse(adopted["duplicate"])
                self.assertEqual(ledger.execute(
                    "SELECT origin,admits_against_seq FROM relays "
                    "WHERE seq=?", (adopted["ruling_seq"],)).fetchone(),
                    ("adopted", None))
                self.assertEqual(ledger.execute(
                    "SELECT target_seq,ruling_seq,source,applied "
                    "FROM supersession_edges").fetchone(),
                    (target.seq, adopted["ruling_seq"], "adopted", 1))
                replay = supersede.adopt_ruling(ledger, child, bundle)
                self.assertTrue(replay["duplicate"])
                self.assertEqual(ledger.execute(
                    "SELECT COUNT(*) FROM supersession_edges").fetchone()[0],
                    1)
            finally:
                ledger.close()
                os.close(child_engine)
                child.close()

    def test_tampered_bundle_refuses(self):
        bundle = bytearray(base64.b64decode(
            self.bundle_result["bundle_b64"]))
        bundle[10] ^= 1
        with tempfile.TemporaryDirectory() as child_name:
            child = Root(child_name)
            child_engine = ensure_engine_dir(child.dirfd)
            ledger = init_schema(child_engine, child.path)
            try:
                with self.assertRaises(errors.EngineError) as caught:
                    supersede.adopt_ruling(ledger, child, bytes(bundle))
                self.assertEqual(caught.exception.code, "E-CARRIER-INVALID")
                self.assertEqual(ledger.execute(
                    "SELECT COUNT(*) FROM supersession_edges").fetchone()[0],
                    0)
            finally:
                ledger.close()
                os.close(child_engine)
                child.close()



class TestCarrierFields(TestAdoptedSupersession):
    """PR-4 fold D1, D2: every malformed carrier gets a carrier code, never E-ENVELOPE."""

    def adopt(self, data):
        with tempfile.TemporaryDirectory() as child_name:
            child = Root(child_name)
            child_engine = ensure_engine_dir(child.dirfd)
            ledger = init_schema(child_engine, child.path)
            try:
                establish_run_identity(ledger, "child", base64.b64decode(
                    self.record_result["record_b64"]))
                target_text = relay("child", "target", "child.planner", "child.implementer")
                admit(ledger, child, parse_draft(target_text), target_text.encode(), None,
                      origin="hand", stamp="20260808-010000", rendered_path="child/target.md")
                try:
                    supersede.adopt_ruling(ledger, child, data)
                except errors.EngineError as error:
                    return error
                return None
            finally:
                ledger.close()
                os.close(child_engine)
                child.close()

    def fields(self):
        from relay_engine.jcs import parse_record
        return parse_record(base64.b64decode(self.bundle_result["bundle_b64"]))[0]

    def framed(self, fields):
        from relay_engine.jcs import frame_record
        return frame_record(fields)

    def altered(self, **changes):
        fields = self.fields()
        for name, value in changes.items():
            if value is KeyError:
                del fields[name]
            else:
                fields[name] = value
        return self.adopt(self.framed(fields))

    def test_control_adopts(self):
        self.assertIsNone(self.adopt(self.framed(self.fields())))

    def test_deep_nesting_is_invalid(self):  # D1, C1
        import hashlib
        line = b"[" * 200000 + b"]" * 200000
        data = line + b"\n" + hashlib.sha256(line).hexdigest().encode() + b"\n"
        self.assertEqual(self.adopt(data).code, "E-CARRIER-INVALID")

    def test_wrong_types_are_field(self):  # D1
        for name, value in (("ruling_body_b64", 5), ("ruling_body_b64", None),
                            ("ruling_body_b64", ["x"]), ("ruling_content_digest", 5),
                            ("child_run_id", 7), ("ruling_seq", "1"), ("ruling_seq", True),
                            ("ruling_stamp", None)):
            with self.subTest(name=name, value=value):
                self.assertEqual(self.altered(**{name: value}).code, "E-CARRIER-FIELD")

    def test_version_and_field_set(self):  # D2
        for changes in ({"v": True}, {"v": 2}, {"extra": "x"}, {"ruling_path": KeyError}):
            with self.subTest(changes=repr(changes)):
                self.assertEqual(self.altered(**changes).code, "E-CARRIER-FIELD")
        # JCS writes 1.0 as 1, so a carrier holding 1.0 is framed by hand
        import hashlib
        import json
        line = json.dumps(self.fields(), sort_keys=True, separators=(",", ":")).replace(
            '"v":1', '"v":1.0').encode()
        self.assertIn(b'"v":1.0', line)
        data = line + b"\n" + hashlib.sha256(line).hexdigest().encode() + b"\n"
        self.assertEqual(self.adopt(data).code, "E-CARRIER-FIELD")

    def test_body_faults_name_the_field(self):  # D1, C2
        import hashlib
        latin = b"caf\xe9\n"
        cases = (
            ({"ruling_body_b64": "!!!not base64!!!"}, "ruling_body_b64", "not base64"),
            ({"ruling_body_b64": base64.b64encode(latin).decode(),
              "ruling_content_digest": hashlib.sha256(latin).hexdigest()},
             "ruling_body_b64", "not UTF-8 text"),
            ({"ruling_content_digest": "0" * 64}, "ruling_content_digest",
             "does not match the carried body"),
        )
        for changes, field, reason in cases:
            with self.subTest(field=field, reason=reason):
                error = self.altered(**changes)
                self.assertEqual(error.code, "E-CARRIER-INVALID")
                self.assertIn(field, error.cause)
                self.assertIn(reason, error.cause)
                self.assertNotIn("envelope", error.remedy)


if __name__ == "__main__":
    unittest.main()

