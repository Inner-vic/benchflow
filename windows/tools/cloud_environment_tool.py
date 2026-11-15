"""Read/check or explicitly request one fixed development-space startup."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bench_gateway.config import load
from bench_gateway.cloud_provider import profile_for_job
from bench_gateway.cloud_bootstrap import EnvironmentTool


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['check','ensure'])
    p.add_argument('--root',type=Path,default=Path('D:/developer/Desktop/Project/台架飞书统一入口'))
    p.add_argument('--request-start',action='store_true',help='Explicitly authorize requesting startup through the official CLI; platform approval is respected')
    p.add_argument('--wait-seconds',type=float,default=0)
    p.add_argument('--backend',choices=['cli','browser'],default='cli')
    args=p.parse_args()
    if args.action=='check' and args.request_start:p.error('check cannot request startup')
    cfg=load(args.root);profile=profile_for_job(cfg,{'created':time.time()})
    if profile is None:p.error('no active cloud evaluation profile')
    tool=EnvironmentTool(cfg,profile,binary=str(Path(os.environ['LOCALAPPDATA'])/'bin/lpai-agent.exe'),
                         control_root=Path(cfg['project_root'])/'runtime/cloud-environment-control',backend=args.backend)
    result=tool.check() if args.action=='check' else tool.ensure(allow_start=args.request_start,wait_seconds=args.wait_seconds)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
