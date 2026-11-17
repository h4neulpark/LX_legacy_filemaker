import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

import app
from pdf_pipeline import file_identity
from survey_batch import BatchItem, copy_batch, batch_context, file_digest
from survey_ocr import normalize_survey_lot


class SurveyBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.source = self.folder / 'source.pdf'
        self.source.write_bytes(b'original complete PDF bytes')
        self.output = self.folder / 'output'
        self.output.mkdir()
        self.identity = file_identity(self.source)
        self.context = batch_context(self.identity, '4111513900', '2025', self.output)
        self.events = queue.Queue()

    def items(self, lots=('301-6', '301-37', '301-45')):
        items = []
        for lot in lots:
            pnu = app.make_pnu('4111513900', app.parse_lot_number(lot))
            address = f'경기도 수원시 팔달구 지동 {lot}'
            plan = app.build_destination_plan(self.source, self.output, '2025', '측량성과도', pnu, address)
            stem = f'2025_측량성과도_{pnu}_{app.sanitize_filename_part(address)}'
            items.append(BatchItem(f'{self.context}:{lot}', lot, stem, plan))
        return items

    def checkpoint(self, ledger):
        app.save_work_state(self.folder, {'survey_progress': ledger})

    def run_batch(self, items=None, ledger=None, copier=None, checkpoint=None):
        return copy_batch(self.source, self.identity, tuple(items or self.items()), ledger or {},
                          checkpoint or self.checkpoint, copier or app.copy_with_destination_plan,
                          app.destination_plan_for_stem, app.DestinationPlan, self.events)

    def test_three_lots_copy_complete_original_with_selected_legal_code(self):
        result = self.run_batch()
        self.assertTrue(result.complete, result.error)
        self.assertEqual(len(list(self.output.glob('*.pdf'))), 3)
        for outcome in result.outcomes:
            self.assertEqual(outcome.plan.destination.read_bytes(), self.source.read_bytes())
            self.assertIn('4111513900', outcome.plan.destination.name)
        self.assertEqual(len(app.load_work_state(self.folder)['survey_progress']), 3)
        self.assertEqual(list(self.output.glob('*.tmp')), [])

    def test_partial_failure_retry_skips_successes_without_creating_duplicates(self):
        def fail_second(source, plan):
            if '3010037_' in plan.destination.name:
                raise OSError('disk test error')
            app.copy_with_destination_plan(source, plan)

        result = self.run_batch(copier=fail_second)
        self.assertFalse(result.complete)
        self.assertEqual([o.status for o in result.outcomes], ['copied', 'failed', 'copied'])
        ledger = app.load_work_state(self.folder)['survey_progress']
        retried = self.run_batch(ledger=ledger)
        self.assertTrue(retried.complete, retried.error)
        self.assertEqual([o.status for o in retried.outcomes], ['skipped', 'copied', 'skipped'])
        self.assertEqual(len(list(self.output.glob('*.pdf'))), 3)

    def test_checkpoint_failure_before_copy_stops_without_writing_output(self):
        def fail(_ledger):
            raise OSError('cannot save state')

        result = self.run_batch(checkpoint=fail)
        self.assertFalse(result.complete)
        self.assertIn('cannot save', result.error)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_crash_after_copy_is_recovered_from_pending_intent(self):
        saves = 0

        def crash_after_copy(ledger):
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError()  # Even an exception without a message is a failure.
            self.checkpoint(ledger)

        result = self.run_batch(checkpoint=crash_after_copy)
        self.assertFalse(result.complete)
        self.assertEqual(len(list(self.output.glob('*.pdf'))), 1)
        saved = app.load_work_state(self.folder)['survey_progress']
        self.assertEqual(next(iter(saved.values()))['status'], 'pending')
        retried = self.run_batch(ledger=saved)
        self.assertTrue(retried.complete)
        self.assertEqual(retried.outcomes[0].status, 'skipped')
        self.assertEqual(len(list(self.output.glob('*.pdf'))), 3)

    def test_modified_previous_output_is_not_silently_skipped(self):
        result = self.run_batch()
        result.outcomes[0].plan.destination.write_bytes(b'changed by another app')
        retried = self.run_batch(ledger=result.ledger)
        self.assertTrue(retried.complete, retried.error)
        self.assertEqual(retried.outcomes[0].status, 'copied')
        self.assertEqual(len(list(self.output.glob('*.pdf'))), 4)

    def test_output_change_after_confirmation_is_reported_without_overwrite(self):
        items = self.items()
        items[0].plan.destination.write_bytes(b'external PDF')
        result = self.run_batch(items=items)
        self.assertFalse(result.complete)
        self.assertEqual(result.outcomes[0].status, 'failed')
        self.assertEqual(items[0].plan.destination.read_bytes(), b'external PDF')

    def test_source_change_during_batch_stops_remaining_copies(self):
        def change_source(source, plan):
            app.copy_with_destination_plan(source, plan)
            source.write_bytes(b'changed original contents')

        result = self.run_batch(copier=change_source)
        self.assertFalse(result.complete)
        self.assertTrue(result.error)

    def test_partial_copy_cleanup_for_plain_and_collision_copies(self):
        def partial(source, destination):
            Path(destination).write_bytes(b'partial')
            raise OSError('copy interrupted')

        for collision in (False, True):
            with self.subTest(collision=collision):
                target = self.output / f'target-{collision}.pdf'
                if collision:
                    target.write_bytes(b'existing')
                plan = app.destination_plan_for_stem(self.output, target.stem, '.pdf')
                with patch('app.shutil.copy2', side_effect=partial):
                    with self.assertRaises(OSError):
                        app.copy_with_destination_plan(self.source, plan)
                self.assertFalse(plan.destination.exists() if collision else target.exists())
                self.assertEqual(list(self.output.glob('*.tmp')), [])
                if collision:
                    self.assertEqual(target.read_bytes(), b'existing')

    def test_failed_rename_rolls_back_prior_renames(self):
        base = self.output / 'report.pdf'
        numbered = self.output / 'report__2.pdf'
        base.write_bytes(b'base')
        numbered.write_bytes(b'numbered')
        plan = app.destination_plan_for_stem(self.output, 'report', '.pdf')
        real_rename = Path.rename

        def fail_base(path, target):
            if path == base:
                raise OSError('rename test failure')
            return real_rename(path, target)

        with patch.object(Path, 'rename', fail_base), self.assertRaises(OSError):
            app.copy_with_destination_plan(self.source, plan)
        self.assertEqual(base.read_bytes(), b'base')
        self.assertEqual(numbered.read_bytes(), b'numbered')
        self.assertEqual(len(list(self.output.glob('*.pdf'))), 2)
        self.assertEqual(list(self.output.glob('*.tmp')), [])

    def test_source_is_never_renamed_when_output_overlaps(self):
        plan = app.destination_plan_for_stem(self.folder, self.source.stem, '.pdf')
        with self.assertRaises(ValueError):
            app.copy_with_destination_plan(self.source, plan)
        self.assertEqual(self.source.read_bytes(), b'original complete PDF bytes')

    def test_lot_normalization_accepts_mountain_and_rejects_ocr_letters(self):
        for raw, expected in (('301 - 6', '301-6'), ('_301-45', '301-45'),
                              ('산 0012–0003', '산12-3'), ('301-0', '301'),
                              ('301-6ㄱ', None), ('30I-6', None), ('0-3', None)):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_survey_lot(raw), expected)


if __name__ == '__main__':
    unittest.main()
