#!/usr/bin/env python3
"""Exercise artifact compression, text encodings and final package names offline."""

import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
sys.dont_write_bytecode = True


def checker():
    spec = importlib.util.spec_from_file_location("artifact_checker", ROOT / "tools/sdk/check_release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_entries(check):
    repository = check.load_release_policy().public_repository().removesuffix(".git")
    metadata = ("Metadata-Version: 2.4\nName: rightnow-system-zero\nVersion: 0.1.0\n"
                "License-Expression: Apache-2.0\nLicense-File: LICENSE\nLicense-File: NOTICE\n"
                "Requires-Python: >=3.10\nRequires-Dist: httpx<0.29,>=0.28.1\n"
                "Requires-Dist: httpcore<2,>=1.0.9\n"
                f"Project-URL: Source, {repository}\nProject-URL: Issues, {repository}/issues\n").encode("ascii")
    license_body = b"Apache License\nVersion 2.0, January 2004\n"
    notice = b"System Zero SDKs\nSynthetic package notice.\n"
    package = json.dumps({
        "name": "@rightnow/system-zero", "version": VERSION,
        "repository": {"type": "git", "url": check.load_release_policy().public_repository()},
    }).encode("utf-8")
    prefix = "rightnow_system_zero-0.1.0/"
    return {
        "npm": {
            "package/package.json": package, "package/dist/index.js": b"export {};\n",
            "package/LICENSE": license_body, "package/NOTICE": notice, "package/docs/": b"",
        },
        "wheel": {
            "rightnow_system_zero-0.1.0.dist-info/METADATA": metadata,
            "system_zero/__init__.py": b"", "system_zero/py.typed": b"",
            "rightnow_system_zero-0.1.0.dist-info/licenses/LICENSE": license_body,
            "rightnow_system_zero-0.1.0.dist-info/licenses/NOTICE": notice,
            "system_zero/docs/": b"",
        },
        "sdist": {
            prefix + "PKG-INFO": metadata, prefix + "src/system_zero/__init__.py": b"",
            prefix + "LICENSE": license_body, prefix + "NOTICE": notice, prefix + "docs/": b"",
        },
    }


def packed(kind, values, level=9):
    output = io.BytesIO()
    if kind == "wheel":
        method = zipfile.ZIP_STORED if level == "stored" else zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(output, "w", compression=method) as archive:
            for name, body in values.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (0o040755 if name.endswith("/") else 0o100644) << 16
                archive.writestr(info, body, compress_type=method,
                                 compresslevel=None if level == "stored" else level)
        return output.getvalue()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, body in values.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
            archive.addfile(info, io.BytesIO(body))
    return gzip.compress(output.getvalue(), compresslevel=level, mtime=0)


def opaque_units(kind, data):
    if kind != "wheel":
        return {data, data[:10], data[-8:]}
    terminal = data.rfind(b"PK\x05\x06")
    units = {data[terminal:]}
    central = struct.unpack_from("<I", data, terminal + 16)[0]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            local = member.header_offset
            name_size, extra_size = struct.unpack_from("<2H", data, local + 26)
            body = local + 30 + name_size + extra_size
            units.add(data[local:body])
            if member.compress_type == zipfile.ZIP_DEFLATED:
                units.add(data[body:body + member.compress_size])
            name_size, extra_size, comment_size = struct.unpack_from("<3H", data, central + 28)
            end = central + 46 + name_size + extra_size + comment_size
            units.add(data[central:end])
            central = end
    return units


def run_controls():
    check = checker()
    entries = fixture_entries(check)
    counts = {"compressionCases": 0, "credentialCases": 0, "encodedControls": 0, "opaqueScans": 0,
              "metadataCases": 0, "inventoryCases": 0, "manifestCases": 0, "contentCases": 0}
    base = ROOT / "tmp"
    base.mkdir(exist_ok=True)
    assert base.resolve().parent == ROOT and not base.is_symlink()
    assert not getattr(base, "is_junction", lambda: False)()
    with tempfile.TemporaryDirectory(prefix="artifact-controls-", dir=base) as directory:
        path = Path(directory) / "sample"
        for kind, values in entries.items():
            for level in [*range(10), *(["stored"] if kind == "wheel" else [])]:
                data = packed(kind, values, level)
                path.write_bytes(data)
                opaque = opaque_units(kind, data)
                original = check.scan_container_bytes
                def observe(value):
                    if value in opaque:
                        counts["opaqueScans"] += 1
                    return original(value)
                check.scan_container_bytes = observe
                try:
                    names = getattr(check, "check_" + kind)(path, VERSION)
                    assert set(names) == {name if kind == "wheel" else name.rstrip("/") for name in values}
                finally:
                    check.scan_container_bytes = original
                counts["compressionCases"] += 1
        assert counts["opaqueScans"] == 0
        counts["contentCases"] = content_controls(check, entries, path)
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        token = "".join(alphabet[(index * 17 + 11) % len(alphabet)] for index in range(24))
        controls = (("Authorization: " + "Bearer " + token, "credential-header"),
                    ("Authorization: " + "Basic " + token, "credential-header"),
                    ("password" + chr(61) + chr(34) + token + chr(34), "generic-secret-assignment"))
        final_names = "@rightnow/system-zero rightnow-system-zero system_zero"
        for kind, values in entries.items():
            prefix = {"npm": "package/", "wheel": "system_zero/", "sdist": "rightnow_system_zero-0.1.0/"}[kind]
            for encoding in ("utf-8", "utf-16", "utf-16-be"):
                def encoded(text):
                    raw = text.encode(encoding)
                    return b"\xfe\xff" + raw if encoding == "utf-16-be" else raw
                positive = {**values, prefix + "README.md": encoded(final_names)}
                path.write_bytes(packed(kind, positive))
                getattr(check, "check_" + kind)(path, VERSION)
                counts["encodedControls"] += 1
                for control, rule in controls:
                    negative = {**values, prefix + "README.md": encoded(control)}
                    path.write_bytes(packed(kind, negative))
                    refuses(lambda: getattr(check, "check_" + kind)(path, VERSION), check.ReleaseError, rule)
                    paired = {**values, prefix + "README.md": encoded("label=" + token)}
                    path.write_bytes(packed(kind, paired))
                    getattr(check, "check_" + kind)(path, VERSION)
                    counts["credentialCases"] += 1
            for filename in (".netrc", "id_ed25519", "configuration.ppk"):
                negative = {**values, prefix + filename: b"Synthetic input.\n"}
                path.write_bytes(packed(kind, negative))
                refuses(lambda: getattr(check, "check_" + kind)(path, VERSION), check.ReleaseError)
                counts["credentialCases"] += 1
        counts.update(metadata_controls(check, entries))
        counts["inventoryCases"] = inventory_controls(check, entries)
        counts["manifestCases"] = manifest_controls(Path(directory))
    return counts


def refuses(action, exception=ValueError, rule=None):
    try:
        action()
    except exception as error:
        if rule is not None:
            assert rule in error.rules, "The expected credential rule did not refuse the control."
        return
    raise AssertionError("Invalid package control was accepted")


def content_controls(check, entries, path):
    count = 0
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    generated = "".join(alphabet[(index * 17 + 11) % len(alphabet)] for index in range(24))
    controls = (
        ("access" + "Token" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
        ("acme_" + generated, "credential-shape"),
        ("password" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
        ("session" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
        ("clientKeyData" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
        ("Authorization: " + "Bearer " + generated, "credential-header"),
        ("Authorization: " + "Basic " + generated, "credential-header"),
        ("databasePasswordPrimary" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
        ("private" + "KeyMaterial" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
        ("signing" + "-key-value" + chr(61) + chr(34) + generated + chr(34), "generic-secret-assignment"),
    )
    for kind, values in entries.items():
        prefix = {"npm": "package/", "wheel": "system_zero/", "sdist": "rightnow_system_zero-0.1.0/"}[kind]
        for value, rule in controls:
            changed = {**values, prefix + "README.md": value.encode("ascii")}
            path.write_bytes(packed(kind, changed))
            refuses(lambda: getattr(check, "check_" + kind)(path, VERSION), check.ReleaseError, rule)
            neutral = {**values, prefix + "README.md": ("label=" + generated).encode("ascii")}
            path.write_bytes(packed(kind, neutral))
            getattr(check, "check_" + kind)(path, VERSION)
            if rule == "generic-secret-assignment":
                with mock.patch.object(check.load_content_rules(), "credential_name", return_value=False):
                    check.scan_text(value, "README.md")
            count += 1
    return count


def metadata_controls(check, entries):
    count = 0
    repository = check.load_release_policy().public_repository().removesuffix(".git")
    check.scan_text("Project-URL: Source, " + repository + "\nProject-URL: Issues, " + repository + "/issues\n",
                    "METADATA")
    count += 1
    for url in (repository + "-other", repository + ".git-other", "https://example.com/repository"):
        raw = json.dumps({"name": "@rightnow/system-zero",
                          "repository": {"type": "git", "url": url}}).encode("ascii")
        refuses(lambda: check.check_npm_metadata(raw), check.ReleaseError)
        count += 1
    package = json.loads(entries["npm"]["package/package.json"])
    for field, value in (
        *(('scripts', {name: "node example.js"}) for name in (
            "preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare",
            "prepublishOnly", "prepack", "postpack", "publish", "postpublish")),
        *((name, {"example": "1.0.0"}) for name in ("dependencies", "optionalDependencies", "peerDependencies")),
        ("bundledDependencies", ["example"]), ("bundleDependencies", ["example"]), ("gypfile", True),
    ):
        refuses(lambda: check.check_npm_metadata(json.dumps({**package, field: value}).encode("ascii")), check.ReleaseError)
        count += 1
    metadata = entries["wheel"]["rightnow_system_zero-0.1.0.dist-info/METADATA"]
    for label in (b"License-Expression", b"License-File: NOTICE", b"Project-URL: Source", b"Project-URL: Issues"):
        changed = b"\n".join(line for line in metadata.split(b"\n") if not line.startswith(label))
        refuses(lambda: check.python_identity(changed, VERSION), check.ReleaseError)
        count += 1
    for name in ("package/dist/index.js.map", "package/binding.gyp", "package/dist/native.node"):
        refuses(lambda: check.check_member_name(name), check.ReleaseError)
        count += 1
    return {"metadataCases": count}


def inventory_controls(check, entries):
    count = 0
    notices = {name: entries["npm"]["package/" + name] for name in ("LICENSE", "NOTICE")}
    for kind, values in entries.items():
        actual = {name: raw for name, raw in values.items() if not name.endswith("/")}
        if kind == "npm":
            expected = dict(actual)
        elif kind == "wheel":
            expected = {name: raw for name, raw in actual.items() if name.startswith("system_zero/")}
            root = "rightnow_system_zero-0.1.0.dist-info/"
            actual.update({root + "WHEEL": b"Wheel-Version: 1.0\n", root + "RECORD": b""})
        else:
            expected = {name: raw for name, raw in actual.items() if not name.endswith("/PKG-INFO")}
        check.check_payload_inventory(kind, actual, expected, VERSION, notices)
        count += 1
        for changed in ({**actual, "extra.txt": b"extra\n"}, {**actual, next(iter(expected)): b"changed\n"}):
            refuses(lambda: check.check_payload_inventory(kind, changed, expected, VERSION, notices), check.ReleaseError)
            count += 1
        if kind == "wheel":
            for name in notices:
                changed = {**actual, root + "licenses/" + name: b"different\n"}
                refuses(lambda: check.check_payload_inventory(kind, changed, expected, VERSION, notices), check.ReleaseError)
                count += 1
            changed = {**actual, root + "unexpected.py": b"pass\n"}
            refuses(lambda: check.check_payload_inventory(kind, changed, expected, VERSION, notices), check.ReleaseError)
            count += 1
    return count


def manifest_controls(directory):
    spec = importlib.util.spec_from_file_location("manifest_controls", ROOT / "tools/sdk/verify_manifest.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    path = directory / "manifest.json"
    path.write_bytes(b'{"files":{"package.tgz":"original"}}\n')
    digest = verifier.manifest_digest(path)
    verifier.verify(path, digest)
    path.write_bytes(b'{"files":{"package.tgz":"changed"}}\n')
    refuses(lambda: verifier.verify(path, digest))
    for value in ("", "a" * 63, digest.upper()):
        refuses(lambda: verifier.verify(path, value))
    return 5


if __name__ == "__main__":
    print(json.dumps(run_controls(), sort_keys=True))
