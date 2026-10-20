"""Refuse a release unless source and built package identities match its tag.

Python 3.11+; standard library only. This command never contacts a registry.
--write-manifest records build checksums; without it, artifact checks require
the unchanged manifest downloaded from that build job. Every artifact check
also scans archive contents without extracting them.
"""
from __future__ import annotations
import argparse
from email import policy
from email.parser import BytesParser
from functools import cache
import hashlib
import io
from contextlib import contextmanager
import importlib.util
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tarfile
import tomllib
from types import ModuleType
from urllib.parse import unquote, urlsplit
import zipfile
import zlib
NPM_NAME = '@rightnowai/system-zero'
PYTHON_NAME = 'rightnow-system-zero'
IMPORT_NAME = 'system_zero'
DRAFT_IMPORT = '_'.join(('system', 'zero', 'draft'))
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_CONTAINER_BYTES = MAX_ARCHIVE_BYTES + MAX_MEMBERS * 1024 + 65536
URL = re.compile('(?:[a-z][a-z0-9+.-]*://|//(?=[a-z0-9_]|\\[))[^\\s<>\\"\'`\\\\]+', re.IGNORECASE)
PRIVATE_REPOSITORY = re.compile('github\\.com/(?!RightNow\\-AI/system\\-zero\\-sdk(?:\\.git)?(?:[/\\s?#\\"\']|$))[^/\\s]+/[^/\\s]+', re.IGNORECASE)
SEMVER = re.compile('sdk-v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\\+([0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*))?')
STRUCTURAL_RULES = (('tag must', 'tag-format'), ('releases require', 'version-format'), ('version 0.0.0', 'reserved-version'), ('npm package name', 'npm-package-name'), ('Python package name', 'python-package-name'), ('package versions', 'source-version'), ('rename must', 'rename-required'), ('artifact import package', 'artifact-import'), ('npm artifact version', 'artifact-version'), ('Python artifact version', 'artifact-version'), ('npm artifact package name', 'npm-package-name'), ('Python artifact package name', 'python-package-name'), ('artifact filename', 'artifact-filename'), ('expected exactly', 'artifact-inventory'), ('artifact manifest is missing', 'manifest-missing'), ('artifact manifest differs', 'manifest-mismatch'), ('artifact content contains an internal URL or private', 'private-repository'), ('artifact content contains an internal URL', 'internal-url'), ('artifact content has duplicate JSON', 'invalid-metadata'))

class ReleaseError(Exception):
    """A refusal with only controlled rule IDs and hashed identities in diagnostics."""

    def __init__(self, message: str, rule: str | None=None, *, rules: list[str] | None=None):
        super().__init__(message)
        self.rules = rules or [rule or next((identifier for prefix, identifier in STRUCTURAL_RULES if message.startswith(prefix)), 'invalid-artifact')]
        self.member = 'none'

    def diagnostic(self) -> str:
        return f"release check: count={len(self.rules)} rules={','.join(self.rules)} member={self.member}"

def require(condition: bool, message: str, rule: str | None=None) -> None:
    if not condition:
        raise ReleaseError(message, rule)

@contextmanager
def member_context(name: str):
    """Attach an irreversible short identity; raw names never reach refusal output."""
    try:
        yield
    except ReleaseError as error:
        if error.member == 'none':
            error.member = 'sha256:' + hashlib.sha256(name.encode('utf-8', errors='surrogatepass')).hexdigest()[:12]
        raise

@cache
def load_package_names() -> ModuleType:
    """Use the same sibling normalizer from the CLI and importlib callers."""
    try:
        spec = importlib.util.spec_from_file_location('sdk_package_names', Path(__file__).with_name('package_names.py'))
        if spec is None or spec.loader is None:
            raise ImportError('missing name normalizer')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        raise ReleaseError('artifact scanner is unavailable', 'scanner-unavailable') from None
    return module

@cache
def load_content_rules() -> ModuleType:
    try:
        spec = importlib.util.spec_from_file_location('sdk_content_rules', Path(__file__).with_name('content_rules.py'))
        if spec is None or spec.loader is None:
            raise ImportError('missing rule loader')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        raise ReleaseError('artifact scanner is unavailable', 'scanner-unavailable') from None
    return module

@cache
def load_public_content_rules() -> ModuleType:
    """Load the reviewed generic public-source rules beside the repository rules."""
    try:
        spec = importlib.util.spec_from_file_location('sdk_public_content_rules', Path(__file__).with_name('public_content_rules.py'))
        if spec is None or spec.loader is None:
            raise ImportError('missing rule loader')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        raise ReleaseError('public artifact scanner is unavailable', 'scanner-unavailable') from None
    return module

@cache
def load_release_policy() -> ModuleType:
    """Load the sibling policy for both CLI and importlib-based archive checks."""
    try:
        spec = importlib.util.spec_from_file_location('sdk_release_policy', Path(__file__).with_name('release_policy.py'))
        if spec is None or spec.loader is None:
            raise ImportError('missing policy loader')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        raise ReleaseError('artifact content repository policy is unavailable or invalid') from None
    return module

