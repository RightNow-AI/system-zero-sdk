"""Neutral controls for credential detection and repository file hygiene."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise ValueError("Test dependency is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULES = load("public_credential_rules", "tools/sdk/public_content_rules.py")
TREE = load("public_tree_checks", "tools/sdk/scan_tree.py")
ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generated(length):
    return "".join(ALPHABET[(index * 17 + 11) % len(ALPHABET)] for index in range(length))


def expressions(left, right):
    a, b = json.dumps(left), json.dumps(right)
    return (
        a + " + " + b,
        "[" + a + ", " + b + "].join('')",
        a + " " + b,
        "''.join((" + a + ", " + b + "))",
        "`${" + a + "}${" + b + "}`",
        a + ".concat(" + b + ")",
        "'%s%s' % (" + a + ", " + b + ")",
        "'{}{}'.format(" + a + ", " + b + ")",
        "f'{" + a + "}{" + b + "}'",
    )


class PublicContentControls(unittest.TestCase):
    def test_public_wording_controls(self):
        tests = load("public_wording_controls", "tools/sdk/test_public_wording.py")
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromModule(tests).run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def checked(self, callback, value):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = callback(value)
        self.assertEqual(output.getvalue(), "", "Scanned input must not be written to diagnostics.")
        return result

    def named(self, source, neutral):
        for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids):
            self.assertIn("generic-secret-assignment", self.checked(callback, source))
            self.assertNotIn("opaque-token", self.checked(callback, source))
            self.assertEqual(self.checked(callback, neutral), [])
        with mock.patch.object(RULES, "credential_name", return_value=False):
            self.assertNotIn("generic-secret-assignment", self.checked(RULES.public_rule_ids, source))

    def test_flags_prefixed_literals_and_qualified_plurals(self):
        value = generated(24)
        for name in ("api-key", "client-secret", "api_key", "password", "token"):
            for separator in ("=", " "):
                for literal in (value, json.dumps(value)):
                    with self.subTest(flag=name, separator=separator):
                        self.named("run --" + name + separator + literal,
                                   "run --label" + separator + literal)
        for name in ("API_KEYS_PROD", "clientSecrets", "tokens_json", "WEBHOOK_SECRETS_V2", "PASSWORDS",
                     "cookies", "accessTokens", "signingKeys", "CREDS", "dbCreds", "pat", "SERVICE_PAT",
                     "jwt", "otp", "totpSeed", "passcode", "signature", "sig"):
            with self.subTest(name=name):
                self.named(json.dumps({name: value}), json.dumps({"label": value}))
        for prefix in ("b", "r", "f", "rb", "br", "u"):
            with self.subTest(prefix=prefix):
                self.named("API_KEY " + chr(61) + " " + prefix + json.dumps(value),
                           "label " + chr(61) + " " + prefix + json.dumps(value))
        for expression in ("(" + json.dumps(value) + ")",
                           "(\n" + json.dumps(value[:12]) + "\n" + json.dumps(value[12:]) + "\n)",
                           "b" + json.dumps(value[:12]) + " + b" + json.dumps(value[12:])):
            self.named("API_KEY " + chr(61) + " " + expression, "label " + chr(61) + " " + expression)

    def test_nested_mappings_setters_and_fallback_literals(self):
        value = generated(24)
        controls = (
            ("<apiKey>" + value + "</apiKey>", "<label>" + value + "</label>"),
            ('client.setApiKey("' + value + '")', 'client.setLabel("' + value + '")'),
            ('const apiKey ' + chr(61) + ' process.env.VALUE ?? "' + value + '";', 'const label = process.env.VALUE ?? "' + value + '";'),
            ('const apiKey ' + chr(61) + ' process.env.VALUE || "' + value + '";', 'const label = process.env.VALUE || "' + value + '";'),
            ('api_key ' + chr(61) + ' os.getenv("VALUE") or "' + value + '"', 'label = os.getenv("VALUE") or "' + value + '"'),
        )
        for number, (source, neutral) in enumerate(controls):
            with self.subTest(control=number):
                self.named(source, neutral)
        for name in ("SYSTEM_ZERO_API_KEY", "DATABASE_PASSWORD", "API_TOKEN"):
            self.named("env:\n  " + name + ": " + value + "\n", "env:\n  label: " + value + "\n")
        for source in ('run --api-key "$SYSTEM_ZERO_API_KEY"', 'run --token "${{ secrets.API_KEY }}"',
                       'API_KEY ' + chr(61) + ' f"{configuration.api_key}"', 'apiKey ' + chr(61) + ' process.env.VALUE || configuration.key',
                       'signature = "ordinary-document-label"'):
            self.assertEqual(self.checked(RULES.public_rule_ids, source), [])

    def test_named_literal_credential_collections(self):
        values = json.dumps(generated(24)) + ", " + json.dumps(generated(12))
        for name in ("API_KEYS_PROD", "clientSecrets", "SECRETS", "tokens_json"):
            for left, right in (("[", "]"), ("(", ")")):
                self.named(name + " = " + left + values + right, "LABELS = " + left + values + right)
        for source in ("USAGE_KEYS = ['input_tokens', 'cached_input_tokens']",
                       "API_KEYS = [configuration.primary, configuration.secondary]"):
            self.assertEqual(self.checked(RULES.public_rule_ids, source), [])

    def test_header_schemes_and_restored_generic_shapes(self):
        for scheme in ("Bearer", "Basic", "Token", "ApiKey", "Custom-Auth"):
            for value in (generated(24), "c7a19e0b4d8625f3" * 2 + "c7a19e0b"):
                positive = "Authorization" + chr(58) + " " + scheme + " " + value
                negative = "Description" + chr(58) + " " + scheme + " " + value
                self.assertIn("credential-header", self.checked(RULES.public_rule_ids, positive))
                self.assertEqual(self.checked(RULES.public_rule_ids, negative), [])
        digest = "c7a19e0b4d8625f3" * 2
        self.assertIn("hex-credential", self.checked(RULES.public_rule_ids, json.dumps({"label": digest})))
        for name in ("sha256", "md5", "WORKFLOW_SHA256", "cache_key", "idempotency_key"):
            self.assertEqual(self.checked(RULES.public_rule_ids, json.dumps({name: digest})), [])
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        value = "NEUT" + "".join(alphabet[(index * 7 + 3) % len(alphabet)] for index in range(16))
        self.assertIn("access-key-id", self.checked(RULES.public_rule_ids, value))
        self.assertIn("access-key-id", self.checked(RULES.public_rule_ids, value[:16] + "0189"))
        self.assertIn("access-key-id", self.checked(RULES.public_rule_ids, "N3UT" + value[4:]))
        self.assertEqual(self.checked(RULES.public_rule_ids, value.lower()), [])
        for separator in ("-", "_"):
            for index in (20, 22, 30, 38):
                value = generated(40)
                value = value[:index] + separator + value[index + 1:]
                self.assertIn("opaque-token", self.checked(RULES.public_rule_ids, json.dumps({"label": value})))
        self.assertEqual(self.checked(RULES.public_rule_ids, json.dumps({"label": "ordinary-data-label"})), [])

    def test_qualified_credential_names(self):
        names = ("secretKey", "encryptionKey", "masterKey", "sessionKey", "hmacKey", "authKey",
                 "adminKey", "webhookKey", "secret-key", "API_KEY_PROD", "API_KEY_2", "apiKeyValue",
                 "apiKeyB64", "SECRET_V2", "TOKEN_VALUE", "tokenValue", "secretValue", "DB_PASSWORD_PROD",
                 "PRIVATE_KEY_PEM", "SIGNING_SECRET_B64", "CLIENT_SECRET_JSON", "AccountKey", "auth",
                 "bearer", "cookie", "session", "sessionId", "passphrase", "pwd", "pass", "client-key-data")
        for number, name in enumerate(names):
            for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids):
                with self.subTest(control=number):
                    self.assertIn("generic-secret-assignment", self.checked(callback, json.dumps({name: generated(24)})))
                    self.assertEqual(self.checked(callback, json.dumps({"label": generated(24)})), [])
            with self.subTest(mutation=number), mock.patch.object(RULES, "credential_name", return_value=False):
                self.assertEqual(self.checked(RULES.public_rule_ids, json.dumps({name: generated(24)})), [])

    def test_short_named_credentials_and_headers(self):
        for number, name in enumerate(("password", "secret", "apiKey", "passphrase", "pwd", "pin")):
            for length in (8, 12, 15):
                with self.subTest(control=number, length=length):
                    self.assertIn("generic-secret-assignment", self.checked(RULES.archive_rule_ids, json.dumps({name: generated(length)})))
                    self.assertEqual(self.checked(RULES.archive_rule_ids, json.dumps({"label": generated(length)})), [])
        for number, prefix in enumerate(("Cookie: session" + chr(61), "Set-Cookie: sessionId" + chr(61), "Authorization: Basic ")):
            with self.subTest(header=number):
                expected = "credential-header" if prefix.startswith("Authorization") else "generic-secret-assignment"
                self.assertIn(expected, self.checked(RULES.archive_rule_ids, prefix + generated(24)))
                self.assertEqual(self.checked(RULES.archive_rule_ids, "Description: " + generated(24)), [])

    def test_constant_string_expressions(self):
        for number, expression in enumerate(expressions("acme_", generated(24))):
            for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids):
                with self.subTest(control=number):
                    self.assertIn("credential-shape", self.checked(callback, "const value = " + expression))
                    self.assertEqual(self.checked(callback, '["acme_", "' + generated(24) + '"]'), [])
            with self.subTest(mutation=number), mock.patch.object(RULES, "_fold_literals", side_effect=lambda value: value):
                self.assertNotIn("credential-shape", self.checked(RULES.public_rule_ids, expression))
        for number, expression in enumerate(expressions(generated(12)[:6], generated(12)[6:])):
            with self.subTest(named=number):
                self.assertIn("generic-secret-assignment", self.checked(RULES.rule_ids, "password" + chr(61) + expression))
                self.assertEqual(self.checked(RULES.rule_ids, "label" + chr(61) + expression), [])
            with self.subTest(named_mutation=number), mock.patch.object(RULES, "_fold_literals", side_effect=lambda value: value):
                self.assertNotIn("generic-secret-assignment", self.checked(RULES.rule_ids, "password" + chr(61) + expression))

    def test_long_literal_fields_are_refused_conservatively(self):
        for number, name in enumerate(("accessToken", "clientSecret", "serviceCredential", "privateKey")):
            with self.subTest(control=number):
                self.assertIn("archive-secret-assignment", self.checked(RULES.archive_rule_ids, json.dumps({name: "A" * 24})))
                self.assertEqual(self.checked(RULES.archive_rule_ids, json.dumps({"label": "A" * 24})), [])

    def test_references_and_digest_contexts_remain_valid(self):
        digest = "c7a19e0b4d8625f3" * 4
        values = (json.dumps({"sha256": digest}), "--hash=sha256:" + digest,
                  json.dumps({"idempotency_key": digest}), "Authorization: `Bearer ${this.#key}`",
                  "def ensure_package_resources_are_present(): pass", "Public SDK documentation.")
        for number, value in enumerate(values):
            with self.subTest(control=number):
                self.assertEqual(self.checked(RULES.public_rule_ids, value), [])

    def test_compact_checks_preserve_shapes_without_inventing_assignments(self):
        controls = ((chr(45) * 5 + "BEGINPRIVATEKEY" + chr(45) * 5, "private-key"),
                    ("Authorization:Bearer" + generated(32), "credential-header"),
                    ("Authorization:Basic" + generated(32), "credential-header"),
                    ("token_" + generated(24), "credential-shape"))
        for number, (value, rule) in enumerate(controls):
            with self.subTest(control=number):
                self.assertIn(rule, self.checked(RULES.compact_rule_ids, value))
        for number, value in enumerate((
            "const valid = keys.some(key => typeof key !== 'string');",
            "const selected = values.map(key => key.length);",
            "const headers = { Authorization: `Bearer ${this.#key}` };",
            "run --label " + generated(24),
        )):
            with self.subTest(source=number):
                self.assertEqual(self.checked(RULES.compact_rule_ids, "".join(value.split())), [])

    def test_opaque_and_segmented_values_have_generic_controls(self):
        controls = ((generated(40), "opaque-token"), ("OPAQ" + generated(76), "opaque-token"),
                    (base64.b64encode(bytes(range(64))).decode("ascii"), "opaque-token"),
                    ("ZX." + generated(22) + "." + generated(24), "segmented-token"),
                    ("https://service.example.com/actions/100/" + generated(24), "url-token"),
                    ("https://service.example.com/actions/" + generated(24), "url-token"))
        for number, (value, rule) in enumerate(controls):
            for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids):
                with self.subTest(control=number):
                    self.assertIn(rule, self.checked(callback, value))
        digest = "c7a19e0b4d8625f3" * 4
        integrity = "sha512-" + base64.b64encode(bytes(range(64))).decode("ascii")
        controls = (json.dumps({"integrity": integrity}),
                    json.dumps({"resolved": "https://registry.npmjs.org/synthetic-package/-/synthetic-package-1.2.3.tgz",
                                "integrity": integrity}),
                    "uses: example/action@" + digest[:40],
                    "https://github.com/example/project/tree/" + digest[:40],
                    'const names = "' + ALPHABET + '";')
        for number, value in enumerate(controls):
            for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids, RULES.compact_rule_ids):
                with self.subTest(reference=number):
                    self.assertEqual(self.checked(callback, value), [])

    def test_symbolic_labels_and_computed_references_remain_source_values(self):
        label = ("class Label(str, Enum):\n    INVALID_IDEMPOTENCY_KEY " + chr(61)
                 + " " + json.dumps("invalid_idempotency_key") + "\n")
        controls = (label,
                    "const rows = fields.map(key => `| ${key} | ${report.usage.totals[key]} |`);",
                    "const key = configuration.transport.reference;")
        for number, value in enumerate(controls):
            with self.subTest(control=number):
                self.assertEqual(self.checked(RULES.archive_rule_ids, value), [])
        for name in ("INVALID_IDEMPOTENCY_KEY", "SECRET_KEY", "SIGNING_KEY"):
            self.assertIn("generic-secret-assignment", self.checked(RULES.archive_rule_ids, json.dumps({name: generated(24)})))
            self.assertEqual(self.checked(RULES.archive_rule_ids, json.dumps({"label": generated(24)})), [])

    def test_source_and_compiled_expressions_remain_valid(self):
        digest = "c7a19e0b4d8625f3" * 4
        controls = (
            "const valid = keys.some(key => !Object.hasOwn(value, key));",
            "const valid = keys.some(key => row[key] !== record[key]);",
            "const apiKey " + chr(61) + " source.transport.credentialReference;",
            "client = Service(api_key" + chr(61) + "configuration.connection.reference)",
            "const key = deterministicKey(namespace, request);",
            'settings.remove("SYSTEM_ZERO_API_KEY", raising=False)',
            "assert result[key] == approximation(expected[key], abs=1e-12)",
            "response(200, json=records[key], headers={})",
            'message = "Idempotency-Key must be nonblank ASCII and 255 characters or less."',
            'const ERROR_LABELS = { invalid_cache_key: [400, "Cache-Key must contain ASCII characters."] };',
            "def ensure_package_resources_are_present(): pass",
            "def test_choice_ties_use_utf8_byte_order(): pass",
            '"node_modules/component-android-arm64": {}',
            json.dumps({"idempotency_key": "record-v1-" + "c7a19e0b4d8625f3" * 4}),
            json.dumps({"value": True, "sha256": digest, "idempotency_key": "record-v1-" + digest}),
            "usage = ('input_tokens', 'cached_input_tokens', 'media_tokens')",
            "const usage = ['input_tokens', 'cached_input_tokens', 'media_tokens'];",
            "def test_question_order_is_utf8_sorted_without_changing_wire_order(): pass",
            json.dumps({"node_modules/componentparser-linux-arm64-gnu": {"version": "1.2.3"}}),
        )
        for number, value in enumerate(controls):
            for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids):
                with self.subTest(control=number):
                    self.assertEqual(self.checked(callback, value), [])
            with self.subTest(compact=number):
                self.assertEqual(self.checked(RULES.compact_rule_ids, "".join(value.split())), [])

    def test_literal_credentials_inside_expressions_remain_refused(self):
        digest = "c7a19e0b4d8625f3" * 2
        controls = (
            "const apiKey " + chr(61) + " " + json.dumps(generated(24)) + ";",
            "const values = keys.map(key " + chr(61) + "> " + json.dumps(generated(24)) + ");",
            "const apiKey " + chr(61) + " " + json.dumps("source.transport.credentialReference") + ";",
            "const apiKey " + chr(61) + " source.transport.reference || " + json.dumps(generated(24)) + ";",
            json.dumps({"idempotency_key": "record-v1-" + generated(24)}),
            json.dumps({"apiKey": digest}),
            "const apiKey " + chr(61) + " " + json.dumps("deterministicKey") + ";",
            'lookup("apiKey", password' + chr(61) + json.dumps(generated(12)) + ")",
        )
        rules = ("generic-secret-assignment", "generic-secret-assignment", "archive-secret-assignment",
                 "generic-secret-assignment", "credential-shape", "generic-secret-assignment",
                 "archive-secret-assignment", "generic-secret-assignment")
        negatives = (
            "const label = " + json.dumps(generated(24)) + ";",
            "const values = items.map(label => " + json.dumps(generated(24)) + ");",
            "const label = " + json.dumps("source.transport.credentialReference") + ";",
            "const label = source.transport.reference || " + json.dumps(generated(24)) + ";",
            json.dumps({"idempotency_key": generated(24)}),
            json.dumps({"idempotency_key": digest}),
            "const label = " + json.dumps("deterministicKey") + ";",
            'lookup("label", label=' + json.dumps(generated(12)) + ")",
        )
        for number, (value, rule, negative) in enumerate(zip(controls, rules, negatives)):
            with self.subTest(control=number):
                self.assertIn(rule, self.checked(RULES.archive_rule_ids, value))
                self.assertEqual(self.checked(RULES.archive_rule_ids, negative), [])
        for number, value in enumerate(expressions("acme_", generated(24))):
            with self.subTest(split=number):
                self.assertIn("credential-shape", self.checked(RULES.archive_rule_ids, value))

    def test_long_lowercase_prefixed_tokens_are_not_numeric_word_identifiers(self):
        body = "".join(chr(97 + (index * 17 + 11) % 26) for index in range(24))
        value = "token_" + body + "73"
        for callback in (RULES.rule_ids, RULES.archive_rule_ids, RULES.public_rule_ids, RULES.compact_rule_ids):
            with self.subTest(entry=callback.__name__):
                self.assertIn("credential-shape", self.checked(callback, value))
                self.assertEqual(self.checked(callback, "component-arm64"), [])

    def test_sensitive_paths_are_refused_without_body_values(self):
        names = ("keys/id_ed25519", "keys/id_rsa.pub", ".git-credentials", ".docker/config.json", ".ssh/config",
                 ".aws/config", ".netrc", "deploy.ppk", "release.jks", "release.keystore", "credentials.json",
                 "keys/id_rsa_old", "keys/id_ed25519-backup", "keys/id_ecdsa-copy", ".pgpass", ".htpasswd",
                 "signing.p8", "state.tfstate", "state.tfstate.backup", "values.tfvars", "values.tfvars.json",
                 "service-account.json", "service-account-production.json", "service_account.json",
                 "production.env", "config.env", "prod.env.json")
        for number, name in enumerate(names):
            with self.subTest(control=number):
                self.assertIn("sensitive-path", TREE.scan_mapping({name: b""}, rules=RULES))

    def test_sensitive_names_do_not_reach_filesystem_access(self):
        class Directory:
            def absolute(self):
                return self

            def resolve(self):
                return self

            def is_dir(self):
                return True

            def joinpath(self, *_parts):
                raise AssertionError("A sensitive name reached filesystem access.")

        directory = Directory()
        with mock.patch.object(TREE, "Path", return_value=directory), \
                mock.patch.object(TREE, "_linked", return_value=False), \
                mock.patch.object(TREE, "_tracked_names", return_value=("credentials.json",)):
            with self.assertRaisesRegex(ValueError, "Sensitive"):
                TREE.read_tree("unused")

    def test_tree_scanning_refuses_generated_and_opaque_files(self):
        base = {"README.md": b"Public SDK.", "tools/sdk/check_release.py": b""}
        self.assertEqual(TREE.scan_mapping(base, rules=RULES), [])
        changes = (({"tools/sdk/__pycache__/module.pyc": b"opaque"}, "generated-path"),
                   ({"typescript/dist/index.js": b"export {};"}, "generated-path"),
                   ({"tmp/vite/result.json": b"{}"}, "generated-path"), ({"README.md": b"\x00\xff"}, "binary-content"),
                   ({"README.md": json.dumps({"accessToken": generated(24)}).encode("ascii")}, "generic-secret-assignment"))
        for number, (change, rule) in enumerate(changes):
            with self.subTest(control=number):
                self.assertIn(rule, TREE.scan_mapping({**base, **change}, rules=RULES))


if __name__ == "__main__":
    unittest.main()
