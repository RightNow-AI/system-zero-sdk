#!/usr/bin/env python3
"""Verify the reviewed workflow bytes and essential publication invariants.

Workflow changes require a matching reviewed digest change. This intentionally
refuses alternate YAML syntax instead of implementing a partial YAML parser.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_SHA256 = {
    "continuous": {
        "ci.yml": "4742b01d2e879cf5d5e7c206a66313155dbc5a652ff6dd7a9cb4b946777d7e89",
        "sdk-release.yml": "235622f9d48815708d21c52b6ed603b936ef83b0d2613a8483fe0a4c75d1bbf8"
    },
    "manual": {
        "ci.yml": "a8fa638d31935647416e1f426aa73e75f913da6a888b7db6af9f0e964ea3fb5f",
        "sdk-release.yml": "0aa2a878aa8131d7239d20f609fd757438d4b20a2bc8d90ffb6088827b600b40"
    }
}
ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/setup-python": "e797f83bcb11b83ae66e0230d6156d7c80228e7c",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "pypa/gh-action-pypi-publish": "ed0c53931b1dc9bd32cbe73a98c7f6766f8a527e",
}


def require(condition, label):
    if not condition:
        raise ValueError(label)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools/sdk" / (name + ".py"))
    require(spec is not None and spec.loader is not None, 'Workflow invariant failed')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def job_text(workflow, name):
    job = workflow.split("\n  " + name + ":\n", 1)[1]
    return re.split(r"\n  [a-z_]+:\n", job, maxsplit=1)[0]


def job_steps(workflow, name):
    body = job_text(workflow, name)
    entries = re.findall(r"^      - (.+)$", body, re.MULTILINE)
    require(entries and all((entry.startswith('name: ') for entry in entries)), 'Workflow invariant failed')
    require(len(re.findall('^    steps:$', body, re.MULTILINE)) == 1, 'Workflow invariant failed')
    return re.split(r"^      - name: ", body, flags=re.MULTILINE)[1:]


def step_run(step):
    require(len(re.findall('^\\s+run:', step, re.MULTILINE)) <= 1, 'Workflow invariant failed')
    match = re.search(r"^        run: ([^\n]+)(?:\n|$)", step, re.MULTILINE)
    if match is None:
        return None
    if match[1].startswith("'") and match[1].endswith("'"):
        return match[1][1:-1].replace("''", "'")
    if match[1] != "|":
        require(not match[1].startswith(('|', '>', '&', '*', '!', '"')), 'Workflow invariant failed')
        require(not any((line.startswith('          ') for line in step[match.end():].splitlines())), 'Workflow invariant failed')
        return match[1]
    lines = step[match.end():].splitlines()
    require(all((not line.strip() or line.startswith('          ') for line in lines)), 'Workflow invariant failed')
    return "\n".join(line[10:] for line in lines).strip()


def check_source_content_steps(workflow, name):
    steps = job_steps(workflow, name)
    runs = [step_run(step) or "" for step in steps]
    commands = ["python -I -B -X utf8 tools/sdk/scan_tree.py --root .",
                "python -I -B -X utf8 tests/content_rules.py"]
    valid = all(runs.count(command) == 1 for command in commands)
    if valid:
        positions = [runs.index(command) for command in commands]
        valid = all(re.search(r"^        (?:if|continue-on-error|shell):", steps[index], re.MULTILINE) is None
                    for index in positions)
        if name == "build":
            preparation = [i for i, run in enumerate(runs) if "tools/sdk/prepare_packages.py" in run]
            valid = valid and len(preparation) == 1 and all(index < preparation[0] for index in positions)
    require(valid, "Release source content gates must run before preparation" if name == "build"
            else "CI source content gates must be mandatory")


def check_archive_steps(release):
    source = 'python -I -B -X utf8 tools/sdk/check_release.py --root . --tag "$GITHUB_REF_NAME" --ref "$GITHUB_REF"'
    publication = source + ' --artifacts sdk-release-dist --check-contents'
    binding = 'python -I -B -X utf8 tools/sdk/verify_manifest.py --manifest sdk-release-dist/manifest.json'
    build = job_steps(release, "build")
    runs = [step_run(step) or "" for step in build]
    archive = [i for i, run in enumerate(runs) if '--check-contents --write-manifest' in run]
    payload = [i for i, run in enumerate(runs) if 'tools/sdk/check_package_payloads.py' in run]
    output = [i for i, run in enumerate(runs) if 'tools/sdk/verify_manifest.py' in run]
    upload = [i for i, step in enumerate(build) if 'uses: actions/upload-artifact@' in step]
    require(len(archive) == len(payload) == len(output) == len(upload) == 1, 'Workflow invariant failed')
    require(archive[0] < payload[0] < output[0] < upload[0], 'Build verification must precede artifact upload')
    require('-m build --no-isolation' in job_text(release, 'build'), 'Workflow invariant failed')
    require(not any(('pytest' in run or 'vitest' in run for run in runs)), 'Workflow invariant failed')
    for name in ("publish_npm", "publish_pypi"):
        steps = job_steps(release, name)
        runs = [step_run(step) for step in steps]
        require(runs.count(binding) == runs.count(publication) == 1, 'Artifact binding and content checks must each run exactly once')
        publishers = [i for i, step in enumerate(steps) if (runs[i] or '').startswith('npm publish ') or 'uses: pypa/gh-action-pypi-publish@' in step]
        require(len(publishers) == 1, 'Workflow invariant failed')
        index = publishers[0]
        require(runs[index - 2:index] == [binding, publication], 'Artifact checks must immediately precede publication')
        for step in steps[index - 2:index + 1]:
            require(not re.search('^        (?:if|continue-on-error|shell):', step, re.MULTILINE), 'Workflow invariant failed')


def check():
    policy = load("release_policy")
    ci_mode = getattr(policy, "PUBLIC_CI_MODE", None)
    require(ci_mode in WORKFLOW_SHA256, 'Workflow invariant failed')
    directory = ROOT / ".github/workflows"
    require({path.name for path in directory.iterdir()} == {'ci.yml', 'sdk-release.yml'}, 'Workflow invariant failed')
    workflows = {}
    for name, digest in WORKFLOW_SHA256[ci_mode].items():
        raw = (directory / name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == digest, 'Workflow differs from reviewed policy bytes')
        text = raw.decode("ascii")
        workflows[name] = text
        trigger = text.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
        expected = ("  workflow_dispatch:\n" if ci_mode == "manual" else
                    "  pull_request:\n  push:\n    branches: [main]\n" if name == "ci.yml" else
                    "  push:\n    tags: ['sdk-v*']\n")
        require(trigger == expected, 'Workflow invariant failed')
        require(text.split('\npermissions:\n', 1)[1].split('\nconcurrency:\n', 1)[0] == '  contents: read\n', 'Workflow invariant failed')
        jobs = text.split("\njobs:\n", 1)[1]
        names = re.findall(r"^  ([a-z_]+):$", jobs, re.MULTILINE)
        require(len(names) == len(set(names)), 'Workflow invariant failed')
        require(set(names) == ({'typescript', 'python', 'policy'} if name == 'ci.yml' else {'build', 'typescript', 'python', 'publication_gate', 'publish_npm', 'publish_pypi'}), 'Workflow invariant failed')
        runners = re.findall(r"^    runs-on: (.+)$", jobs, re.MULTILINE)
        require(len(runners) == len(names) and set(runners) == {'ubuntu-24.04'}, 'Workflow invariant failed')
        require(len(re.findall('^    timeout-minutes: [1-9][0-9]*$', jobs, re.MULTILINE)) == len(names), 'Workflow invariant failed')
        require('\npermissions:\n  contents: read\n' in text, 'Workflow invariant failed')
        require(not re.search('(?:container|services|continue-on-error):|secrets\\s*(?:[.]|\\[)|github[.]token', text), 'Workflow invariant failed')
        uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.MULTILINE)
        for action in uses:
            action_name, revision = action.rsplit("@", 1)
            require(ACTIONS.get(action_name) == revision, 'Workflow invariant failed')
        require(text.count('persist-credentials: false') == sum((action.startswith('actions/checkout@') for action in uses)), 'Workflow invariant failed')
        require('node: [22, 24]' in text and "python: ['3.10', '3.11', '3.12', '3.13', '3.14']" in text, 'Workflow invariant failed')
        require('npm ci --prefix typescript --ignore-scripts --no-audit --no-fund' in text, 'Workflow invariant failed')
        require('--require-hashes --only-binary :all:' in text, 'Workflow invariant failed')
        require("PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1'" in text, 'Workflow invariant failed')
        for name in names:
            body = job_text(text, name)
            permissions = re.findall(r"^    permissions:\n((?:      [^\n]+\n)+)", body, re.MULTILINE)
            require(permissions == (['      contents: read\n      id-token: write\n'] if name in {'publish_npm', 'publish_pypi'} else []), 'Workflow invariant failed')
            require(len(re.findall('^\\s*permissions:', body, re.MULTILINE)) == len(permissions), 'Workflow invariant failed')
            for step in job_steps(text, name):
                run = step_run(step) or ''
                require('${{' not in run, 'Workflow invariant failed')
                for command in re.finditer(r'(?m)^[ \t]*(?:python|"\$SDK_RELEASE_TEMP/(?:venv|runtime)/bin/python")(?=\s)', run):
                    require(run[command.end():].startswith(' -I -B -X utf8 '),
                            'Python commands must isolate imports, disable bytecode and select UTF-8')
    ci = workflows['ci.yml']
    require('cancel-in-progress: true' in ci and 'id-token' + chr(58) not in ci, 'Workflow invariant failed')
    require('python -I -B -X utf8 tests/release_artifacts.py' in ci and 'python -I -B -X utf8 tools/sdk/scan_tree.py --root .' in ci, 'Workflow invariant failed')
    check_source_content_steps(ci, "policy")
    release = workflows['sdk-release.yml']
    require('\n  group: sdk-release\n  cancel-in-progress: false\n' in release, 'Workflow invariant failed')
    for name in ('build', 'publication_gate'):
        require("    if: ${{ github.ref_type == 'tag' }}\n" in job_text(release, name), 'Workflow invariant failed')
    for name in ('publish_npm', 'publish_pypi'):
        job = job_text(release, name)
        require('needs: [build, typescript, python, publication_gate]' in job, 'Workflow invariant failed')
        require("if: ${{ github.ref_type == 'tag' && needs.build.outputs.source_public == 'true' }}" in job, 'Workflow invariant failed')
        require('SDK_MANIFEST_SHA256: ${{ needs.build.outputs.manifest_sha256 }}' in job, 'Workflow invariant failed')
        require('environment: sdk-release-' + ('npm' if name == 'publish_npm' else 'pypi') + '\n' in job, 'Workflow invariant failed')
    require(release.count('id-token: write') == 2, 'Workflow invariant failed')
    require("github.event.repository.visibility != 'public'" in release, 'Workflow invariant failed')
    require('--require-public --repository "$GITHUB_REPOSITORY"' in release, 'Workflow invariant failed')
    require('git merge-base --is-ancestor "$GITHUB_SHA" FETCH_HEAD' in release, 'Workflow invariant failed')
    require('manifest_sha256: ${{ steps.artifact_manifest.outputs.manifest_sha256 }}' in release, 'Workflow invariant failed')
    check_source_content_steps(release, "build")
    check_archive_steps(release)
    require(policy.SDK_SOURCE_PUBLIC is True, 'Workflow invariant failed')
    package = json.loads((ROOT / 'typescript/package.json').read_text(encoding='utf-8'))
    policy.check_npm_repository(package)
    policy.require_public(policy.public_repository().removeprefix('https://github.com/').removesuffix('.git'))
    load('check_release').check_source(ROOT, 'sdk-v' + package['version'])
    print('Hosted workflow and public SDK release policy passed.')


if __name__ == '__main__':
    check()
