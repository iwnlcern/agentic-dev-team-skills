"""Strict public-data verification; expectations follow design section 7a."""
import ast
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import unittest

from relay_engine.tests.test_strings_gate import gate_violations


FIXTURES = Path(__file__).parent / "fixtures" / "ed25519-speccheck"
# 0/1: small A; 2/3: cofactorless equality with mixed A; 4/5: inequality;
# 6/7: S >= L; 8/9: noncanonical R; 10/11: noncanonical A.
# Outcomes follow the design, never another implementation's results.
SPEC_EXPECTED = (
    (False, "small-order A"), (False, "small-order A"),
    (True, "cofactorless equality"), (True, "cofactorless equality"),
    (False, "cofactorless inequality"), (False, "cofactorless inequality"),
    (False, "S >= L"), (False, "S >= L"),
    (False, "noncanonical R"), (False, "noncanonical R"),
    (False, "noncanonical A"), (False, "noncanonical A"),
)


class VerifierTests(unittest.TestCase):
    def verifier(self):
        self.assertIsNotNone(importlib.util.find_spec("relay_engine.ed25519_verify"),
                             "Task 7 verifier module is absent")
        return importlib.import_module("relay_engine.ed25519_verify")

    def test_verifier_vectors(self):
        verifier = self.verifier()
        cases_bytes = (FIXTURES / "cases.json").read_bytes()
        self.assertEqual(hashlib.sha256(cases_bytes).hexdigest(),
                         "08e47a36d9aead288664930505584f353fff113ab854f2800db1e4f5b3540450")
        self.assertEqual(hashlib.sha256((FIXTURES / "rfc8032.txt").read_bytes()).hexdigest(),
                         "ed63657ff389301282b169b0abde9b5dd2c7e4d524fdfa5da6ff3094fc93c4c3")
        rfc = json.loads((FIXTURES / "rfc8032-vectors.json").read_text())
        self.assertEqual([v["name"] for v in rfc], ["1", "2", "3", "1024", "SHA(abc)"])
        for vector in rfc:
            key, message, signature = (bytes.fromhex(vector[n])
                                       for n in ("public_key", "message", "signature"))
            with self.subTest(rfc=vector["name"]):
                self.assertIs(verifier.check_public_key(key), True)
                self.assertIs(verifier.verify(key, message, signature), True)
            with self.subTest(rfc=vector["name"], flipped_bit=True):
                flipped = bytes([signature[0] ^ 1]) + signature[1:]
                self.assertIs(verifier.verify(key, message, flipped), False)
        cases = json.loads(cases_bytes)
        self.assertEqual(len(cases), 12)
        for index, (vector, (expected, reason)) in enumerate(zip(cases, SPEC_EXPECTED)):
            with self.subTest(speccheck=index, rule=reason):
                self.assertIs(verifier.verify(bytes.fromhex(vector["pub_key"]),
                                              bytes.fromhex(vector["message"]),
                                              bytes.fromhex(vector["signature"])), expected)
        for index in (0, 1, 10, 11):
            with self.subTest(rejected_key=index):
                self.assertIs(verifier.check_public_key(bytes.fromhex(cases[index]["pub_key"])), False)
        # Erratum 4: solving the curve equation at y=3 gives a non-small-order
        # point with even x. The second encoding has y=p+3 and the same sign.
        # Removing BOTH y<p and the re-encoding comparison admits that key;
        # neither deletion alone is distinguishable under the complete decode.
        with self.subTest(canonical_pair="y=3"):
            self.assertIs(verifier.check_public_key(bytes.fromhex(
                "0300000000000000000000000000000000000000000000000000000000000000")), True)
        with self.subTest(canonical_pair="y=p+3"):
            self.assertIs(verifier.check_public_key(bytes.fromhex(
                "f0ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f")), False)

    def test_verifier_off_curve_key(self):
        verifier = self.verifier()
        # y=2 gives a nonsquare x^2 for Edwards25519; no point decodes.
        off_curve = bytes.fromhex("02" + "00" * 31)
        good = json.loads((FIXTURES / "rfc8032-vectors.json").read_text())[0]
        signature = bytes.fromhex(good["signature"])
        self.assertIs(verifier.check_public_key(off_curve), False)
        self.assertIs(verifier.verify(off_curve, b"", signature), False)
        key = bytes.fromhex(good["public_key"])
        for bad in (b"", key[:-1], key + b"x"):
            with self.subTest(key_length=len(bad)):
                self.assertIs(verifier.check_public_key(bad), False)
                self.assertIs(verifier.verify(bad, b"", signature), False)
        for bad in (b"", signature[:-1], signature + b"x"):
            with self.subTest(signature_length=len(bad)):
                self.assertIs(verifier.verify(key, b"", bad), False)
        self.assertIs(verifier.verify(key, b"changed message", signature), False)

    def test_verifier_imports_stdlib_only(self):
        verifier = self.verifier()
        path = Path(verifier.__file__)
        imported = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"hashlib"})
        self.assertEqual(gate_violations([str(path)]), [])


if __name__ == "__main__":
    unittest.main()
