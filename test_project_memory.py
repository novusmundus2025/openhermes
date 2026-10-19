import tempfile
import unittest
from pathlib import Path
from project_memory import initialize, record


class ProjectMemoryTests(unittest.TestCase):
    def test_preserves_existing_instructions_and_records_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'AGENTS.md').write_text('Owner instructions')
            initialize(root)
            self.assertEqual((path / 'AGENTS.md').read_text(), 'Owner instructions')
            self.assertTrue((path / 'PLAN.md').exists())
            record(root, 'fixture', 124, [])
            text = (path / 'TASK-fixture.md').read_text()
            self.assertIn('Incomplete', text)
            self.assertIn('None recorded', text)

    def test_existing_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'owner.txt').write_text('keep')
            (path / 'PLAN.md').symlink_to(path / 'owner.txt')
            initialize(root)
            self.assertEqual((path / 'owner.txt').read_text(), 'keep')
