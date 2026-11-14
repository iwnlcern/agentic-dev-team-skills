# Ed25519 verifier fixtures

cases.json and LICENSE are unmodified retained files from https://github.com/novifinancial/ed25519-speccheck at commit 65519336fda78a3d016e947df6d82848aca0c9da.
cases.json SHA256: 08e47a36d9aead288664930505584f353fff113ab854f2800db1e4f5b3540450.
LICENSE SHA256: c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4.
The upstream vectors are distributed under the accompanying Apache License 2.0.

rfc8032.txt is the retained RFC Editor text, https://www.rfc-editor.org/rfc/rfc8032.txt, SHA256 ed63657ff389301282b169b0abde9b5dd2c7e4d524fdfa5da6ff3094fc93c4c3.
Its embedded copyright and licence notices remain intact.
rfc8032-vectors.json extracts only the public key, message and signature of section 7.1 TEST 1, 2, 3, 1024 and SHA(abc), preserving their bytes.
The fixture called TEST 1024 has a 1023-byte message, as the RFC states.
Expected outcomes and their reasons are in test_ed25519_verify.py, derived from the governing design rather than another verifier.
