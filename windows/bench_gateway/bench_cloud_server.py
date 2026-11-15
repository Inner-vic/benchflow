"""Start the fixed receiver when this dedicated Jupyter server starts."""
from pathlib import Path
import subprocess
import sys


def _jupyter_server_extension_points():
    return [{'module':'bench_cloud_server'}]


def _load_jupyter_server_extension(serverapp):
    runtime = Path(__file__).resolve().parent
    log = (runtime/'receiver.log').open('ab')
    subprocess.Popen([sys.executable,str(runtime/'cloud_agent.py')], cwd=runtime,
                     stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    serverapp.log.info('Dedicated bench evaluation receiver started')