def check_source(root: Path, tag: str) -> str:
    require(SEMVER.fullmatch(tag) is not None, 'tag must be sdk-v<semver>')
    version = tag.removeprefix('sdk-v')
    require(re.fullmatch('(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)', version) is not None, 'releases require stable MAJOR.MINOR.PATCH versions identical in both registries')
    require(version != '0.0.0', "version 0.0.0 is reserved for the bootstrap package")
    npm = json.loads((root / 'typescript/package.json').read_text(encoding='utf-8'))
    with (root / 'python/pyproject.toml').open('rb') as stream:
        python = tomllib.load(stream)['project']
    require(npm.get('name') == NPM_NAME, f'npm package name must be {NPM_NAME}')
    require(python.get('name') == PYTHON_NAME, f'Python package name must be {PYTHON_NAME}')
    require(npm.get('version') == python.get('version') == version, f'package versions must both equal tag {version}')
    packages = root / 'python/src'
    require((packages / IMPORT_NAME / '__init__.py').is_file() and (not (packages / DRAFT_IMPORT).exists()), 'rename must be complete before releasing the SDKs')
    require({path.name for path in packages.iterdir() if path.is_dir() and path.name != '__pycache__'} == {IMPORT_NAME}, 'Python import package inventory is invalid')
    return version

def archive_names(names: list[str], directories: set[str]) -> None:
    require(len(names) <= MAX_MEMBERS, 'too many archive members', 'archive-member-count')
    require(len(names) == len({PurePosixPath(name).as_posix() for name in names}), 'duplicate archive member', 'duplicate-member')
    for name in names:
        with member_context(name):
            check_member_name(name)
            path = PurePosixPath(name)
            require(bool(path.parts) and (not path.is_absolute()) and ('..' not in path.parts) and ('\\' not in name) and (':' not in name) and (name == path.as_posix() or (name in directories and name == path.as_posix() + '/')) and all((ord(character) >= 32 and ord(character) != 127 for character in name)), 'unsafe artifact archive member', 'unsafe-member-name')

def check_internal_urls(value: str) -> None:
    value = unquote(value.replace('\\/', '/'))
    require(PRIVATE_REPOSITORY.search(value) is None, 'artifact content contains an internal URL or private repository identifier')
    for match in URL.finditer(value):
        candidate = match.group().rstrip('.,;!?)>}]')
        if candidate.count('[') > candidate.count(']'):
            candidate += ']'
        if candidate.startswith('///') or candidate.startswith('//#'):
            continue
        require(PRIVATE_REPOSITORY.search(candidate) is None, 'artifact content contains an internal URL or private repository identifier')
        try:
            parsed = urlsplit(candidate)
            host = (parsed.hostname or '').lower().rstrip('.')
            internal = not host or parsed.scheme.lower() == 'file' or parsed.username is not None
            if host:
                try:
                    address = ipaddress.ip_address(host)
                    internal = internal or not address.is_global or address.is_multicast
                except ValueError:
                    internal = internal or '.' not in host or host.endswith(('.localhost', '.local', '.localdomain', '.internal', '.lan', '.home', '.corp', '.intranet', '.test', '.invalid')) or (re.fullmatch('(?:0x[0-9a-f]+|[0-9]+)(?:\\.(?:0x[0-9a-f]+|[0-9]+))*', host) is not None)
        except ValueError:
            internal = True
        require(not internal, 'artifact content contains an internal URL')
ESCAPE = re.compile('\\\\(?:u\\{([0-9a-fA-F]{1,6})\\}|u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|([nrtbfv\\\\/\'\\"]))')
ESCAPE_CHARS = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\x08', 'f': '\x0c', 'v': '\x0b'}

def normalized_forms(value: str) -> tuple[list[str], str]:
    """One normalization path for names, bodies and archive text metadata."""
    forms = [value]

    def decode(match: re.Match[str]) -> str:
        number = next((group for group in match.groups()[:3] if group is not None), None)
        if number is not None:
            codepoint = int(number, 16)
            require(codepoint <= 1114111, 'invalid string escape', 'text-encoding')
            return chr(codepoint)
        character = match[4]
        return ESCAPE_CHARS.get(character, character)
    for _ in range(8):
        decoded = ESCAPE.sub(decode, forms[-1])
        if decoded == forms[-1]:
            break
        forms.append(decoded)
    else:
        require(ESCAPE.sub(decode, forms[-1]) == forms[-1], 'nested escape limit', 'scan-limit')
    return (forms, ''.join(forms[-1].split()))

