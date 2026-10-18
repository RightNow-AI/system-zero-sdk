"""Generic credential checks that return identifiers without scanned values."""

from __future__ import annotations

import ast
from collections import Counter, OrderedDict
import html
import json
import math
from pathlib import PurePosixPath
import re
import string
from threading import RLock
from urllib.parse import unquote
import warnings


_IDENTIFIER = r"(?<![A-Za-z0-9_-])([A-Za-z_][A-Za-z0-9_-]*)(?![A-Za-z0-9_-])"
_DATA_NAMES = frozenset({"idempotencykey", "cachekey", "foreignkey", "sortkey", "partitionkey", "lookupkey"})
_CREDENTIAL_WORDS = frozenset({
    "secret", "token", "password", "passwd", "passphrase", "pwd", "pass", "credential", "credentials",
    "auth", "authorization", "bearer", "cookie", "session", "sid", "pin", "key",
    "cred", "creds", "pat", "jwt", "otp", "totp", "passcode", "signature", "sig",
})


def _data_name(value):
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())
    return compact in _DATA_NAMES or compact.endswith("s") and compact[:-1] in _DATA_NAMES


def _name_words(value):
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return tuple(word for word in re.split(r"[^a-z]+", separated.casefold()) if word)


def _credential_word(word):
    variants = (word, word[:-1] if word.endswith("s") else word,
                word[:-2] if word.endswith("es") else word,
                word[:-3] + "y" if word.endswith("ies") else word)
    return any(item in _CREDENTIAL_WORDS or item.endswith(("secret", "token", "password", "credential", "key"))
               for item in variants if item)


def _opaque_only_name(value):
    words = _name_words(value)
    return any(word in {"signature", "sig"} for word in words) and not any(
        _credential_word(word) for word in words if word not in {"signature", "sig"})


def credential_name(value: str) -> bool:
    """Recognize whole credential words within qualified identifier names."""
    if _data_name(value):
        return False
    return any(_credential_word(word) for word in _name_words(value))


def _entropy(value):
    return -sum(count / len(value) * math.log2(count / len(value)) for count in Counter(value).values()) if value else 0


_REFERENCE = re.compile(r"^[$%]|\$\{|\$\(|\{\{|<[^<>]*>|\[[^[\]]*\]|^\*+$|x{4}|\.{3}|^(?:null|undefined|none|nil|true|false)$", re.I)
_EXAMPLE = re.compile(r"example|changeme|change[-_]me|your[-_]|redacted|dummy|sample|fixme|todo|insert[-_]|replace[-_]|fake|not[-_]?a[-_]?(?:real|secret)", re.I)
_WORD = re.compile(r"^(?:[a-z]+|[A-Z]+|[A-Z]?[a-z]+(?:[A-Z][a-z]+)*)$")


def opaque_value(value, *, minimum=8, minimum_entropy=3):
    if not minimum <= len(value) <= 4096 or _REFERENCE.search(value) or _EXAMPLE.search(value):
        return False
    segments = [item for item in re.split(r"[-_.:/+@\s]+", value) if item]
    if segments and all(_WORD.fullmatch(item) or item.isdigit() or len(item) <= 3 for item in segments):
        return False
    if len(value) > 1 and sum(ord(value[index]) - ord(value[index - 1]) == 1 for index in range(1, len(value))) / (len(value) - 1) >= 0.5:
        return False
    return _entropy(value) >= (min(minimum_entropy, 2.5) if re.fullmatch(r"[0-9a-fA-F]+", value) else minimum_entropy)


_STRING = r'''(?<![A-Za-z0-9_$])(?:[rRbBuUfF]{1,2})?(?:"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*')'''
_STRINGS = re.compile(_STRING)
_ESCAPE = re.compile(r"\\(?:u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|([nrtbfv\\/'\"`$]))")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}


def _unescape(value):
    def replace(match):
        number = next((part for part in match.groups()[:3] if part is not None), None)
        if number is not None:
            point = int(number, 16)
            return chr(point) if point <= 0x10ffff else "\ufffd"
        return _ESCAPES.get(match[4], match[4])
    return _ESCAPE.sub(replace, value)


def _constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for item in node.values:
            if isinstance(item, ast.FormattedValue):
                if item.format_spec is not None or item.conversion not in (-1, 115):
                    return None
                item = item.value
            part = _constant(item)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    return None


