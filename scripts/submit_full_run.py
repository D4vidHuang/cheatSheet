#!/usr/bin/env python3
"""Freeze and submit the complete four-model SWE-Refactor run, without sampling.

Creates a new immutable release and a durable submission journal before sbatch.
Reusing a run ID reuses that exact frozen release and confirmed job IDs.
An unconfirmed submission is never silently repeated.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from swe_refactor.core import (digest, file_sha, load_prepared, prepare, read_json,
                              source_fingerprint, write_json)

MODELS = ['qwen35-2b', 'qwen35-4b', 'qwen35-9b', 'qwen38-27b']
DEFAULT_PYTHON = '/tudelft.net/staff-umbrella/cheatsheet research/cheat/type-inference/qwen35/runtime/venv/bin/python'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def checked_plan(plan):
    body = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    if plan.get('plan_sha256') != digest(body):
        raise ValueError('Frozen plan hash mismatch')
    if (plan['models'] != MODELS or plan['split'] != 'all'
            or plan['expected_tasks_per_model'] != 1099
            or plan['expected_total_requests'] != 4396
            or len(plan['task_ids']) != 1099 or len(set(plan['task_ids'])) != 1099):
        raise ValueError('Full run must cover exactly 1099 tasks for each of four models')
    if plan['task_ids'] != sorted(plan['task_ids']):
        raise ValueError('Task IDs must be sorted before deterministic sharding')
    return plan


def freeze_release(run_id, server, num_shards, concurrency, walltime):
    folder = REPO / 'artifacts/full' / run_id
    release = folder / 'release'
    plan_path = release / 'configs/full_run.plan.json'
    if folder.exists():
        if not plan_path.is_file():
            raise ValueError('Incomplete local release exists; use a fresh run ID')
        plan = checked_plan(read_json(plan_path))
        for key, expected in [('num_shards', num_shards), ('concurrency_per_model', concurrency),
                              ('walltime', walltime), ('ssh_host', server['ssh_host'])]:
            if plan[key] != expected:
                raise ValueError('Existing run ID has different settings: ' + key)
        return release, plan
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=REPO).decode().split('\0')
    paths = [x for x in paths if x and not x.startswith(('reports/', 'data/'))]
    required = ['scripts/full_array_worker.py', 'scripts/aggregate_full_run.py',
                'scripts/full_array.sbatch', 'scripts/full_aggregate.sbatch',
                'scripts/submit_full_run.py', 'src/swe_refactor/core.py']
    if any(name not in paths for name in required):
        raise ValueError('Stage the complete full-run source before freezing')
    release.mkdir(parents=True)
    for name in paths:
        src = REPO / name
        if src.is_symlink() or not src.is_file():
            raise ValueError('Snapshot source must be a regular file: ' + name)
        dest = release / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    # The canonical input is copied unchanged for future independent evaluation.
    data = release / 'data/SWE-Refactor/pure_refactoring_data.json'
    data.parent.mkdir(parents=True)
    shutil.copyfile(REPO / 'data/SWE-Refactor/pure_refactoring_data.json', data)
    prepare(data, release / 'prepared/swe-refactor')
    manifest, tasks = load_prepared(release / 'prepared/swe-refactor', include_gold=False)
    if len(tasks) != 1099:
        raise ValueError('Canonical data does not contain all1099 tasks')
    source_map = {name: file_sha(release / name) for name in paths}
    plan = {
        'schema_version': 1, 'run_id': run_id, 'created_at_utc': utc_now(),
        'models': MODELS, 'num_shards': num_shards,
        'concurrency_per_model': concurrency, 'walltime': walltime, 'split': 'all',
        'expected_tasks_per_model': 1099, 'expected_total_requests': 4396,
        'prepared': 'prepared/swe-refactor', 'run_root': 'runs/full/' + run_id,
        'model_lock': 'configs/models.lock.json', 'experiment': 'configs/experiment.json',
        'task_ids': sorted(t['task_id'] for t in tasks),
        'dataset_manifest_sha256': digest(manifest), 'tasks_sha256': manifest['tasks_sha256'],
        'source_dataset_sha256': manifest['source_sha256'],
        'model_lock_sha256': digest(read_json(release / 'configs/models.lock.json')),
        'experiment_sha256': digest(read_json(release / 'configs/experiment.json')),
        'harness_source_sha256': source_fingerprint(), 'source_files_sha256': source_map,
        'git_commit_at_freeze': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'ssh_host': server['ssh_host'], 'remote_root': server['remote_root'] + '/releases/' + run_id,
        'account': server['account'], 'partition': server['partition'], 'gres': server['gres'],
        'functional_evaluation': 'Requires complete task-specific gold and verified historical build/test configurations; generation/format diagnostics are not semantic scores.'}
    plan['plan_sha256'] = digest(plan)
    checked_plan(plan)
    write_json(plan_path, plan)
    write_json(folder / 'plan.json', plan)
    return release, plan


def ssh_python(host, code, timeout=55):
    return subprocess.run(SSH + [host, 'python3', '-'], input=code, text=True,
                          capture_output=True, check=True, timeout=timeout)


def deploy_release(release, plan):
    host, remote = plan['ssh_host'], plan['remote_root']
    files = sorted(p for p in release.rglob('*') if p.is_file())
    expected = {str(p.relative_to(release)): file_sha(p) for p in files}
    probe = 'import pathlib,json\nr=pathlib.Path(' + repr(remote) + ')\n'
    probe += 'print(json.dumps({"exists":r.exists(),"plan":(r/"configs/full_run.plan.json").exists()}))\n'
    exists = json.loads(ssh_python(host, probe).stdout)
    if exists['exists'] and not exists['plan']:
        raise ValueError('Remote release path exists without a frozen plan; use a new run ID')
    if not exists['exists']:
        code = 'import pathlib,sys,tarfile,shutil\nr=pathlib.Path(' + repr(remote) + ')\n'
        code += 'r.mkdir(parents=True,exist_ok=False)\n'
        code += 'with tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz") as a:\n'
        code += ' for m in a:\n'
        code += '  p=pathlib.PurePosixPath(m.name)\n'
        code += '  if not m.isfile() or p.is_absolute() or ".." in p.parts:raise ValueError("Unsafe source archive")\n'
        code += '  t=r.joinpath(*p.parts);t.parent.mkdir(parents=True,exist_ok=True)\n'
        code += '  with a.extractfile(m) as src,t.open("wb") as dst:shutil.copyfileobj(src,dst)\n'
        process = subprocess.Popen(SSH + [host, 'python3 -c ' + shlex.quote(code)], stdin=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdin, mode='w|gz') as archive:
                for p in files:
                    archive.add(p, arcname=str(p.relative_to(release)), recursive=False)
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError('Snapshot transfer failed; no sbatch calls were made')
    code = 'import pathlib,json,hashlib,sys\nr=pathlib.Path(' + repr(remote) + ')\n'
    code += 'expected=json.loads(' + repr(json.dumps(expected)) + ')\n'
    code += 'bad=[n for n,h in expected.items() if not (r/n).is_file() or hashlib.sha256((r/n).read_bytes()).hexdigest()!=h]\n'
    code += 'result={"verified_files":len(expected),"mismatches":bad}\n'
    code += 'print(json.dumps(result));sys.exit(bool(bad))\n'
    receipt = json.loads(ssh_python(host, code).stdout)
    write_json(release.parent / 'deployment_receipt.json', receipt)
    return receipt


def submit_remote(plan, python):
    """Run this frozen script remotely; a journal makes confirmed submits idempotent."""
    checked_plan(plan)
    root = Path(plan['remote_root'])
    state_path = root / 'submission.json'
    state = read_json(state_path) if state_path.exists() else {
        'run_id': plan['run_id'], 'plan_sha256': plan['plan_sha256'], 'jobs': {}}
    if state['plan_sha256'] != plan['plan_sha256']:
        raise ValueError('Existing submission journal belongs to another plan')
    (root / 'logs').mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(SWE_ROOT=str(root), SWE_PYTHON=python,
               SWE_HF_HOME=str(root.parent.parent / '.cache/hf'),
               SWE_PLAN=str(root / 'configs/full_run.plan.json'))

    def submit(key, argv):
        row = state['jobs'].get(key)
        if row and row.get('status') == 'confirmed':
            return row['job_id']
        if row and row.get('status') == 'submitting':
            raise ValueError('Previous submission has unknown outcome; reconcile before retry: ' + key)
        state['jobs'][key] = {'status': 'submitting', 'started_at_utc': utc_now(), 'argv': argv}
        write_json(state_path, state)
        try:
            p = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            raise  # Preserve unknown state; never risk duplicate GPU jobs.
        if p.returncode:
            state['jobs'][key].update(status='rejected', stderr=p.stderr.strip(), returncode=p.returncode)
            write_json(state_path, state)
            raise RuntimeError('sbatch rejected ' + key + ': ' + p.stderr.strip())
        job_id = p.stdout.strip().split(';')[0]
        if not re.fullmatch(r'[0-9]+', job_id):
            raise ValueError('Unrecognized sbatch receipt; inspect journal before repeating')
        state['jobs'][key].update(status='confirmed', job_id=job_id,
                                  confirmed_at_utc=utc_now())
        write_json(state_path, state)
        return job_id

    ids = []
    for model in plan['models']:
        env['SWE_MODEL'] = model
        argv = ['sbatch', '--parsable', '--account=' + plan['account'],
                '--partition=' + plan['partition'], '--gres=' + plan['gres'],
                '--array=0-%d%%%d' % (plan['num_shards'] - 1, plan['concurrency_per_model']),
                '--time=' + plan['walltime'], '--job-name=swe-full-' + model,
                '--comment=swe-full:' + plan['run_id'] + ':' + model,
                '--chdir=' + str(root), '--export=ALL',
                '--output=' + str(root / ('logs/' + model + '-%A_%a.out')),
                str(root / 'scripts/full_array.sbatch')]
        ids.append(submit(model, argv))
    submit('aggregate', ['sbatch', '--parsable', '--account=' + plan['account'],
                        '--partition=' + plan['partition'], '--dependency=afterany:' + ':'.join(ids),
                        '--comment=swe-full:' + plan['run_id'] + ':aggregate',
                        '--chdir=' + str(root), '--export=ALL',
                        '--output=' + str(root / 'logs/aggregate-%j.out'),
                        str(root / 'scripts/full_aggregate.sbatch')])
    state['all_submitted'] = True
    state['expected_total_requests'] = plan['expected_total_requests']
    state['gpu_array_tasks'] = len(plan['models']) * plan['num_shards']
    write_json(state_path, state)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=REPO / 'configs/server.example.json')
    p.add_argument('--run-id', default='full-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    p.add_argument('--num-shards', type=int, default=16)
    p.add_argument('--concurrency-per-model', type=int, default=1)
    p.add_argument('--walltime', default='12:00:00')
    p.add_argument('--python', default=DEFAULT_PYTHON)
    p.add_argument('--stage-only', action='store_true')
    p.add_argument('--remote-submit', type=Path, help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.remote_submit:
        print(json.dumps(submit_remote(read_json(args.remote_submit), args.python), indent=2))
        return
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', args.run_id):
        p.error('Invalid run ID')
    if not 1 <= args.num_shards <= 1099 or not 1 <= args.concurrency_per_model <= 3:
        p.error('Use 1..1099 shards and 1..3 concurrent shards per model')
    server = read_json(args.config)
    if server['ssh_host'] != 'daic01':
        p.error('This user-confirmed full run targets DAIC01')
    check = subprocess.run([sys.executable, str(REPO / 'scripts/server_preflight.py'),
                            '--output', str(REPO / '.cache/full-preflight.json')],
                           capture_output=True, text=True, check=True)
    release, plan = freeze_release(args.run_id, server, args.num_shards,
                                  args.concurrency_per_model, args.walltime)
    deployment = deploy_release(release, plan)
    if args.stage_only:
        print(json.dumps({'run_id': plan['run_id'], 'deployment': deployment, 'submitted': False}))
        return
    code = 'import subprocess,sys\n'
    code += 'sys.exit(subprocess.call(' + repr([args.python, plan['remote_root'] + '/scripts/submit_full_run.py',
                  '--remote-submit', plan['remote_root'] + '/configs/full_run.plan.json', '--python', args.python]) + '))\n'
    result = ssh_python(server['ssh_host'], code, timeout=55)
    state = json.loads(result.stdout)
    state['remote_root'] = plan['remote_root']
    state['plan_sha256'] = plan['plan_sha256']
    state['deployment'] = deployment
    write_json(release.parent / 'submission.json', state)
    write_json(REPO / 'reports/full_submission.json', state)
    print(json.dumps(state, indent=2))


if __name__ == '__main__':
    main()
