#!/usr/bin/env python3
"""Check staged bytes and import resources from actual built SDK archives offline."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True


def require(condition, label):
    if not condition:
        raise ValueError(label)


def load(name):
    spec = importlib.util.spec_from_file_location("payload_" + name, ROOT / "tools/sdk" / (name + ".py"))
    require(spec is not None and spec.loader is not None, "tool-load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files(directory, regular):
    regular(directory, directory=True)
    found = {}
    for path in sorted(directory.iterdir()):
        if path.name == "__pycache__":
            continue
        if path.is_dir():
            for name, raw in files(path, regular).items():
                found[path.name + "/" + name] = raw
        else:
            regular(path)
            found[path.name] = path.read_bytes()
    return found


def payloads(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {member.filename: b"" if member.is_dir() else archive.read(member)
                    for member in archive.infolist()}
    with tarfile.open(path, "r:gz") as archive:
        result = {}
        for member in archive.getmembers():
            if member.isdir():
                result[member.name.rstrip("/") + "/"] = b""
            elif member.isfile():
                stream = archive.extractfile(member)
                require(stream is not None, "artifact-body")
                with stream:
                    result[member.name] = stream.read()
        return result


def write_payloads(directory, contents):
    directory.mkdir(parents=True)
    for name, raw in contents.items():
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in name, "payload-path")
        path = directory.joinpath(*relative.parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


PYTHON_IMPORT = r'''
import importlib, pathlib, sys
def offline(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('offline import')
sys.addaudithook(offline)
directory = pathlib.Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(directory))
module = importlib.import_module('rightnow.tools')
assert isinstance(module.NESTED_FUNCTION_TOOLS, list) and module.NESTED_FUNCTION_TOOLS
assert pathlib.Path(module.__file__).resolve().is_relative_to(directory)
'''

NODE_IMPORT = r'''
import { pathToFileURL } from 'node:url';
import { Socket } from 'node:net';
Socket.prototype.connect = function () { throw new Error('offline import'); };
globalThis.fetch = async function () { throw new Error('offline import'); };
const tools = await import(pathToFileURL(process.argv[1]).href);
if (!Array.isArray(tools.NESTED_FUNCTION_TOOLS) || tools.NESTED_FUNCTION_TOOLS.length === 0) throw new Error('missing tool resources');
'''


def check(args):
    prepare, checker = load("prepare_packages"), load("check_release")
    stage, artifacts, node = args.stage.absolute(), args.artifacts.absolute(), args.node.absolute()
    prepare.regular(stage, directory=True)
    prepare.regular(artifacts, directory=True)
    require(node.is_file(), "node-path")
    runtime = args.python_runtime.absolute()
    require(runtime.is_file() and runtime != Path(sys.executable).absolute(), "runtime-path")
    npm = json.loads(prepare.read(stage / "npm/package.json"))
    python = tomllib.loads(prepare.read(stage / "python/pyproject.toml"))["project"]
    version = npm["version"]
    require(npm["name"] == "@rightnowai/rightnow" and python["name"] == "rightnow-sdk"
            and python["version"] == version, "package-identities")
    with contextlib.redirect_stdout(io.StringIO()):
        checker.check_artifacts(artifacts, "sdk-v" + version, version, False)
    paths = {
        "npm": artifacts / "npm" / ("rightnowai-rightnow-" + version + ".tgz"),
        "wheel": artifacts / "python" / ("rightnow_sdk-" + version + "-py3-none-any.whl"),
        "sdist": artifacts / "python" / ("rightnow_sdk-" + version + ".tar.gz"),
    }
    compatible = tuple(map(int, version.split('.'))) >= (0, 5, 0)
    if compatible:
        paths.update({
            "npm-shim": artifacts / "npm-shim" / ("rightnowai-system-zero-" + version + ".tgz"),
            "wheel-shim": artifacts / "python-shim" / ("rightnow_system_zero-" + version + "-py3-none-any.whl"),
            "sdist-shim": artifacts / "python-shim" / ("rightnow_system_zero-" + version + ".tar.gz"),
        })
    manifest = (artifacts / "manifest.json").read_bytes()
    digests = {kind: hashlib.sha256(path.read_bytes()).digest() for kind, path in paths.items()}
    contents = {kind: payloads(path) for kind, path in paths.items()}
    dist = files(stage / "npm/dist", prepare.regular)
    sources = files(stage / "npm/src", prepare.regular)
    require(sources and all(
        name.removesuffix(".ts") + suffix in dist
        for name in sources if name.endswith(".ts") for suffix in (".js", ".d.ts")
    ), "compiled-input")
    npm_expected = {"package/dist/" + name: raw for name, raw in dist.items()}
    for name in ("package.json", "README.md", "LICENSE", "NOTICE"):
        path = stage / "npm" / name
        prepare.regular(path)
        npm_expected["package/" + name] = path.read_bytes()
    python_expected = {"rightnow/" + name: raw for name, raw in files(
        stage / "python/src/rightnow", prepare.regular,
    ).items()}
    require("rightnow/tools.py" in python_expected, "python-input")
    prefix = "rightnow_sdk-" + version + "/"
    sdist_expected = {prefix + "src/" + name: raw for name, raw in python_expected.items()}
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
        path = stage / "python" / name
        prepare.regular(path)
        sdist_expected[prefix + name] = path.read_bytes()
    notices = {name: sdist_expected[prefix + name] for name in ("LICENSE", "NOTICE")}
    for kind, expected in (("npm", npm_expected), ("wheel", python_expected), ("sdist", sdist_expected)):
        checker.check_payload_inventory(kind, contents[kind], expected, version, notices)
    base = ROOT / "tmp"
    base.mkdir(exist_ok=True)
    prepare.regular(base, directory=True)
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    with tempfile.TemporaryDirectory(prefix="p-", dir=base) as temporary:
        work = Path(temporary)
        write_payloads(work / "npm", {name: contents["npm"][name] for name in npm_expected})
        write_payloads(work / "wheel", {name: contents["wheel"][name] for name in python_expected})
        write_payloads(work / "sdist", {name: contents["sdist"][prefix + "src/" + name] for name in python_expected})
        commands = [
            [str(node), "--input-type=module", "--eval", NODE_IMPORT,
             str(work / "npm/package/dist/integrations/function-tools/index.js")],
            [str(runtime), "-I", "-B", "-c", PYTHON_IMPORT, str(work / "wheel")],
            [str(runtime), "-I", "-B", "-c", PYTHON_IMPORT, str(work / "sdist")],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=work, env=environment, capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            require(result.returncode == 0, "archived-import")
    if compatible:
        check_compatibility(stage, artifacts, checker, prepare, node, runtime, version, environment, base)
    with contextlib.redirect_stdout(io.StringIO()):
        checker.check_artifacts(artifacts, "sdk-v" + version, version, False)
    require((artifacts / "manifest.json").read_bytes() == manifest
            and all(hashlib.sha256(path.read_bytes()).digest() == digests[kind] for kind, path in paths.items()),
            "artifact-changed")
    return {"verified": 6 if compatible else 3, "imports": {"npm": True, "wheel": True, "sdist": True}, "unchanged": True}


def check_compatibility(stage, artifacts, checker, prepare, node, runtime, version, environment, base):
    """Check the forwarding bytes and import them beside the preferred packages."""
    npm = artifacts / 'npm-shim' / ('rightnowai-system-zero-' + version + '.tgz')
    wheel = artifacts / 'python-shim' / ('rightnow_system_zero-' + version + '-py3-none-any.whl')
    sdist = artifacts / 'python-shim' / ('rightnow_system_zero-' + version + '.tar.gz')
    expected_npm = {'package/' + name: raw for name, raw in files(stage / 'npm-shim', prepare.regular).items()}
    expected_py = {'system_zero/' + name: raw for name, raw in files(
        stage / 'python-shim/src/system_zero', prepare.regular).items()}
    prefix = 'rightnow_system_zero-' + version + '/'
    expected_sdist = {prefix + 'src/' + name: raw for name, raw in expected_py.items()}
    for name in ('pyproject.toml', 'README.md', 'LICENSE', 'NOTICE'):
        expected_sdist[prefix + name] = (stage / 'python-shim' / name).read_bytes()
    notices = {name: (stage / 'npm-shim' / name).read_bytes() for name in ('LICENSE', 'NOTICE')}
    with checker.compatibility_identity():
        for kind, archive, expected in (('npm', npm, expected_npm), ('wheel', wheel, expected_py), ('sdist', sdist, expected_sdist)):
            checker.check_payload_inventory(kind, payloads(archive), expected, version, notices)
    with tempfile.TemporaryDirectory(prefix='compat-', dir=base) as temporary:
        work = Path(temporary)
        new_npm = artifacts / 'npm' / ('rightnowai-rightnow-' + version + '.tgz')
        for name, archive in (('rightnow', new_npm), ('system-zero', npm)):
            write_payloads(work / 'node_modules/@rightnowai' / name,
                           {key.removeprefix('package/'): raw for key, raw in payloads(archive).items() if not key.endswith('/')})
        for suffix, archive in (('wheel', wheel), ('sdist', sdist)):
            primary = payloads(artifacts / 'python' / ('rightnow_sdk-' + version + '-py3-none-any.whl'))
            legacy = payloads(archive)
            if suffix == 'sdist':
                legacy = {key.removeprefix(prefix + 'src/'): raw for key, raw in legacy.items() if key.startswith(prefix + 'src/system_zero/')}
            combined = {key: raw for key, raw in {**primary, **legacy}.items() if key.startswith(('rightnow/', 'system_zero/')) and not key.endswith('/')}
            write_payloads(work / suffix, combined)
            script = """import importlib, sys, warnings