def _string(value, *, fields=True):
    prefix = re.match(r"[rRbBuUfF]{0,2}", value)[0]
    body = value[len(prefix) + 1:-1]
    if not prefix:
        return _unescape(body)
    if "f" not in prefix.casefold():
        return body if "r" in prefix.casefold() else _unescape(body)
    if len(value) > 8192:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            node = ast.parse(value, mode="eval").body
            if not fields and isinstance(node, ast.JoinedStr) and any(isinstance(item, ast.FormattedValue) for item in node.values):
                return None
            return _constant(node)
    except (SyntaxError, ValueError, RecursionError):
        return None


_PLUS = re.compile("(" + _STRING + r")\s*\+\s*(" + _STRING + ")")
_ADJACENT = re.compile("(" + _STRING + r")[ \t\r\n]+(" + _STRING + ")")
_ARRAY_JOIN = re.compile(r"\[([^\[\]]{0,8192})\]\s*[.]\s*join\s*\(\s*(" + _STRING + r")\s*\)")
_SEPARATOR_JOIN = re.compile("(" + _STRING + r")\s*[.]\s*join\s*\(\s*([\[(])([^\[\]()]{0,8192})[\])]\s*\)")
_TEMPLATE = re.compile(r"`([^`\\]*(?:\\.[^`\\]*)*)`")
_PART = re.compile(r"\$\{\s*(" + _STRING + r")\s*\}")
_CONCAT = re.compile("(" + _STRING + r")\s*[.]\s*concat\s*\(([^()]{0,8192})\)")
_FORMAT = re.compile("(" + _STRING + r")\s*[.]\s*format\s*\(([^()]{0,8192})\)")
_PERCENT = re.compile("(" + _STRING + r")\s*%\s*(?:\(([^()]{0,8192})\)|(" + _STRING + "))")
_PARENTHESIZED = re.compile(r"\(\s*(" + _STRING + r")\s*\)")


def _sequence(value):
    strings = list(_STRINGS.finditer(value))
    cursor = 0
    parts = []
    for item in strings:
        gap = value[cursor:item.start()]
        if (not parts and gap.strip()) or (parts and re.fullmatch(r"\s*,\s*", gap) is None):
            return None
        part = _string(item[0])
        if part is None:
            return None
        parts.append(part)
        cursor = item.end()
    return parts if parts and re.fullmatch(r"\s*,?\s*", value[cursor:]) is not None else None


def _fold_literal_operations(value):
    """Fold only literal string operations; no scanned expression is executed."""
    def array_join(match):
        parts = _sequence(match[1])
        separator = _string(match[2])
        return json.dumps(separator.join(parts)) if parts is not None and separator is not None else match[0]

    def separator_join(match):
        parts = _sequence(match[3])
        separator = _string(match[1])
        return json.dumps(separator.join(parts)) if parts is not None and separator is not None else match[0]

    def template(match):
        body = match[1]
        parts = list(_PART.finditer(body))
        if not parts:
            return match[0]
        remainder = _PART.sub("", body)
        if "${" in remainder:
            return match[0]
        if any(_string(part[1]) is None for part in parts):
            return match[0]
        return json.dumps(_unescape(_PART.sub(lambda part: _string(part[1]), body)))

    def combine(match):
        left, right = _string(match[1]), _string(match[2])
        return json.dumps(left + right) if left is not None and right is not None else match[0]

    def prefixed(match):
        if match[0][0] in "\"'":
            return match[0]
        part = _string(match[0])
        return json.dumps(part) if part is not None else match[0]

    def concat(match):
        left, parts = _string(match[1]), _sequence(match[2])
        return json.dumps(left + "".join(parts)) if left is not None and parts is not None else match[0]

    def formatted(match):
        pattern, parts = _string(match[1]), _sequence(match[2])
        if pattern is None or parts is None:
            return match[0]
        output, automatic = [], 0
        try:
            for literal, field, spec, conversion in string.Formatter().parse(pattern):
                output.append(literal)
                if field is None:
                    continue
                if spec or conversion is not None or field and not field.isdigit():
                    return match[0]
                index = int(field) if field else automatic
                automatic += not field
                output.append(parts[index])
        except (ValueError, IndexError):
            return match[0]
        return json.dumps("".join(output))

    def percent(match):
        pattern = _string(match[1])
        parts = _sequence(match[2]) if match[2] is not None else [_string(match[3])]
        if pattern is None or parts is None or any(part is None for part in parts):
            return match[0]
        if re.search(r"%(?![%s])", pattern) or pattern.count("%s") != len(parts):
            return match[0]
        iterator = iter(parts)
        return json.dumps(re.sub(r"%[%s]", lambda item: "%" if item[0] == "%%" else next(iterator), pattern))

    def parenthesized(match):
        prefix = value[:match.start()].rstrip()
        if prefix and (prefix[-1].isalnum() or prefix[-1] in "_$.)]"):
            return match[0]
        part = _string(match[1])
        return json.dumps(part) if part is not None else match[0]

    for _ in range(12):
        previous = value
        value = _STRINGS.sub(prefixed, value)
        value = _ARRAY_JOIN.sub(array_join, value)
        value = _SEPARATOR_JOIN.sub(separator_join, value)
        value = _TEMPLATE.sub(template, value)
        value = _CONCAT.sub(concat, value)
        value = _FORMAT.sub(formatted, value)
        value = _PERCENT.sub(percent, value)
        value = _PLUS.sub(combine, value)
        value = _ADJACENT.sub(combine, value)
        value = _PARENTHESIZED.sub(parenthesized, value)
        if value == previous:
            break
    return value


