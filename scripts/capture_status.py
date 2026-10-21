#!/usr/bin/env python3
"""Capture scheduler and actual model-record counts; never infer work from queueing."""
import argparse
import json
from pathlib import Path
import subprocess

REMOTE = r'''
import json,subprocess,datetime,pathlib,collections
rows=json.loads((root/"smoke_submissions.json").read_text())
for row in rows:
    p=subprocess.run(["squeue","-j",row["job_id"],"-h","-o","%T|%R"],capture_output=True,text=True)
    row["scheduler_state"]=p.stdout.strip()
    row["accounting"]=subprocess.run(["sacct","-j",row["job_id"],"-n","-P","--format=JobID,State,Elapsed,AllocTRES,ExitCode"],capture_output=True,text=True).stdout.strip()
    records=[]
    for p in (root/"runs/smoke"/(row["model"]+"-"+row["job_id"])/"model/records").glob("*.json"):
        records.append(json.loads(p.read_text()))
    row["model_record_count"]=len(records)
    row["nonempty_model_outputs"]=sum(bool(r.get("output_text")) for r in records)
    row["record_status_counts"]=dict(collections.Counter(r.get("status") for r in records))
nodes=subprocess.check_output(["sinfo","-N","-h","-o","%N|%t|%G"],text=True).splitlines()
states={}
for line in nodes:
    name,state,gpu=line.split("|",2)
    if "gpu:" in gpu:states[name]=state
print(json.dumps({"checked_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "jobs":rows,"unique_gpu_node_states":dict(collections.Counter(states.values())),
                  "requested_smoke_tasks":sum(r["limit"] for r in rows),
                  "model_outputs_observed":sum(r["nonempty_model_outputs"] for r in rows)}))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=Path('configs/server.example.json'))
    p.add_argument('--output', type=Path, default=Path('reports/experiment_status.json'))
    args = p.parse_args()
    cfg = json.loads(args.config.read_text())
    code = 'import pathlib\nroot=pathlib.Path(' + repr(cfg['remote_root']) + ')\n' + REMOTE
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12',
                             cfg['ssh_host'], 'python3', '-'], input=code, text=True,
                            capture_output=True, check=True, timeout=55)
    status = json.loads(result.stdout)
    pending = all(r['scheduler_state'].startswith('PENDING') for r in status['jobs'])
    draining = bool(status['unique_gpu_node_states']) and all(
        state.startswith(('drain', 'drng')) for state in status['unique_gpu_node_states'])
    status.update(status='blocked_by_cluster_maintenance' if pending and draining else 'inspect_job_records',
                  models=['qwen35-2b', 'qwen35-4b', 'qwen35-9b', 'qwen38-27b'],
                  planned_formal_test_tasks_per_model=891,
                  formal_inference_completed=False, functional_performance=None,
                  cluster_announcement={
                      'url': 'https://mattermost.tudelft.nl/daic/pl/54d4n1fuqbrjdy6eba6se6cx9a',
                      'source': 'User-provided Koen Mulderij announcement: kernel vulnerability drain and reboot; existing jobs continue, new jobs do not start.'},
                  remaining_before_formal_scores=[
                      'GPU allocation and real generation',
                      'Per-project historical JDK/build/test configurations',
                      'Complete task-specific gold patches with positive/no-op controls',
                      'Independent repository scoring and paired grouped analysis'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(status, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(status, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
