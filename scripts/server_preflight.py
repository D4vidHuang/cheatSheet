#!/usr/bin/env python3
"""Check SSH, Kerberos, storage, Slurm and tool inventory without allocating a GPU."""
import argparse
import datetime
import json
import subprocess
from pathlib import Path

REMOTE = r'''
import getpass,json,os,shutil,socket,subprocess,tempfile
def command(argv):
    if not shutil.which(argv[0]):
        return {"returncode":127,"stdout":"","stderr":"not installed"}
    try:
        p=subprocess.run(argv,capture_output=True,text=True,timeout=15)
        return {"returncode":p.returncode,"stdout":p.stdout.strip(),"stderr":p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"returncode":124,"stdout":"","stderr":"timeout"}
share="/tudelft.net/staff-umbrella/cheatsheet research"
kerberos=command(["klist","-s"])
d={"hostname":socket.getfqdn(),"user":getpass.getuser(),"kerberos":kerberos,
   "share":{"path":share,"exists":os.path.isdir(share),"readable":os.access(share,os.R_OK),"writable":os.access(share,os.W_OK)},
   "association":command(["sacctmgr","-n","-P","show","assoc","where","user="+getpass.getuser(),"format=Cluster,Account,User,Partition,QOS"]),
   "queue":command(["squeue","--me","-h","-o","%i|%j|%T|%R"]),
   "gpu_inventory":command(["sinfo","-h","-o","%P|%G|%a"]),
   "node_states":command(["sinfo","-N","-h","-o","%N|%t|%G"]),
   "tools":{x:shutil.which(x) for x in ["python3","uv","java","mvn","sbatch","apptainer"]},
   "gpu_allocated_by_this_check":False}
d["share"]["write_read_delete_verified"]=False
if kerberos["returncode"]==0 and d["share"]["readable"] and d["share"]["writable"]:
    try:
        fd,name=tempfile.mkstemp(prefix=".swe-refactor-preflight-",dir=share)
        try:
            with os.fdopen(fd,"w") as f:f.write("swe-refactor preflight\n")
            with open(name) as f:assert f.read()=="swe-refactor preflight\n"
            d["share"]["write_read_delete_verified"]=True
        finally:os.unlink(name)
    except OSError as e:d["share"]["write_error"]=str(e)
d["ready_for_setup"]=bool(kerberos["returncode"]==0 and d["share"]["write_read_delete_verified"] and d["tools"]["sbatch"] and d["association"]["returncode"]==0)
d["gpu_allocation_verified"]=False
print(json.dumps(d))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', choices=['daic01', 'daic02', 'agent16'], default='daic01')
    p.add_argument('--output', type=Path, default=Path('reports/server_preflight.json'))
    args = p.parse_args()
    report = {'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'ssh_host': args.host, 'ready_for_setup': False}
    try:
        result = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12',
             '-o', 'ConnectionAttempts=1', args.host, 'python3', '-'],
            input=REMOTE, text=True, capture_output=True, timeout=55)
        report['ssh_returncode'] = result.returncode
        if result.returncode == 0:
            report.update(json.loads(result.stdout))
        else:
            report['error'] = result.stderr.strip()
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        report['error'] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report['ready_for_setup'] else 2)


if __name__ == '__main__':
    main()
