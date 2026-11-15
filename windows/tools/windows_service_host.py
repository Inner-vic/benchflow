"""Fixed Task Scheduler action; no user-provided command or credential arguments."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bench_gateway.config import load
from bench_gateway.windows_host import run_host

if __name__=='__main__':raise SystemExit(run_host(load(ROOT)))
