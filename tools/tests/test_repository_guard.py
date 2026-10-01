"""Failure-oriented checks for the public release guard, independent of pytest."""
import hashlib
import importlib.util
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