def decode_text(raw: bytes) -> str:
    """Binary data is refused, never silently skipped; UTF-8/UTF-16 text is supported."""
    require(len(raw) <= MAX_MEMBER_BYTES, 'member exceeds scan limit', 'scan-limit')
    try:
        require(not raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')), 'unsupported binary encoding', 'binary-content')
        value = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    except UnicodeDecodeError:
        raise ReleaseError('unsupported binary data', 'binary-content') from None
    require(not any((ord(character) < 32 and character not in '\t\r\n' or 127 <= ord(character) < 160 for character in value)), 'unsupported binary or control data', 'binary-content')
    return value

def public_project_urls(repository: str) -> dict[str, str]:
    """The public Python package's canonical Project-URL headers, labels as written to METADATA."""
    return {'Changelog': repository + '/blob/main/CHANGELOG.md', 'Source': repository, 'Issues': repository + '/issues', 'Homepage': repository, 'Documentation': repository}

def scan_text(value: str, path: str='') -> None:
    forms, joined = normalized_forms(value)
    scanner = load_content_rules()
    found = sorted({rule for form in forms for rule in scanner.public_rule_ids(form, path)} | set(scanner.compact_rule_ids(joined)))
    if found:
        raise ReleaseError('artifact content contains credential-shaped data', rules=found)
    normalize_name = load_package_names().normalize_package_name
    draft = normalize_name(DRAFT_IMPORT)
    for form in (*forms, joined):
        require(draft not in normalize_name(form), 'artifact content contains a draft name', 'draft-name')
    repository_view = unquote(forms[-1])
    if load_release_policy().SDK_SOURCE_PUBLIC and PurePosixPath(path).name in {'METADATA', 'PKG-INFO'}:
        raw_metadata = value.encode('utf-8')
        metadata = BytesParser(policy=policy.default).parsebytes(raw_metadata)
        versions = metadata.get_all('Version', [])
        require(len(versions) == 1, 'artifact content has invalid Python metadata', 'invalid-metadata')
        check_public_python_identity(raw_metadata, str(versions[0]))
        approved = load_release_policy().public_repository().removesuffix('.git')
        example = 'https://sdk.example.com'
        headers = {f'Project-URL: {label}, {url}': f'Project-URL: {label}, ' + example + url[len(approved):] for label, url in public_project_urls(approved).items()}
        lines = repository_view.splitlines(keepends=True)
        for index, line in enumerate(lines):
            complete = line.rstrip('\r\n')
            if not complete:
                break
            if index + 1 < len(lines) and lines[index + 1].startswith((' ', '\t')):
                continue
            if complete in headers:
                lines[index] = headers[complete] + line[len(complete):]
        repository_view = ''.join(lines)
    for form in (*forms, ''.join(repository_view.split())):
        require(PRIVATE_REPOSITORY.search(unquote(form)) is None, 'artifact content contains an internal URL or private repository identifier')
    for form in forms:
        check_internal_urls(form)

def check_member_name(name: str) -> None:
    scan_text(name)
    if load_release_policy().SDK_SOURCE_PUBLIC:
        require(not load_public_content_rules().sensitive_path_rule_ids(name), 'artifact member has a credential-shaped name', 'forbidden-member-name')
    lowered = name.lower()
    for part in PurePosixPath(lowered).parts:
        forbidden = re.search('(?:^|[._-])(?:tests?|specs?|fixtures?|snapshots?)(?:[._-]|$)', part) is not None or part.startswith('.env') or part.endswith('.env') or (part in {'conftest.py', '.npmrc', '.pypirc', '.netrc', '.ssh', '.aws'}) or (part in {'binding.gyp', '__pycache__'}) or ('secret' in part) or ('credential' in part) or part.endswith(('.pem', '.key', '.p12', '.pfx', '.keystore')) or part.endswith(('.map', '.pyc', '.pyo', '.tsbuildinfo', '.node', '.so', '.dll', '.dylib', '.pyd')) or (re.match('id_(?:rsa|dsa|ecdsa|ed25519)(?:\\.|$)', part) is not None) or (re.search('(?:^|[._-])(?:api[._-]?key|private[._-]?key|keys?|tokens?)(?:[._-]|$)', part) is not None)
        require(not forbidden, 'artifact content has a test, fixture, environment or key-shaped member', 'forbidden-member-name')

def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, 'artifact content has duplicate JSON fields')
        result[key] = value
    return result

def check_npm_metadata(raw: bytes) -> None:
    package = json.loads(decode_text(raw), object_pairs_hook=unique_json_object)
    require(isinstance(package, dict), 'artifact content has invalid npm metadata', 'invalid-metadata')
    scripts = package.get('scripts', {})
    require(isinstance(scripts, dict) and all((isinstance(name, str) and isinstance(command, str) for name, command in scripts.items())), 'artifact content has invalid npm scripts', 'invalid-metadata')
    lifecycle = {'preinstall', 'install', 'postinstall', 'prepublish', 'preprepare', 'prepare', 'postprepare', 'prepublishOnly', 'prepack', 'postpack', 'publish', 'postpublish', 'preuninstall', 'uninstall', 'postuninstall', 'dependencies'}
    require(not lifecycle.intersection(scripts), 'artifact content has npm lifecycle hooks', 'npm-lifecycle')
    for name in ('dependencies', 'optionalDependencies', 'peerDependencies'):
        if name in package:
            require(isinstance(package[name], dict) and (not package[name]), 'artifact content has npm runtime dependencies', 'npm-runtime-dependencies')
    for name in ('bundledDependencies', 'bundleDependencies'):
        if name in package:
            require(isinstance(package[name], list) and (not package[name]), 'artifact content has npm bundled dependencies', 'npm-runtime-dependencies')
    require('gypfile' not in package, 'artifact content has npm native build metadata', 'npm-native-build')
    release_policy = load_release_policy()
    try:
        release_policy.check_npm_repository(package)
    except release_policy.ReleaseRuleError:
        raise ReleaseError('artifact content violates repository metadata policy', 'repository-metadata') from None
