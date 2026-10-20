import tempfile
import unittest
from pathlib import Path
from project_memory import initialize, record


class ProjectMemoryTests(unittest.TestCase):
    def test_execution_worker_has_layout_policy_in_its_scope(self):
        import symtable
        source=Path(__file__).with_name('projects.py').read_text(encoding='utf-8')
        table=symtable.symtable(source,'projects.py','exec')
        def inspect(scope):
            for child in scope.get_children():
                symbols={symbol.get_name():symbol for symbol in child.get_symbols()}
                policy=symbols.get('PROJECT_STRUCTURE_GUIDANCE')
                if policy and policy.is_referenced():
                    self.assertTrue(policy.is_imported(), child.get_name())
                inspect(child)
        inspect(table)

    def test_new_project_records_ecosystem_layout_policy(self):
        with tempfile.TemporaryDirectory() as root:
            initialize(root)
            text=(Path(root)/'AGENTS.md').read_text()
            self.assertIn('Follow the user-specified layout', text)
            self.assertIn('there is no universal folder tree', text)
            self.assertIn('For existing projects, preserve the established layout', text)
            self.assertIn('small script', text)

    def test_preserves_existing_instructions_and_records_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'AGENTS.md').write_text('Owner instructions')
            initialize(root)
            self.assertEqual((path / 'AGENTS.md').read_text(), 'Owner instructions')
            self.assertTrue((path / '.hermes-project/PLAN.md').exists())
            record(root, 'fixture', 124, [])
            text = (path / '.hermes-project/tasks/TASK-fixture.md').read_text()
            self.assertIn('Incomplete', text)
            self.assertIn('None recorded', text)

    def test_existing_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'owner.txt').write_text('keep')
            (path / '.hermes-project').mkdir()
            (path / '.hermes-project/PLAN.md').symlink_to(path / 'owner.txt')
            initialize(root)
            self.assertEqual((path / 'owner.txt').read_text(), 'keep')

    def test_migrates_root_records_without_overwriting_conflicts(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            (path/'PLAN.md').write_text('existing plan')
            task='TASK-'+('a'*32)+'.md'
            (path/task).write_text('old outcome')
            initialize(root)
            self.assertFalse((path/'PLAN.md').exists())
            self.assertEqual((path/'.hermes-project/PLAN.md').read_text(),'existing plan')
            self.assertEqual((path/'.hermes-project/tasks'/task).read_text(),'old outcome')
            (path/'PLAN.md').write_text('different root plan')
            initialize(root)
            self.assertEqual((path/'PLAN.md').read_text(),'different root plan')
            self.assertEqual((path/'.hermes-project/PLAN.md').read_text(),'existing plan')

    def test_symlinked_memory_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(root)/'.hermes-project').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(OSError):
                initialize(root)
            self.assertEqual(list(Path(outside).iterdir()),[])
