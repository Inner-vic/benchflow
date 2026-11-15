import json
import os
from pathlib import Path
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def load(root=ROOT):
    root = Path(root).resolve()
    path = root / "config" / "gateway.json"
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    cfg["project_root"] = str(root)
    if cfg.get('deployment_role')=='ai-client':
        # The PC has program configuration only; never manufacture a business
        # state directory when an old operator command is used accidentally.
        cfg.pop('state_root',None)
    else:
        cfg["state_root"] = str((root / cfg.get("state_root", "state")).resolve())
    return cfg


def ssh_binary():
    return str(Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH/ssh.exe") if os.name == "nt" else "ssh"


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Readers can briefly deny replacement on Windows. Each writer also needs
    # its own temporary file so concurrent saves never replace another's bytes.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    delays = (0, .01, .02, .04, .08, .16, .32)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        for attempt, delay in enumerate(delays):
            if delay:time.sleep(delay)
            try:
                os.replace(tmp, path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(delays) - 1:
                    raise
    finally:
        tmp.unlink(missing_ok=True)
