"""Read-only checks for the tracked public release; no third-party dependencies.

These checks cover declared repository boundaries, not complete secret scanning,
GitHub settings, human review, or independent verification of the controller.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'research/mission_lab_v0_1'
LICENSE_SHA256 = 'ffcca38841adb694b6f380647e15f17c446a4d1656fed51a1e2041d064c94cc8'
MAX_FILE_BYTES = 1024 * 1024
FORBIDDEN_PARTS = {'evidence', '__pycache__', '.pytest_cache', 'node_modules', '.git'}
FORBIDDEN_SUFFIXES = {'.gguf', '.safetensors', '.pt', '.pth', '.ckpt', '.onnx', '.vhd', '.vhdx', '.pkl', '.pickle', '.pyc'}
REQUIRED = {'README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'LICENSE.md', 'NOTICE.md',
            'THIRD_PARTY_NOTICES.md', 'KNOWN_ISSUES.md', 'VV_PLAN.md', 'VALIDATION.md',
            'validation_summary.json', '.gitattributes', '.coderabbit.yaml',
            '.github/workflows/ci.yml', '.github/CODEOWNERS', 'requirements-dev.txt',
            PACKAGE + '/SOURCES.json', PACKAGE + '/LICENSE.md', PACKAGE + '/NOTICE.md'}
LINK = re.compile(r'\[[^\]\n]*\]\(([^)\n]+)\)')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def path_error(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        return 'unsafe tracked path'
    if any(part in FORBIDDEN_PARTS or part.startswith('.venv') for part in path.parts):
        return 'generated evidence, environment or cache must stay local'
    if path.name.startswith('.env') and path.name != '.env.example':
        return 'environment configuration must stay local'
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return 'model, checkpoint or machine-state artifact is outside release scope'
    if path.suffix.lower() == '.bin' and name not in {
        PACKAGE + '/fixtures/empty.bin', PACKAGE + '/fixtures/abc.bin', PACKAGE + '/fixtures/pattern.bin'
    }:
        return 'unregistered binary fixture'
    return None


def tracked(root: Path) -> dict[str, str]:
    raw = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root)
    result = {}
    for entry in raw.decode('utf-8').split('\0'):
        if not entry:
            continue
        metadata, name = entry.split('\t', 1)
        mode, _blob, stage = metadata.split()
        if stage != '0':
            raise ValueError('Unmerged index entry: ' + name)
        result[name] = mode
    return result


def note_errors(root: Path, manifest: dict) -> list[str]:
    errors = []
    for source in manifest['sources']:
        relative = source['archived_path']
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative:
            errors.append('Unsafe provenance note: ' + relative)
            continue
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            errors.append('Linked provenance note: ' + relative)
        elif not target.is_file():
            errors.append('Missing provenance note: ' + relative)
        elif hashlib.sha256(target.read_bytes()).hexdigest() != source['archived_content_sha256']:
            errors.append('Changed provenance note: ' + relative)
    return errors


def link_errors(root: Path, name: str, text: str, names: set[str]) -> list[str]:
    errors = []
    for raw in LINK.findall(text):
        value = raw.strip().split(' "', 1)[0].strip('<>')
        target = urlsplit(value)
        if target.scheme or target.netloc or not target.path:
            continue
        path = (root / name).parent / unquote(target.path)
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            errors.append(name + ': local link escapes repository: ' + value)
        elif resolved.is_file() and resolved.relative_to(root.resolve()).as_posix() not in names:
            errors.append(name + ': link targets an untracked file: ' + value)
        elif not resolved.exists():
            errors.append(name + ': broken local link: ' + value)
    return errors


def check(root: Path) -> dict:
    entries = tracked(root)
    names = set(entries)
    errors = ['Missing required file: ' + name for name in sorted(REQUIRED - names)]
    for name, mode in entries.items():
        problem = path_error(name)
        if problem:
            errors.append(name + ': ' + problem)
            continue
        path = root / name
        if mode != '100644' and mode != '100755':
            errors.append(name + ': links and submodules are not supported in this release')
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            errors.append(name + ': linked or escaped file')
            continue
        if not path.is_file():
            errors.append(name + ': tracked file is missing')
            continue
        content = path.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            errors.append(name + ': exceeds the 1 MiB public-file limit')
        if name.endswith('.bin'):
            continue
        try:
            text = content.decode('utf-8')
        except UnicodeError:
            errors.append(name + ': non-UTF-8 public text')
            continue
        if PRIVATE_KEY.search(text):
            errors.append(name + ': private-key marker found')
        if name.endswith('.md'):
            errors.extend(link_errors(root, name, text, names))
    for name in ('LICENSE.md', PACKAGE + '/LICENSE.md'):
        if (root / name).is_file() and hashlib.sha256((root / name).read_bytes()).hexdigest() != LICENSE_SHA256:
            errors.append(name + ': standard license text changed')
    if (root / 'NOTICE.md').is_file() and (root / PACKAGE / 'NOTICE.md').is_file():
        if (root / 'NOTICE.md').read_bytes() != (root / PACKAGE / 'NOTICE.md').read_bytes():
            errors.append('Root and package required notices differ')
    try:
        source_notes = json.loads((root / PACKAGE / 'SOURCES.json').read_text(encoding='utf-8'))
        errors.extend(note_errors(root / PACKAGE, source_notes))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append('Invalid source-note manifest: ' + str(exc))
    return {'passed': not errors, 'tracked_files': len(entries), 'errors': errors,
            'scope': 'tracked public files, standard license bytes, local document links and source-note hashes',
            'human_review': False, 'independent_vv': False}


if __name__ == '__main__':
    try:
        result = check(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        result = {'passed': False, 'errors': [str(exc)]}
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.exit(0 if result['passed'] else 1)
