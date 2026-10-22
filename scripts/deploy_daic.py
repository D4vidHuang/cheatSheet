#!/usr/bin/env python3
"""Sync tracked source plus the canonical input to a dedicated DAIC directory.

No --delete, model copy, or source project reset. Requires a successful preflight.
"""
import argparse
import hashlib
import json
import shlex
import subprocess
import tarfile
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=Path('configs/server.example.json'))
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    host, remote = config['ssh_host'], config['remote_root']
    if host not in {'daic01', 'daic02'} or not remote.startswith('/tudelft.net/staff-umbrella/cheatsheet research/swe-refactor-'):
        p.error('Use a DAIC host and a dedicated swe-refactor-* path under the verified share')
    subprocess.run(['python3', 'scripts/server_preflight.py', '--host', host,
                    '--output', '.cache/deploy-preflight.json'], check=True)
    paths = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    paths = [x for x in paths if x and not x.startswith('reports/')]
    paths += ['data/SWE-Refactor/pure_refactoring_data.json']
    if 'src/swe_refactor/core.py' not in paths:
        p.error('Stage or commit the completed experiment source before deployment')
    local_manifest = {}
    for name in paths:
        path = Path(name)
        if path.is_symlink() or not path.is_file():
            raise SystemExit('Source manifest includes non-regular file: ' + name)
        local_manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # This NFS export rejects chmod, including rsync's initial-file chmod even
    # with --no-perms. Stream regular-file bytes without applying Unix metadata.
    extract_script = 'import pathlib,sys,tarfile,os\nr=pathlib.Path(' + repr(remote) + ')\n'
    extract_script += 'r.mkdir(parents=True,exist_ok=True)\n'
    extract_script += 'with tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz") as archive:\n'
    extract_script += ' for member in archive:\n'
    extract_script += '  p=pathlib.PurePosixPath(member.name)\n'
    extract_script += '  if not member.isfile() or p.is_absolute() or ".." in p.parts: raise ValueError("unsafe archive member")\n'
    extract_script += '  target=r.joinpath(*p.parts)\n'
    extract_script += '  if not target.resolve().is_relative_to(r.resolve()): raise ValueError("symlink escape")\n'
    extract_script += '  target.parent.mkdir(parents=True,exist_ok=True)\n'
    extract_script += '  with archive.extractfile(member) as src, target.open("wb") as dst:\n'
    extract_script += '   import shutil;shutil.copyfileobj(src,dst)\n'
    process = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', host,
                                'python3 -c ' + shlex.quote(extract_script)], stdin=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdin, mode='w|gz') as archive:
            for name in paths:
                archive.add(name, arcname=name, recursive=False)
    finally:
        process.stdin.close()
    if process.wait() != 0:
        raise SystemExit('Remote source transfer failed')
    verify_script = 'import hashlib,json,pathlib,sys\nr=pathlib.Path(' + repr(remote) + ')\n'
    verify_script += 'expected=json.loads(' + repr(json.dumps(local_manifest)) + ')\n'
    verify_script += 'bad=[p for p,h in expected.items() if hashlib.sha256((r/p).read_bytes()).hexdigest()!=h]\n'
    verify_script += 'print(json.dumps({"verified_files":len(expected),"mismatches":bad}));sys.exit(bool(bad))\n'
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, 'python3', '-'],
                            input=verify_script, text=True, capture_output=True, check=True)
    print(result.stdout)
    Path('.cache').mkdir(exist_ok=True)
    Path('.cache/deploy-manifest.json').write_text(json.dumps(local_manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
