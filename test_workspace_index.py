import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_index import SourceIndex, OutputIndex


class WorkspaceIndexTests(unittest.TestCase):
    def test_1300_files_1000_completed_hot_path_has_no_resolve_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            files = [Path(folder) / f'{i:04}.pdf' for i in range(1300)]
            processed = {path.resolve() for path in files[:999]}
            index = SourceIndex(files, processed)
            with patch.object(Path, 'resolve', side_effect=AssertionError('Repeated filesystem resolve')):
                self.assertEqual(index.index(files[999]), 999)
                index.mark(files[999], processed)
                self.assertEqual(index.completed, 1000)
                self.assertEqual(index.next_pending(files[999], processed), files[1000])
                index.mark(files[999], processed)
                self.assertEqual(index.completed, 1000, 'Repeated copies must not increment completion')
                self.assertEqual(index.next_pending(files[1299], processed), files[1000])

    def test_refresh_recounts_added_deleted_and_completed_files(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b, c = [Path(folder) / f'{name}.pdf' for name in 'abc']
            processed = {a.resolve(), b.resolve()}
            initial = SourceIndex([a, b], processed)
            self.assertEqual(initial.completed, 2)
            refreshed = SourceIndex([b, c], processed)
            self.assertEqual(refreshed.completed, 1)
            self.assertIsNone(refreshed.index(a))
            self.assertEqual(refreshed.next_pending(b, processed), c)
            refreshed.mark(c, processed)
            self.assertIsNone(refreshed.next_pending(b, processed))

    def test_output_index_preserves_sorting_and_deduplicates(self):
        files = [Path('/output') / name for name in ('a.pdf', 'C.pdf')]
        index = OutputIndex(files)
        b = Path('/output/b.pdf')
        self.assertEqual(index.insert(b), (1, True))
        self.assertEqual(index.insert(b), (1, False))
        self.assertEqual(index.remove(files[0]), 0)
        self.assertEqual(index.index(b), 0)
        self.assertEqual(index.files, [b, files[1]])


if __name__ == '__main__':
    unittest.main()