_FOLD_CACHE_LIMIT = 64
_FOLD_TEXT_LIMIT = 128 * 1024
_FOLD_CACHE = OrderedDict()
_FOLD_LOCK = RLock()


def _fold_literals(value):
    """Memoize bounded pure normalization, never credential classifications."""
    if len(value) > _FOLD_TEXT_LIMIT:
        return _fold_literal_operations(value)
    with _FOLD_LOCK:
        result = _FOLD_CACHE.pop(value, None)
        if result is not None:
            _FOLD_CACHE[value] = result
            return result
    result = _fold_literal_operations(value)
    if len(result) <= _FOLD_TEXT_LIMIT:
        with _FOLD_LOCK:
            _FOLD_CACHE.pop(value, None)
            _FOLD_CACHE[value] = result
            while len(_FOLD_CACHE) > _FOLD_CACHE_LIMIT:
                _FOLD_CACHE.popitem(last=False)
    return result


def literal_views(value: str) -> tuple[str, ...]:
    """Return bounded decoded and constant-folded forms for content inspection."""
    forms = [value]
    for _ in range(8):
        decoded = unquote(html.unescape(_unescape(forms[-1])))
        if decoded == forms[-1]:
            break
        forms.append(decoded)
    for form in tuple(forms):
        folded = _fold_literals(form)
        if folded not in forms:
            forms.append(folded)
    return tuple(forms)