WHEEL_RECORD_ROW = re.compile('[^,\\r\\n]+,sha256=[A-Za-z0-9_-]{43},[0-9]+|[^,\\r\\n]+,,')

def wheel_record_view(value: str) -> str:
    """A wheel RECORD has one row per file: its path, urlsafe base64 SHA-256 digest and size. Every row must have
    exactly that shape; the digests are opaque by design, so only paths and sizes reach the content scanner."""
    rows = value.splitlines()
    require(all((WHEEL_RECORD_ROW.fullmatch(row) for row in rows)), 'artifact content has an invalid wheel RECORD', 'wheel-record')
    return '\n'.join((row.partition(',sha256=')[0] + ',' + row.rpartition(',')[2] for row in rows))

def check_member_content(name: str, raw: bytes, *, npm_metadata: bool=False) -> None:
    """The same name/body scanner applies to every format and text metadata record."""
    with member_context(name):
        check_member_name(name)
        value = decode_text(raw)
        member = PurePosixPath(name)
        if member.name == 'RECORD' and member.parent.name.endswith('.dist-info'):
            value = wheel_record_view(value)
        scan_text(value, name)
        if PurePosixPath(name).name.casefold().partition('.')[0] == 'readme':
            names = load_package_names()
            draft = names.normalize_package_name(DRAFT_IMPORT)
            require(all((draft not in names.normalize_package_name(form) for form in names.readme_visible_forms(value))), 'artifact content contains a draft name', 'draft-name')
        if npm_metadata and name == 'package/package.json':
            check_npm_metadata(raw)

def check_notices(nonempty: set[str], prefixes: tuple[str, ...]) -> None:
    for name in ('LICENSE', 'NOTICE'):
        require(any((prefix + name in nonempty for prefix in prefixes)), f'artifact content is missing {name}', 'missing-' + name.lower())

def scan_container_bytes(raw: bytes) -> None:
    """Apply the shared text rules to raw TAR headers and ZIP text fields.

    TAR headers contain structural NULs, so decode_text's binary refusal does
    not apply to this view. Binary gzip/ZIP integers and compressed bodies are
    validated structurally, never interpreted as text.
    UTF-8 also preserves Unicode whitespace for the existing joined form.
    """
    for value in dict.fromkeys((raw.decode('latin-1'), raw.decode('utf-8', errors='replace'))):
        scan_text(value)

def canonical_tar_header(header: bytes) -> None:
    scan_container_bytes(header)
    for start, stop in ((0, 100), (157, 257), (265, 297), (297, 329), (345, 500)):
        field = header[start:stop]
        end = field.find(b'\x00')
        require(end < 0 or not any(field[end:]), 'noncanonical tar string', 'archive-metadata')
    for start, stop in ((100, 108), (108, 116), (116, 124), (124, 136), (136, 148), (329, 337), (337, 345)):
        field = header[start:stop]
        end = field.find(b'\x00')
        require(end < 0 or not any(field[end:]), 'noncanonical tar number', 'archive-metadata')
        require(re.fullmatch(b'(?:[0-7]+ ?)?\\x00*', field) is not None, 'unsupported tar number', 'archive-metadata')
    require(re.fullmatch(b'[0-7]{6}(?:\\x00 | \\x00|\\x00\\x00)|[0-7]{7}\\x00', header[148:156]) is not None, 'noncanonical tar checksum', 'archive-metadata')
    require(header[257:265] in (b'ustar\x00' + b'00', bytes(8)) and (not any(header[500:])), 'unsupported tar header', 'archive-metadata')

def read_tar(path: Path) -> tarfile.TarFile:
    """Account for a single gzip stream and every physical TAR record.

    Regular files, empty directories and text PAX records are supported. GNU
    extensions and other special records are scanned as bytes and then refused.
    """
    require(path.stat().st_size <= MAX_CONTAINER_BYTES, 'container exceeds scan limit', 'scan-limit')
    compressed = path.read_bytes()
    require(len(compressed) >= 18 and compressed[:3] == b'\x1f\x8b\x08' and (not compressed[3] & 224), 'invalid gzip header', 'invalid-artifact')
    flags = compressed[3]
    offset = 10
    if flags & 4:
        require(offset + 2 <= len(compressed) - 8, 'truncated gzip extra', 'archive-metadata')
        size = int.from_bytes(compressed[offset:offset + 2], 'little')
        require(size == 0, 'nonempty gzip extra', 'archive-metadata')
        offset += 2
    for flag in (8, 16):
        if flags & flag:
            require(offset < len(compressed) - 8 and compressed[offset] == 0, 'nonempty gzip text metadata', 'archive-metadata')
            offset += 1
    if flags & 2:
        offset += 2
    require(offset <= len(compressed) - 8, 'truncated gzip header', 'archive-metadata')
    decoder = zlib.decompressobj(wbits=31)
    data = decoder.decompress(compressed, MAX_CONTAINER_BYTES + 1)
    require(len(data) <= MAX_CONTAINER_BYTES and (not decoder.unconsumed_tail), 'container exceeds scan limit', 'scan-limit')
    require(decoder.eof and (not decoder.unused_data), 'trailing or concatenated gzip data', 'archive-metadata')
    require(len(data) % 512 == 0, 'unaligned tar container', 'archive-metadata')
    offset = total = count = 0
    while True:
        header = data[offset:offset + 512]
        require(len(header) == 512, 'truncated tar header', 'invalid-artifact')
        offset += 512
        if header == bytes(512):
            require(data[offset:offset + 512] == bytes(512) and (not any(data[offset:])), 'invalid tar terminal records', 'archive-metadata')
            break
        canonical_tar_header(header)
        member = tarfile.TarInfo.frombuf(header, encoding='utf-8', errors='strict')
        count += 1
        total += member.size
        with member_context(member.name):
            require(count <= MAX_MEMBERS and 0 <= member.size <= MAX_MEMBER_BYTES and (total <= MAX_ARCHIVE_BYTES), 'archive exceeds scan limit', 'scan-limit')
            raw = data[offset:offset + member.size]
            require(len(raw) == member.size, 'truncated tar body', 'invalid-artifact')
            if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
                scan_container_bytes(raw)
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.XHDTYPE, tarfile.XGLTYPE), 'unsupported tar member type', 'special-member')
            require(member.type != tarfile.DIRTYPE or member.size == 0, 'directory has a body', 'nonempty-directory')
            check_member_content(member.name, raw)
            for field in (member.uname, member.gname, member.linkname):
                scan_text(field)
            padded = (member.size + 511) // 512 * 512
            padding = data[offset + member.size:offset + padded]
            require(len(padding) == padded - member.size and (not any(padding)), 'nonempty tar padding', 'archive-metadata')
            offset += padded
    return tarfile.open(fileobj=io.BytesIO(data), mode='r:', encoding='utf-8', errors='strict')