def offline(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('offline import')
sys.addaudithook(offline)
sys.path.insert(0, sys.argv[1])
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    import rightnow, system_zero, system_zero.stream, system_zero.tools
    importlib.reload(system_zero)
assert len(caught) == 1 and caught[0].category is DeprecationWarning
assert system_zero.SystemZero is system_zero.RightNow
assert issubclass(system_zero.RightNow, rightnow.RightNow)
assert system_zero.stream.SystemZeroStreamClient is system_zero.RightNowStreamClient
assert issubclass(system_zero.RightNowStreamClient, rightnow.RightNowStreamClient)
assert issubclass(system_zero.tools.ToolHandler, importlib.import_module('rightnow.tools').ToolHandler)
assert system_zero.tools.ToolHandler._wire.name == 'system-zero'
if hasattr(rightnow, 'ToolHandler'):
    assert system_zero.ToolHandler is system_zero.tools.ToolHandler
assert system_zero.RightNow._wire.name == 'system-zero'
for Client in (system_zero.RightNowStreamClient, system_zero.stream.stream_client, system_zero.stream_client):
    assert Client(api_key='fixture-key')._wire.name == 'system-zero'
"""
            result = subprocess.run([str(runtime), '-I', '-B', '-c', script, str(work / suffix)], cwd=work,
                                    env=environment, capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            require(result.returncode == 0, 'compatibility-import')
        script = """import assert from 'node:assert/strict';
import * as old from '@rightnowai/system-zero';
import * as current from '@rightnowai/rightnow';
import * as stream from '@rightnowai/system-zero/stream';
import * as tools from '@rightnowai/system-zero/integrations/function-tools';
import * as currentTools from '@rightnowai/rightnow/integrations/function-tools';
// The clients and the stream subprotocol are the shim's own: the legacy wire keeps system-zero.stream.v1.
const clients = new Set(['RightNowClient', 'SystemZeroClient', 'RightNowStreamClient', 'SystemZeroStreamClient', 'STREAM_PROTOCOL']);
for (const name of Object.keys(current)) if (!clients.has(name)) assert.equal(old[name], current[name]);
assert.equal(old.STREAM_PROTOCOL, 'system-zero.stream.v1');
assert.equal(old.RightNowClient, old.SystemZeroClient);
assert.ok(old.RightNowClient.prototype instanceof current.RightNowClient);
assert.equal(stream.SystemZeroStreamClient, old.RightNowStreamClient);
assert.ok(stream.RightNowStreamClient.prototype instanceof current.RightNowStreamClient);
assert.equal(tools.NESTED_FUNCTION_TOOLS, currentTools.NESTED_FUNCTION_TOOLS);
"""
        result = subprocess.run([str(node), '--input-type=module', '--eval', script], cwd=work,
                                env=environment, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(result.returncode == 0, 'compatibility-import')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args), sort_keys=True))
    except Exception:
        print("Package payload check refused invalid or incomplete artifacts.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