_ASSIGNMENT = r'''["'\]]{0,2}(?:[ \t]*:[ \t]*[A-Za-z_$][\w$.<>\[\]| ]{0,40}?)?[ \t]*(?:===?|!==?|:=|=>|\?\?=|\|\|=|=|:|,(?![ \t]*[A-Za-z_$][A-Za-z0-9_$]*[ \t]*=))[ \t]*'''
_VALUE = "(?:(" + _STRING + r''')|(`[^`\r\n]*`)|([^\s"'`#,;(){}[\]]+))'''
_ASSIGNED = re.compile(_IDENTIFIER + _ASSIGNMENT + _VALUE, re.I)
_DATA_ASSIGNED = re.compile("(?=" + _IDENTIFIER + _ASSIGNMENT + _VALUE + ")", re.I)
_SQL = re.compile(r"(?<![A-Za-z0-9_])(PASSWORD)[ \t]+" + _VALUE, re.I)
_FLAG = re.compile(r"(?<![A-Za-z0-9_-])--?([A-Za-z][A-Za-z0-9_-]*)(?:=|[ \t]+)" + _VALUE, re.I)
_SETTER = re.compile(r"\b(set[A-Za-z_][A-Za-z0-9_]*)[ \t]*\([ \t]*" + _VALUE, re.I)
_COLLECTION = re.compile(_IDENTIFIER + _ASSIGNMENT + r"([\[(])([^\[\]()]{0,8192})([\])])", re.I)
_XML = re.compile(r"<([A-Za-z][A-Za-z0-9_.:-]*)>[ \t]*([^<>]{1,4096}?)[ \t]*</\1>", re.I)
_FALLBACK = re.compile(_IDENTIFIER + r'''["'\]]{0,2}[ \t]*(?:=|:)[ \t]*[^\r\n;]{0,2048}?(?:\?\?|\|\||\bor\b)[ \t]*((''' + _STRING + "))", re.I)
_CONSERVATIVE = re.compile(_IDENTIFIER + r'''["'\]]{0,2}[ \t]*(?:=|:|=>)[ \t]*("(?:\\[\s\S]|[^"\\]){16,}"|'(?:\\[\s\S]|[^'\\]){16,}'|`(?:\\[\s\S]|[^`\\]){16,}`|[A-Za-z0-9_+./=-]{16,})''', re.I)
_HEADER = re.compile(r"\bAuthorization[ \t]*:[ \t]*[A-Za-z][A-Za-z0-9-]*[ \t]+([A-Za-z0-9_+./=-]{8,})", re.I)
_COMPACT_HEADER = re.compile(r"\bAuthorization\s*:\s*(?:Basic|Bearer)\s*([A-Za-z0-9_+./=-]{8,})", re.I)
_PREFIXED = re.compile(r"(?<![A-Za-z0-9_/-])(?:[A-Za-z][A-Za-z0-9]{1,20}[_-]){1,3}([A-Za-z0-9_+/-]{20,}={0,2})(?![A-Za-z0-9_/-])")
_DIGEST = re.compile(r"(?:sha(?:1|224|256|384|512)|md5)[-:]", re.I)
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_./+-])[A-Za-z0-9_+/-]{40,4096}={0,2}(?![A-Za-z0-9_./+=-])")
_HEX = re.compile(r"(?<![A-Za-z0-9_])[0-9a-fA-F]{32}(?![A-Za-z0-9_])")
_ACCESS_ID = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{19}(?![A-Za-z0-9_])")
_SEGMENTED = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z][A-Za-z0-9]{1,15}[.])?([A-Za-z0-9_-]{20,4096})[.]([A-Za-z0-9_-]{20,4096})(?![A-Za-z0-9_.-])")
_URL_PATH = re.compile(r'''\bhttps?:[/]{2}[^\s/?#"'`<>]+/(?:[^\s/?#"'`<>]+/)*([A-Za-z0-9_-]{20,4096})(?=$|[\s?#"'`<>])''', re.I)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}[.]eyJ[A-Za-z0-9_-]{8,}[.][A-Za-z0-9_-]{16,}")
_PEM = re.compile(r"-{5}BEGIN\s*(?:[A-Z0-9]+\s*)*PRIVATE\s*KEY(?:\s*BLOCK)?-{5}")
_KEY_FILE = re.compile(r"PuTTY-User-Key-File-[0-9]+:")
_URL = re.compile(r'''\b[a-z][a-z0-9+.-]{1,20}:[/]{2}[^\s:@/?#"'`<>]{1,128}:([^\s@/?#"'`<>]{1,256})@''', re.I)
_REFERENCE_MEMBER = r"(?:\s*[.]\s*#?[A-Za-z_$][A-Za-z0-9_$]*|\s*\[\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\])"
_REFERENCE_VALUE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*" + _REFERENCE_MEMBER + "+")
_TEMPLATE_REFERENCE = re.compile(r"\$\{\s*[A-Za-z_$][A-Za-z0-9_$]*" + _REFERENCE_MEMBER + r"*\s*\}")
_CALL_REFERENCE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*" + _REFERENCE_MEMBER + "*")


def _symbolic_label(name, value):
    """An enum-style label names itself without carrying a separate value."""
    if len(value) < 2 or value[0] not in "\"'" or value[-1] != value[0]:
        return False
    return (re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", name) is not None
            and value[1:-1].casefold() == name.casefold())


def _reference_template(value):
    if not value.startswith("`") or not value.endswith("`") or "\\" in value:
        return False
    body = value[1:-1]
    remainder, count = _TEMPLATE_REFERENCE.subn("", body)
    return count > 0 and "${" not in remainder and len(remainder) < 16