def check_tar_contents(archive: tarfile.TarFile, prefixes: tuple[str, ...], *, npm_metadata: bool=False) -> None:
    nonempty: set[str] = set()
    total = 0
    for member in archive.getmembers():
        with member_context(member.name):
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), 'unsupported tar member type', 'special-member')
            check_member_name(member.name)
            require(member.type != tarfile.DIRTYPE or member.size == 0, 'directory has a body', 'nonempty-directory')
            if member.isdir():
                continue
            total += member.size
            require(0 <= member.size <= MAX_MEMBER_BYTES and total <= MAX_ARCHIVE_BYTES, 'member exceeds scan limit', 'scan-limit')
            stream = archive.extractfile(member)
            require(stream is not None, 'unreadable member', 'invalid-artifact')
            assert stream is not None
            with stream:
                raw = stream.read(MAX_MEMBER_BYTES + 1)
            require(len(raw) == member.size, 'truncated tar body', 'invalid-artifact')
            check_member_content(member.name, raw, npm_metadata=npm_metadata)
            if raw.strip():
                nonempty.add(member.name)
    check_notices(nonempty, prefixes)

def print_inventory(path: Path, names: list[str]) -> None:
    print(f'artifact contents {path.name}:')
    for name in sorted(names):
        print('  ' + json.dumps(name, ensure_ascii=True))

