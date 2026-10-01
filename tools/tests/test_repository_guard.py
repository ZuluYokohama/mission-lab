"""Failure-oriented checks for the public release guard, independent of pytest."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('repository_guard', Path(__file__).resolve().parents[1] / 'check_repository.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicBoundaryTests(unittest.TestCase):
    def test_private_outputs_and_checkpoint_paths_are_rejected(self):
        for name in ('research/mission_lab_v0_1/evidence/run/raw.json', '.venv/pyvenv.cfg',
                     '.env.local', 'models/weights.gguf', 'backup/distro.vhdx',
                     '../outside.py', 'assets/unknown.bin'):
            with self.subTest(path=name):
                self.assertIsNotNone(guard.path_error(name))

    def test_only_registered_small_binary_paths_are_allowed(self):
        self.assertIsNone(guard.path_error('research/mission_lab_v0_1/fixtures/pattern.bin'))
        self.assertIsNone(guard.path_error('.env.example'))

    def test_changed_missing_and_escaped_provenance_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'note.txt').write_bytes(b'original')
            manifest = {'sources': [{'archived_path': 'note.txt', 'archived_content_sha256': hashlib.sha256(b'original').hexdigest()}]}
            self.assertEqual(guard.note_errors(root, manifest), [])
            (root / 'note.txt').write_bytes(b'altered')
            self.assertTrue(guard.note_errors(root, manifest))
            manifest['sources'][0]['archived_path'] = 'missing.txt'
            self.assertTrue(guard.note_errors(root, manifest))
            manifest['sources'][0]['archived_path'] = '../outside.txt'
            self.assertTrue(guard.note_errors(root, manifest))

    def test_links_cannot_hide_missing_untracked_or_external_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'untracked.md').write_text('private')
            text = '[missing](missing.md) [private](untracked.md) [escape](../outside.md)'
            self.assertEqual(len(guard.link_errors(root, 'README.md', text, {'README.md'})), 3)
            self.assertEqual(guard.link_errors(root, 'README.md', '[web](https://example.com) [section](#scope)', {'README.md'}), [])

    def test_inline_titles_preserve_existing_and_missing_destinations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('guide', encoding='utf-8')
            for title in ('"Guide"', "'Guide'", '(Guide)'):
                for target in ('README.md', '<README.md>'):
                    with self.subTest(title=title, target=target):
                        self.assertEqual(guard.link_errors(root, 'index.md',
                            f'[guide]({target} {title})', {'README.md', 'index.md'}), [])
                        errors = guard.link_errors(root, 'index.md',
                            f'[missing](missing.md {title})', {'README.md', 'index.md'})
                        self.assertEqual(errors, ['index.md: broken local link: missing.md'])

    def test_balanced_destinations_escaped_titles_and_angle_spaces(self):
        text = r'''[a](guide(one).md "A") [b](<guide two.md> 'B') [c](guide.md "A \"quote\"") [d](guide.md 'A \'quote\'') [e](guide.md (A \) bracket))'''
        self.assertEqual(list(guard.inline_destinations(text)),
                         ['guide(one).md', 'guide two.md', 'guide.md', 'guide.md', 'guide.md'])
        self.assertEqual(list(guard.inline_destinations(r'\[ignored](missing.md) [a](guide\(one\).md)')),
                         ['guide(one).md'])

    def test_malformed_or_multiline_candidates_do_not_become_destinations(self):
        for text in ('[a](missing.md "unterminated)', "[a](missing.md 'unterminated)",
                     '[a](missing.md (unterminated)', '[a](<missing.md)',
                     '[a](missing(one.md)', '[a](missing.md "title" trailing)',
                     '[a](missing.md\n"title")', '[a][reference]'):
            with self.subTest(text=text):
                self.assertEqual(list(guard.inline_destinations(text)), [])
        self.assertEqual(list(guard.inline_destinations('[bad](missing.md "broken\n[good](README.md)')),
                         ['README.md'])

    def test_path_checks_still_reject_titled_untracked_and_escaping_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'private.md').write_text('private', encoding='utf-8')
            text = "[private](private.md 'Title') [escape](../outside.md (Title))"
            self.assertEqual(guard.link_errors(root, 'README.md', text, {'README.md'}), [
                'README.md: link targets an untracked file: private.md',
                'README.md: local link escapes repository: ../outside.md'])

    def test_maximum_size_malformed_links_finish_under_external_timeout(self):
        # An external timeout bounds regressions even if regex backtracking
        # holds the interpreter. Each fixture stays within the 1 MiB limit.
        script = '''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('guard', sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
limit = guard.MAX_FILE_BYTES
payloads = ('[' * limit, '[x](' + '(' * (limit - 4),
            '[x](guide.md "' + '[' * (limit - 14))
for payload in payloads:
    assert len(payload) <= limit
    assert list(guard.inline_destinations(payload)) == []
print('bounded')
'''
        completed = subprocess.run([sys.executable, '-B', '-c', script, str(guard.__file__)],
                                   capture_output=True, text=True, timeout=20, check=True)
        self.assertEqual(completed.stdout.strip(), 'bounded')


if __name__ == '__main__':
    unittest.main()
