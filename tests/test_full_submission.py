"""Submission receipts and unknown outcomes must never duplicate GPU work."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('full_submit', Path(__file__).parents[1] / 'scripts/submit_full_run.py')
submit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(submit)


def plan_for(root):
    plan = {'schema_version': 1, 'run_id': 'full-test', 'models': submit.MODELS,
            'split': 'all', 'expected_tasks_per_model': 1099,
            'expected_total_requests': 4396, 'task_ids': [f'{i:064x}' for i in range(1099)],
            'remote_root': str(root), 'num_shards': 16, 'concurrency_per_model': 1,
            'walltime': '12:00:00', 'account': 'test-account', 'partition': 'all',
            'gres': 'gpu:nvidia_rtx_pro_6000:1'}
    plan['plan_sha256'] = submit.digest(plan)
    return plan


class FullSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'release'
        self.root.mkdir()
        self.plan = plan_for(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_four_arrays_and_afterany_aggregate_submitted_exactly_once(self):
        calls = []
        def sbatch(argv, **kwargs):
            calls.append((argv, kwargs['env'].copy()))
            return subprocess.CompletedProcess(argv, 0, str(100 + len(calls)) + '\n', '')
        with mock.patch.object(submit.subprocess, 'run', side_effect=sbatch):
            state = submit.submit_remote(self.plan, '/verified/python')
            again = submit.submit_remote(self.plan, '/verified/python')
        self.assertEqual(len(calls), 5)
        self.assertTrue(state['all_submitted'])
        self.assertEqual(state['gpu_array_tasks'], 64)
        self.assertEqual(state['expected_total_requests'], 4396)
        self.assertEqual(state, again)
        for model, (argv, env) in zip(submit.MODELS, calls[:4]):
            self.assertIn('--array=0-15%1', argv)
            self.assertIn('--time=12:00:00', argv)
            self.assertEqual(env['SWE_MODEL'], model)
            self.assertEqual(env['SWE_ROOT'], str(self.root))
        self.assertIn('--dependency=afterany:101:102:103:104', calls[-1][0])

    def test_unknown_sbatch_outcome_is_never_repeated(self):
        error = subprocess.TimeoutExpired(['sbatch'], 45)
        with mock.patch.object(submit.subprocess, 'run', side_effect=error) as runner:
            with self.assertRaises(subprocess.TimeoutExpired):
                submit.submit_remote(self.plan, '/verified/python')
            with self.assertRaisesRegex(ValueError, 'unknown outcome'):
                submit.submit_remote(self.plan, '/verified/python')
        self.assertEqual(runner.call_count, 1)
        state = json.loads((self.root / 'submission.json').read_text())
        self.assertEqual(state['jobs'][submit.MODELS[0]]['status'], 'submitting')

    def test_known_rejection_can_continue_without_resubmitting_confirmed_array(self):
        outputs = [subprocess.CompletedProcess([], 0, '101\n', ''),
                   subprocess.CompletedProcess([], 1, '', 'scheduler rejected')]
        with mock.patch.object(submit.subprocess, 'run', side_effect=outputs):
            with self.assertRaisesRegex(RuntimeError, 'scheduler rejected'):
                submit.submit_remote(self.plan, '/verified/python')
        with mock.patch.object(submit.subprocess, 'run', side_effect=[
            subprocess.CompletedProcess([], 0, f'{i}\n', '') for i in (102, 103, 104, 105)]) as runner:
            state = submit.submit_remote(self.plan, '/verified/python')
        self.assertEqual(runner.call_count, 4)
        self.assertEqual(state['jobs'][submit.MODELS[0]]['job_id'], '101')

    def test_full_plan_rejects_sampled_or_tampered_denominators(self):
        for field, value in [('expected_tasks_per_model', 2), ('expected_total_requests', 8),
                             ('split', 'pilot'), ('task_ids', self.plan['task_ids'][:-1])]:
            bad = dict(self.plan)
            bad[field] = value
            bad.pop('plan_sha256')
            bad['plan_sha256'] = submit.digest(bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                submit.checked_plan(bad)
        bad = dict(self.plan)
        bad['walltime'] = '1:00:00'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            submit.checked_plan(bad)


if __name__ == '__main__':
    unittest.main()
