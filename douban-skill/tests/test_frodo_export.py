#!/usr/bin/env python3
"""Offline regression coverage using fictional collections only."""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/douban-frodo-export.py'
SPEC = importlib.util.spec_from_file_location('frodo_export', SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def item(number):
    """Construct one fictional interest."""
    return {'subject': {'id': str(number), 'title': 'Example'},
            'comment': 'First line\nSecond line',
            'rating': {'value': 4, 'max': 5},
            'create_time': '2026-01-01 12:00:00'}


class ExportTests(unittest.TestCase):
    def setUp(self):
        exporter.EXPORT_AUDIT.clear()
        self.output = io.StringIO()
        output_context = contextlib.redirect_stdout(self.output)
        output_context.__enter__()
        self.addCleanup(output_context.__exit__, None, None, None)
        sleep_patch = patch.object(exporter.time, 'sleep')
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def run_pages(self, responses):
        """Run pagination and retain the actual requested offsets."""
        starts = []

        def fetch(url, params):
            starts.append(params['start'])
            return responses[params['start']], 200

        with patch.object(exporter, 'fetch_json', side_effect=fetch):
            result = exporter.fetch_all_interests('example', 'movie', 'done')
        return result, starts

    def test_short_page_advances_window_and_probes_exhaustion(self):
        result, starts = self.run_pages({
            0: {'total': 51, 'interests': [item(i) for i in range(49)]},
            50: {'total': 51, 'interests': [item(50)]},
            100: {'total': 51, 'interests': []},
        })
        self.assertEqual(starts, [0, 50, 100])
        self.assertEqual(len(result), 50)
        self.assertEqual(exporter.EXPORT_AUDIT[0]['unavailable_slots'], 1)

    def test_duplicate_subjects_removed(self):
        result, _ = self.run_pages({
            0: {'total': 51, 'interests': [item(1)]},
            50: {'total': 51, 'interests': [item(1), item(2)]},
            100: {'total': 51, 'interests': []},
        })
        self.assertEqual([entry['subject']['id'] for entry in result], ['1', '2'])
        self.assertEqual(exporter.EXPORT_AUDIT[0]['duplicates_removed'], 1)

    def test_empty_internal_page_does_not_stop(self):
        result, starts = self.run_pages({
            0: {'total': 51, 'interests': []},
            50: {'total': 51, 'interests': [item(50)]},
            100: {'total': 51, 'interests': []},
        })
        self.assertEqual(len(result), 1)
        self.assertEqual(starts, [0, 50, 100])

    def test_empty_collection(self):
        result, starts = self.run_pages({0: {'total': 0, 'interests': []}})
        self.assertEqual(result, [])
        self.assertEqual(starts, [0])

    def test_retry_exhaustion_never_returns_partial_results(self):
        with patch.object(exporter, 'fetch_json', return_value=(
                {'error': 'private response body'}, 503)) as fetch:
            with self.assertRaises(RuntimeError):
                exporter.fetch_all_interests('example', 'movie', 'done')
        self.assertEqual(fetch.call_count, 4)
        self.assertNotIn('private response body', self.output.getvalue())
        self.assertEqual(exporter.EXPORT_AUDIT, [])

    def test_retry_recovers_same_offset(self):
        with patch.object(exporter, 'fetch_json', side_effect=[
                ({}, 503), ({'total': 0, 'interests': []}, 200)]) as fetch:
            self.assertEqual(exporter.fetch_all_interests(
                'example', 'movie', 'done'), [])
        self.assertEqual([call.args[1]['start'] for call in fetch.call_args_list], [0, 0])

    def test_changed_total_rejected(self):
        with self.assertRaises(RuntimeError):
            self.run_pages({0: {'total': 51, 'interests': []},
                            50: {'total': 52, 'interests': []}})

    def test_invalid_schema_and_metadata_rejected(self):
        for response in [
            {'total': 51}, {'total': -1, 'interests': []},
            {'total': '0', 'interests': []},
            {'total': True, 'interests': []},
            {'total': False, 'interests': []},
            {'total': 1, 'interests': [{'subject': {}}]},
            {'total': 0, 'interests': [], 'start': 50},
            {'total': 0, 'interests': [], 'count': 25},
            {'total': 0, 'interests': [item(1)]},
        ]:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.run_pages({0: response})

    def test_page_exceeding_requested_size_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'exceeds requested size'):
            self.run_pages({
                0: {'total': 100, 'interests': [item(i) for i in range(51)]},
                50: {'total': 100, 'interests': []},
                100: {'total': 100, 'interests': []},
            })
        self.assertEqual(exporter.EXPORT_AUDIT, [])

    def test_unique_records_exceeding_total_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'exceed reported total'):
            self.run_pages({
                0: {'total': 1, 'interests': [item(1), item(2)]},
                50: {'total': 1, 'interests': []},
            })
        self.assertEqual(exporter.EXPORT_AUDIT, [])

    def test_safety_limit_never_returns_partial_results(self):
        with patch.object(exporter, 'MAX_PAGES_SAFETY', 1):
            with self.assertRaises(RuntimeError):
                self.run_pages({0: {'total': 51, 'interests': [item(1)]}})

    def test_snapshot_manifest_csv_and_previous_backups(self):
        with tempfile.TemporaryDirectory(prefix='douban export ') as tmp:
            base = Path(tmp) / 'example'
            base.mkdir()
            old = base / '影视.csv'
            old.write_bytes(b'KEEP OLD')
            with patch.dict(os.environ, {'DOUBAN_USER': 'example',
                                         'DOUBAN_OUTPUT_DIR': tmp}), \
                    patch.object(exporter, 'preflight_check', return_value=True):
                with patch.object(exporter, 'fetch_all_interests',
                                  side_effect=RuntimeError('network failure')):
                    with self.assertRaises(RuntimeError):
                        exporter.main()
                self.assertEqual(list(base.glob('snapshot-*')), [])

                def fetch(url, params):
                    entries = [item(1)] if params['start'] == 0 else []
                    return {'total': 1, 'interests': entries}, 200

                with patch.object(exporter, 'fetch_json', side_effect=fetch):
                    exporter.main()
                    exporter.main()
                snapshots = list(base.glob('snapshot-*'))
                self.assertEqual(len(snapshots), 2)
                for snapshot in snapshots:
                    manifest = json.loads((snapshot / 'manifest.json').read_text())
                    self.assertEqual(len(manifest['categories']), 12)
                    self.assertEqual(manifest['total_rows'], 12)
                    count = 0
                    for path in snapshot.glob('*.csv'):
                        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
                        with path.open(encoding='utf-8-sig', newline='') as stream:
                            rows = list(csv.DictReader(stream))
                        self.assertEqual(len(rows), 3)
                        self.assertEqual(rows[0]['comment'], 'First line\nSecond line')
                        self.assertEqual(rows[0]['rating'], '★★★★')
                        count += len(rows)
                    self.assertEqual(count, manifest['total_rows'])
                with patch.object(exporter, 'fetch_json', side_effect=fetch), \
                        patch.object(exporter, 'write_csv', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        exporter.main()
                self.assertEqual(old.read_bytes(), b'KEEP OLD')
                self.assertEqual(len(list(base.glob('snapshot-*'))), 2)
                self.assertEqual(list(base.glob('.pending-*')), [])


if __name__ == '__main__':
    unittest.main()
