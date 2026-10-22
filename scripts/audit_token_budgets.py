#!/usr/bin/env python3
"""Tokenizer audit only: no GPU or generation, no source text in output."""
import argparse
from collections import Counter
import json
from pathlib import Path

from swe_refactor.core import (digest, environment_info, file_sha, load_prepared,
                              make_protocol, messages_for, read_json, write_json)
from swe_refactor.inference import VLLMBackend


def quantiles(values):
    ordered = sorted(values)
    return {label: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))]
            for label, q in [('min', 0), ('median', .5), ('p95', .95), ('max', 1)]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared', type=Path, default=Path('prepared/swe-refactor'))
    p.add_argument('--models', nargs='+', default=['qwen35-2b', 'qwen35-4b', 'qwen35-9b', 'qwen38-27b'])
    p.add_argument('--output', type=Path, default=Path('reports/token_budget_audit.json'))
    args = p.parse_args()
    manifest, tasks, gold = load_prepared(args.prepared, include_gold=True)
    gold_by_id = {g['task_id']: g for g in gold}
    config = read_json(Path('configs/experiment.json'))
    lock = read_json(Path('configs/models.lock.json'))
    report = {'dataset_manifest_sha256': digest(manifest), 'task_count': len(tasks),
              'experiment': config, 'experiment_sha256': digest(config),
              'model_lock_sha256': digest(lock),
              'audit_script_sha256': file_sha(Path(__file__)),
              'environment': environment_info(),
              'scope': 'Reference output lengths concern the one stored after-file, not a complete gold patch or a minimum valid solution length.',
              'models': {}}
    for model in args.models:
        protocol, _ = make_protocol(args.prepared, model, read_json(Path('configs/models.lock.json')),
                                    read_json(Path('configs/experiment.json')), 'all', None, 1, .9, True)
        backend = VLLMBackend(protocol)
        rows, categories = [], Counter()
        for task in tasks:
            _, ids = backend.encode(messages_for(task))
            # Compact JSON is a lower representation-overhead reference. The
            # stored snapshot may omit other required files for Move* tasks.
            gold_text = json.dumps({'files': gold_by_id[task['task_id']]['after_files']}, ensure_ascii=False, separators=(',', ':'))
            gold_ids = backend.tokenizer.encode(gold_text, add_special_tokens=False)
            fits_in = len(ids) <= protocol['experiment']['max_input_tokens']
            fits_ref_out = len(gold_ids) < protocol['experiment']['max_output_tokens']
            category = ('input_fit' if fits_in else 'input_overflow') + '/' + ('reference_output_fit' if fits_ref_out else 'reference_output_overflow')
            categories[category] += 1
            rows.append({'task_id': task['task_id'], 'project': task['project'],
                         'type': task['input']['type'], 'split': task['split'],
                         'input_tokens': len(ids), 'stored_reference_output_tokens': len(gold_ids),
                         'input_fits': fits_in, 'stored_reference_output_fits': fits_ref_out})
        report['models'][model] = {'model': protocol['model'], 'categories': dict(categories),
                                   'input_tokens': quantiles([r['input_tokens'] for r in rows]),
                                   'stored_reference_output_tokens': quantiles([r['stored_reference_output_tokens'] for r in rows]),
                                   'tasks': rows}
        print(json.dumps({'model': model, 'categories': dict(categories)}), flush=True)
    write_json(args.output, report)


if __name__ == '__main__':
    main()