def tar_bytes(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    require(member.isfile() and member.size <= 1024 * 1024, 'invalid artifact metadata member')
    stream = archive.extractfile(member)
    require(stream is not None, 'missing artifact metadata member')
    assert stream is not None
    with stream:
        return stream.read()

def check_npm(path: Path, version: str) -> list[str]:
    with read_tar(path) as archive:
        names = archive.getnames()
        archive_names(names, {member.name for member in archive.getmembers() if member.isdir()})
        require('package/package.json' in names, 'npm artifact package.json is missing')
        package = json.loads(tar_bytes(archive, 'package/package.json'))
        require(package.get('name') == NPM_NAME, f'npm artifact package name must be {NPM_NAME}')
        require(package.get('version') == version, f'npm artifact version must equal tag {version}')
        require('package/dist/index.js' in names, 'npm artifact built entrypoint is missing')
        check_tar_contents(archive, ('package/',), npm_metadata=True)
        return names

def python_identity(raw: bytes, version: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(raw)
    names = metadata.get_all('Name', [])
    versions = metadata.get_all('Version', [])
    require(names == [PYTHON_NAME], f'Python artifact package name must be {PYTHON_NAME}')
    require(versions == [version], f'Python artifact version must equal tag {version}')
    if load_release_policy().SDK_SOURCE_PUBLIC:
        check_public_python_identity(raw, version)

def check_public_python_identity(raw: bytes, version: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(raw)
    require(not metadata.defects, 'artifact content has invalid Python metadata', 'invalid-metadata')
    names = metadata.get_all('Name', [])
    versions = metadata.get_all('Version', [])
    require(names == [PYTHON_NAME], f'Python artifact package name must be {PYTHON_NAME}')
    require(versions == [version], f'Python artifact version must equal tag {version}')
    require(metadata.get_all('Requires-Python', []) == ['>=3.10'], 'invalid Python version requirement', 'python-runtime-metadata')
    requirements = metadata.get_all('Requires-Dist', [])
    expected = {'httpx': {'>=0.28.1', '<0.29'}, 'httpcore': {'>=1.0.9', '<2'}}
    found = {}
    for value in requirements:
        match = re.fullmatch('([a-zA-Z0-9_-]+)\\s*(\\([^()]+\\)|[^()]+)', str(value).strip())
        require(match is not None, 'invalid Python dependency', 'python-runtime-metadata')
        name = match[1].lower().replace('_', '-')
        constraints = match[2].strip().removeprefix('(').removesuffix(')')
        parts = [part.strip() for part in constraints.split(',')]
        require(name not in found and len(parts) == len(set(parts)), 'duplicate Python dependency', 'python-runtime-metadata')
        found[name] = set(parts)
    require(found == expected and (not metadata.get_all('Provides-Extra', [])), 'Python dependencies differ from runtime contract', 'python-runtime-metadata')
    require(metadata.get_all('License-Expression', []) == ['Apache-2.0'], 'artifact content has invalid Python license expression', 'python-license-metadata')
    require(sorted(metadata.get_all('License-File', [])) == ['LICENSE', 'NOTICE'], 'artifact content has invalid Python license files', 'python-license-metadata')
    require(not metadata.get_all('License', []) and (not any((str(value).strip().startswith('License ::') for value in metadata.get_all('Classifier', [])))), 'artifact content has legacy Python license metadata', 'python-license-metadata')
    urls: dict[str, str] = {}
    for value in metadata.get_all('Project-URL', []):
        label, separator, url = str(value).partition(',')
        label, url = (label.strip().casefold(), url.strip())
        require(bool(separator and label and url) and label not in urls, 'artifact content has invalid Python project URLs', 'python-repository-metadata')
        urls[label] = url
    release_policy = load_release_policy()
    require(type(release_policy.SDK_SOURCE_PUBLIC) is bool, 'artifact content has invalid repository policy', 'repository-metadata')
    if release_policy.SDK_SOURCE_PUBLIC:
        repository = release_policy.public_repository().removesuffix('.git')
        require(urls.get('source') == repository and urls.get('issues') == repository + '/issues', 'artifact content has invalid Python source URLs', 'python-repository-metadata')
        canonical = {label.casefold(): url for label, url in public_project_urls(repository).items()}
        require(all((url == canonical[label] for label, url in urls.items() if label in canonical)), 'artifact content has invalid Python project URLs', 'python-repository-metadata')

def check_payload_inventory(kind: str, contents: dict[str, bytes], expected: dict[str, bytes], version: str, notices: dict[str, bytes]) -> None:
    """Require exact package bytes with only named build metadata additions."""
    require(kind in {'npm', 'wheel', 'sdist'}, 'unsupported payload kind', 'payload-inventory')
    require(re.fullmatch('(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)', version) is not None, 'invalid payload version', 'payload-inventory')
    require(isinstance(contents, dict) and isinstance(expected, dict) and bool(expected) and all((isinstance(name, str) and isinstance(raw, bytes) for name, raw in contents.items())) and all((isinstance(name, str) and isinstance(raw, bytes) and (not name.endswith('/')) for name, raw in expected.items())), 'invalid payload mapping', 'payload-inventory')
    require(isinstance(notices, dict) and set(notices) == {'LICENSE', 'NOTICE'} and all((isinstance(raw, bytes) and bool(raw.strip()) for raw in notices.values())), 'invalid payload notices', 'payload-notices')
    directories = {name for name in contents if name.endswith('/')}
    archive_names(list(contents), directories)
    archive_names(list(expected), set())
    regular = {name: raw for name, raw in contents.items() if name not in directories}
    require(all((regular.get(name) == raw for name, raw in expected.items())), 'packaged bytes differ from prepared input', 'payload-bytes')
    generated: set[str] = set()
    required: set[str] = set()
    metadata_names: set[str] = set()
    if kind == 'wheel':
        prefix = PYTHON_NAME.replace('-', '_') + '-' + version + '.dist-info/'
        generated = {prefix + name for name in ('METADATA', 'WHEEL', 'RECORD', 'top_level.txt')}
        generated.update((prefix + 'licenses/' + name for name in notices))
        required = generated - {prefix + 'top_level.txt'}
        metadata_names.add(prefix + 'METADATA')
        for name, raw in notices.items():
            require(regular.get(prefix + 'licenses/' + name) == raw, 'packaged license differs from prepared input', 'payload-notices')
    elif kind == 'sdist':
        prefix = PYTHON_NAME.replace('-', '_') + '-' + version + '/'
        generated = {prefix + 'PKG-INFO', prefix + 'setup.cfg'}
        metadata_prefix = prefix + 'src/' + PYTHON_NAME.replace('-', '_') + '.egg-info/'
        generated.update((metadata_prefix + name for name in ('PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'requires.txt', 'top_level.txt')))
        required.add(prefix + 'PKG-INFO')
        metadata_names.update((prefix + 'PKG-INFO', metadata_prefix + 'PKG-INFO'))
    require(set(regular) <= set(expected) | generated and required <= set(regular), 'unexpected or missing generated payload', 'payload-inventory')
    ancestors = {parent.as_posix() + '/' for name in regular for parent in PurePosixPath(name).parents if parent.as_posix() != '.'}
    require(directories <= ancestors and all((contents[name] == b'' for name in directories)), 'unexpected payload directory', 'payload-inventory')
    for name in metadata_names & set(regular):
        with member_context(name):
            python_identity(regular[name], version)

def python_import(names: list[str], prefix: str) -> None:
    require(f'{prefix}{IMPORT_NAME}/__init__.py' in names and (not any((DRAFT_IMPORT in PurePosixPath(name).parts for name in names))), f'artifact import package must be {IMPORT_NAME}')

def zip_records(data: bytes, archive: zipfile.ZipFile) -> list[bytes]:
    """Validate an exact, single-disk, non-ZIP64 stored/deflated wheel layout."""
    end = data.rfind(b'PK\x05\x06')
    require(end >= 0 and end + 22 <= len(data), 'missing ZIP terminal record', 'archive-metadata')
    terminal = struct.unpack_from('<4s4H2IH', data, end)
    _, disk, central_disk, disk_count, count, central_size, central_start, comment_size = terminal
    scan_container_bytes(data[end + 22:])
    require(disk == central_disk == 0 and disk_count == count == len(archive.infolist()) and (count <= MAX_MEMBERS) and (comment_size == 0) and (end + 22 == len(data)) and (central_start + central_size == end) and (archive.start_dir == central_start), 'noncanonical ZIP terminal record', 'archive-metadata')
    central = central_start
    local = total = 0
    bodies = []
    for member in archive.infolist():
        with member_context(member.orig_filename):
            require(central + 46 <= end and data[central:central + 4] == b'PK\x01\x02', 'invalid ZIP central record', 'archive-metadata')
            values = struct.unpack_from('<4s6H3I5H2I', data, central)
            _, _, version, flags, method, mtime, mdate, crc, packed_size, size, name_size, extra_size, file_comment_size, member_disk, _, _, local_start = values
            central_end = central + 46 + name_size + extra_size + file_comment_size
            require(central_end <= end, 'truncated ZIP central record', 'archive-metadata')
            scan_container_bytes(data[central + 46:central_end])
            require(extra_size == file_comment_size == member_disk == 0 and local_start == local and (version <= 20) and (flags & ~2062 == 0) and (method in (0, 8)) and (method == 8 or flags & 6 == 0), 'unsupported ZIP metadata', 'archive-metadata')
            name = data[central + 46:central + 46 + name_size]
            encoding = 'utf-8' if flags & 2048 else 'cp437'
            require(name.decode(encoding) == member.orig_filename == member.filename, 'noncanonical ZIP name', 'archive-metadata')
            require(local + 30 <= central_start and data[local:local + 4] == b'PK\x03\x04', 'invalid ZIP local record', 'archive-metadata')
            _, local_version, local_flags, local_method, local_time, local_date, local_crc, local_packed, local_size, local_name_size, local_extra_size = struct.unpack_from('<4s5H3I2H', data, local)
            body_start = local + 30 + local_name_size + local_extra_size
            body_end = body_start + packed_size
            require(body_start <= body_end <= central_start, 'truncated ZIP member', 'archive-metadata')
            scan_container_bytes(data[local + 30:body_start])
            require(local_extra_size == 0 and local_name_size == name_size and (data[local + 30:local + 30 + local_name_size] == name) and ((local_version, local_flags, local_method, local_time, local_date) == (version, flags, method, mtime, mdate)), 'ZIP local and central mismatch', 'archive-metadata')
            identity = (crc, packed_size, size)
            require((local_crc, local_packed, local_size) == identity or (flags & 8 and (local_crc, local_packed, local_size) == (0, 0, 0)), 'ZIP local size mismatch', 'archive-metadata')
            total += size
            require(size <= MAX_MEMBER_BYTES and total <= MAX_ARCHIVE_BYTES, 'archive exceeds scan limit', 'scan-limit')
            packed = data[body_start:body_end]
            if method == 0:
                raw = packed
            else:
                decoder = zlib.decompressobj(wbits=-15)
                raw = decoder.decompress(packed, MAX_MEMBER_BYTES + 1)
                require(decoder.eof and (not decoder.unused_data) and (not decoder.unconsumed_tail), 'unaccounted compressed ZIP data', 'archive-metadata')
            require(len(raw) == size and zlib.crc32(raw) == crc, 'ZIP content identity mismatch', 'archive-metadata')
            local = body_end
            if flags & 8:
                shift = 4 if data[local:local + 4] == b'PK\x07\x08' else 0
                require(local + shift + 12 <= central_start and struct.unpack_from('<3I', data, local + shift) == identity, 'invalid ZIP descriptor', 'archive-metadata')
                local += shift + 12
            bodies.append(raw)
            central = central_end
    require(local == central_start and central == end, 'unaccounted ZIP records', 'archive-metadata')
    return bodies

def check_wheel(path: Path, version: str) -> list[str]:
    require(path.stat().st_size <= MAX_CONTAINER_BYTES, 'container exceeds scan limit', 'scan-limit')
    data = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        bodies = zip_records(data, archive)
        names = archive.namelist()
        archive_names([member.orig_filename for member in members], {member.orig_filename for member in members if member.is_dir()})
        metadata = [name for name in names if len(PurePosixPath(name).parts) == 2 and name.endswith('.dist-info/METADATA')]
        require(len(metadata) == 1, 'wheel must contain one package metadata record')
        nonempty: set[str] = set()
        metadata_bytes = b''
        for member, raw in zip(members, bodies):
            with member_context(member.filename):
                filetype = stat.S_IFMT(member.external_attr >> 16)
                require(filetype in ({0, stat.S_IFDIR} if member.is_dir() else {0, stat.S_IFREG}) and (not member.flag_bits & 1) and (not member.external_attr & 16 or member.is_dir()), 'link, special or encrypted member', 'special-member')
                require(not member.is_dir() or member.file_size == 0, 'directory has a body', 'nonempty-directory')
                check_member_content(member.filename, raw)
                if member.filename == metadata[0]:
                    metadata_bytes = raw
                if raw.strip():
                    nonempty.add(member.filename)
        require(archive.getinfo(metadata[0]).file_size <= 1024 * 1024, 'invalid artifact metadata member')
        python_identity(metadata_bytes, version)
        python_import(names, '')
        require(f'{IMPORT_NAME}/py.typed' in names, 'wheel typing marker is missing')
        metadata_root = metadata[0].removesuffix('METADATA')
        check_notices(nonempty, (metadata_root + 'licenses/', metadata_root))
        return names

def check_sdist(path: Path, version: str) -> list[str]:
    with read_tar(path) as archive:
        names = archive.getnames()
        archive_names(names, {member.name for member in archive.getmembers() if member.isdir()})
        metadata = [name for name in names if len(PurePosixPath(name).parts) == 2 and name.endswith('/PKG-INFO')]
        require(len(metadata) == 1, 'sdist must contain one top-level package metadata record')
        python_identity(tar_bytes(archive, metadata[0]), version)
        python_import(names, f'{PurePosixPath(metadata[0]).parts[0]}/src/')
        check_tar_contents(archive, (f'{PurePosixPath(metadata[0]).parts[0]}/',))
        return names

def check_artifacts(directory: Path, tag: str, version: str, write_manifest: bool) -> None:
    require(directory.is_dir(), 'artifact directory is missing')
    files: list[Path] = []
    for parent in directory.iterdir():
        if parent.name == 'manifest.json':
            require(parent.is_file() and (not parent.is_symlink()), 'invalid artifact manifest')
            continue
        require(parent.name in {'npm', 'python'} and parent.is_dir() and (not parent.is_symlink()), 'expected exactly one npm tarball, one wheel and one sdist')
        for path in parent.iterdir():
            require(path.is_file() and (not path.is_symlink()), 'unexpected artifact directory or symlink')
            files.append(path)
    npm = [path for path in files if path.parent.name == 'npm' and path.name.endswith('.tgz')]
    wheels = [path for path in files if path.parent.name == 'python' and path.name.endswith('.whl')]
    sdists = [path for path in files if path.parent.name == 'python' and path.name.endswith('.tar.gz')]
    require(len(files) == 3 and len(npm) == len(wheels) == len(sdists) == 1, 'expected exactly one npm tarball, one wheel and one sdist')
    npm_stem = NPM_NAME.removeprefix('@').replace('/', '-')
    python_stem = PYTHON_NAME.replace('-', '_')
    require(npm[0].name == f'{npm_stem}-{version}.tgz' and wheels[0].name == f'{python_stem}-{version}-py3-none-any.whl' and (sdists[0].name == f'{python_stem}-{version}.tar.gz'), f'artifact filename must match tag {version}')
    inventories = [(npm[0], check_npm(npm[0], version)), (wheels[0], check_wheel(wheels[0], version)), (sdists[0], check_sdist(sdists[0], version))]
    manifest = {'schema': 1, 'tag': tag, 'npm': {'name': NPM_NAME, 'version': version}, 'python': {'name': PYTHON_NAME, 'version': version}, 'files': {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}}
    path = directory / 'manifest.json'
    if write_manifest:
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    else:
        require(path.is_file(), 'artifact manifest is missing')
        recorded = json.loads(path.read_text(encoding='utf-8'))
        require(recorded == manifest, 'artifact manifest differs from the tag, package identities or built bytes')
    for artifact, names in inventories:
        print_inventory(artifact, names)

class RedactedArgumentParser(argparse.ArgumentParser):

    def error(self, message: str) -> None:
        raise ReleaseError('invalid command arguments', 'arguments')

def main() -> int:
    parser = RedactedArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--ref', help='Require an exact refs/tags/<tag> reference')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--write-manifest', action='store_true')
    parser.add_argument('--check-contents', action='store_true', help='Explicit content gate; content checks always run with --artifacts')
    try:
        args = parser.parse_args()
        require(args.ref is None or args.ref == 'refs/tags/' + args.tag, 'release must use an exact tag reference', 'tag-reference')
        require(not args.write_manifest or args.artifacts is not None, '--write-manifest requires --artifacts')
        require(not args.check_contents or args.artifacts is not None, '--check-contents requires --artifacts')
        version = check_source(args.root, args.tag)
        if args.artifacts is not None:
            check_artifacts(args.artifacts, args.tag, version, args.write_manifest)
    except ReleaseError as error:
        print(error.diagnostic(), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, EOFError, tarfile.TarError, zipfile.BadZipFile, zlib.error):
        print(ReleaseError('invalid artifact', 'invalid-artifact').diagnostic(), file=sys.stderr)
        return 1
    print(f'SDK release {args.tag} verified')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