def _opaque_shape_ids(value):
    """Recognize opaque values without relying on provider names or prefixes."""
    found = set()
    digests = _data_digest_spans(value)

    def opaque(item, minimum):
        # Unlabelled hexadecimal text is also used for content and revision IDs.
        classes = sum(bool(re.search(pattern, item)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]"))
        return (classes >= 2 and not _source_words(item) and re.fullmatch(r"[0-9a-fA-F]+", item) is None
                and opaque_value(item, minimum=minimum))

    if any(not _inside(match.span(), digests) and not _DIGEST.match(match[0]) and opaque(match[0], 40)
           for match in _OPAQUE.finditer(value)):
        found.add("opaque-token")
    if any(opaque(match[1], 20) and opaque(match[2], 20) for match in _SEGMENTED.finditer(value)):
        found.add("segmented-token")
    if any(opaque(match[1], 20) for match in _URL_PATH.finditer(value)):
        found.add("url-token")
    return found


def _data_digest_spans(value):
    spans = set()
    for match in _DATA_ASSIGNED.finditer(value):
        group = next(index for index in range(2, len(match.groups()) + 1) if match[index] is not None)
        item = _literal_item(match[group])
        if item is None:
            continue
        digest_name = re.search(r"(?:^|_)(?:sha(?:1|224|256|384|512)|md5)(?:_|$)", match[1], re.I) is not None
        prefixed = _PREFIXED.fullmatch(item)
        if ((_data_name(match[1]) or digest_name) and re.fullmatch(r"[0-9a-fA-F]{32,128}", item)
                or _data_name(match[1]) and prefixed is not None and re.fullmatch(r"[0-9a-fA-F]{40,64}", prefixed[1])):
            spans.add(match.span(group))
    for pattern in (r"(?:sha(?:1|224|256|384|512)|md5)[-:][A-Za-z0-9_+/-]{20,}={0,2}",
                    r"\buses:[ \t]*[^\s@]+@[0-9a-fA-F]{32,64}\b",
                    r"https?:[/]{2}[^\s/]+/[^\s]+/(?:tree|commit|blob)/[0-9a-fA-F]{32,64}\b"):
        spans.update(match.span() for match in re.finditer(pattern, value, re.I))
    return spans


def _inside(span, spans):
    return any(start <= span[0] and span[1] <= end for start, end in spans)


def _literal_item(value):
    if _STRINGS.fullmatch(value):
        return _string(value, fields=False)
    if value.startswith("`") and value.endswith("`"):
        return _unescape(value[1:-1])
    return value


def _source_words(value):
    # Numeric word suffixes occur in source identifiers and platform names.
    words = re.split(r"[-_./+]+", value)
    return bool(words) and all(_WORD.fullmatch(word) or word.isdigit() or len(word) <= 3
                              or len(word) <= 8 and re.fullmatch(r"(?:[a-z]+|[A-Z]+)[0-9]+", word) for word in words)


def _prefixed_opaque(value):
    return not _source_words(value) and opaque_value(value, minimum=20)


def _computed_reference(value, item, end):
    if _REFERENCE_VALUE.fullmatch(item.removeprefix("!")):
        return True
    return _CALL_REFERENCE.fullmatch(item) is not None and value[end:].lstrip().startswith("(")


def _structural(value, compact=False):
    found = set()
    if _PEM.search(value) or _KEY_FILE.search(value):
        found.add("private-key")
    if _JWT.search(value):
        found.add("signed-token")
    data_digests = _data_digest_spans(value)
    if any(not _inside(match.span(), data_digests) for match in _HEX.finditer(value)):
        found.add("hex-credential")
    if any(_entropy(match[0]) >= 3 and not _EXAMPLE.search(match[0]) for match in _ACCESS_ID.finditer(value)):
        found.add("access-key-id")
    for match in _PREFIXED.finditer(value):
        # Compaction must not turn a command and its --flag into one token.
        if compact and (match.span() != (0, len(value)) or "--" in match[0]):
            continue
        if not _DIGEST.match(match[0]) and not _inside(match.span(), data_digests) and _prefixed_opaque(match[1]):
            found.add("credential-shape")
    header = _COMPACT_HEADER if compact else _HEADER
    if any(opaque_value(match[1]) for match in header.finditer(value)):
        found.add("credential-header")
    if any(opaque_value(unquote(match[1])) for match in _URL.finditer(value)):
        found.add("url-credentials")
    if not compact:
        found.update(_opaque_shape_ids(value))
    return found


def _find(value, conservative):
    found = _structural(value)

    def named(name, item, raw, *, literal=False):
        if not credential_name(name) or item is None:
            return
        if opaque_value(item):
            found.add("generic-secret-assignment")
        if (conservative and 8 <= len(item) < 16 and opaque_value(item, minimum_entropy=2.5)
                or conservative and literal and len(item) >= 16 and not _opaque_only_name(name)
                and not _reference_template(raw) and not _symbolic_label(name, raw)
                and not re.match(r"^[$%]|^\$\{|^\$\(|^\{\{", item)):
            found.add("archive-secret-assignment")

    for pattern in (_ASSIGNED, _SQL, _FLAG, _SETTER):
        for match in pattern.finditer(value):
            group = next(index for index in range(2, len(match.groups()) + 1) if match[index] is not None)
            raw = match[group]
            item = _literal_item(raw)
            if item is None:
                continue
            if group == len(match.groups()) and _computed_reference(value, item, match.end(group)):
                continue
            syntax = value[match.end(1):match.start(group)]
            named(match[1], item, raw, literal=group < len(match.groups()) and "," not in syntax)
    for match in _XML.finditer(value):
        named(match[1], match[2].strip(), match[2])
    for match in _COLLECTION.finditer(value):
        if (match[2], match[4]) not in (("[", "]"), ("(", ")")):
            continue
        parts = _sequence(match[3])
        if parts is not None:
            for part in parts:
                named(match[1], part, json.dumps(part))
    for match in _FALLBACK.finditer(value):
        named(match[1], _string(match[2], fields=False), match[2], literal=True)
    if conservative:
        for match in _CONSERVATIVE.finditer(value):
            if (credential_name(match[1]) and not _opaque_only_name(match[1]) and not _reference_template(match[2])
                    and not _symbolic_label(match[1], match[2])
                    and not re.match(r"^[$%]|^\$\{|^\$\(|^\{\{", _literal_item(match[2]) or "")
                    and not _computed_reference(value, match[2], match.end(2))):
                found.add("archive-secret-assignment")
    return found


def rule_ids(value: str, path: str = "") -> list[str]:
    """Inspect credential shapes and assignments; never report captured data."""
    return sorted({rule for form in literal_views(value) for rule in _find(form, False)})


def archive_rule_ids(value: str) -> list[str]:
    """Also refuse long literal values assigned to credential fields."""
    return sorted({rule for form in literal_views(value) for rule in _find(form, True)})


def compact_rule_ids(value: str) -> list[str]:
    """Inspect self-contained shapes after whitespace joins, without assignments."""
    found = set()
    for form in literal_views(value):
        found.update(_structural(form, True))
        if _STRINGS.fullmatch(form):
            literal = _string(form)
            if literal is not None:
                found.update(_structural(literal, True))
    return sorted(found)


def public_rule_ids(value: str, path: str = "", *, known_paths=()) -> list[str]:
    """Use the same credential checks for repository names and text bodies."""
    return archive_rule_ids(value)


def sensitive_path_rule_ids(path: str) -> list[str]:
    """Recognize credential files without opening their contents."""
    parts = PurePosixPath(path.replace("\\", "/").casefold()).parts
    for part in parts:
        if (((part.startswith(".env") or part.endswith(".env") or ".env." in part) and part != ".env.example")
                or part in {".npmrc", ".pypirc", ".netrc", ".git-credentials", ".docker", ".ssh", ".aws",
                            "credentials.json", "credentials", "kubeconfig", ".pgpass", ".htpasswd"}
                or re.fullmatch(r"id_(?:rsa|dsa|ecdsa|ed25519).*", part)
                or re.fullmatch(r"service[-_]account.*[.]json", part)
                or re.search(r"[.]tf(?:state|vars)(?:[.]|$)", part)
                or part.endswith((".pem", ".key", ".p8", ".p12", ".pfx", ".ppk", ".jks", ".keystore"))):
            return ["sensitive-path"]
    return []
