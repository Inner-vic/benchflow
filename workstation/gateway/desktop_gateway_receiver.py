"""Fixed workstation entry point for authenticated SSH desktop intake."""
import argparse
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from bridge_protocol import MAX_PACKET, decode, dispatch


def source_version(root):
    # Detect deployed code/config updates before dispatching another request.
    paths = [*sorted(root.glob('*.py')), *sorted((root/'config').glob('*.json')),
             *sorted((root/'bench_gateway').glob('*.py'))]
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)


def menu_stream(root, project, handle, instream, outstream):
    version = source_version(root)
    def send(value):
        outstream.write(json.dumps(value, ensure_ascii=False)+'\n')
        outstream.flush()
    send({'kind': 'ready', 'protocol': 'menu-stream-v1'})
    while True:
        raw = instream.readline(MAX_PACKET+1)
        if not raw:
            return
        if len(raw.encode('utf-8')) > MAX_PACKET or not raw.endswith('\n'):
            return
        if source_version(root) != version:
            send({'kind': 'not_started'})
            return
        try:
            value = json.loads(raw)
            if value == {'kind': 'ping'}:
                send({'kind': 'pong'})
                continue
            if not isinstance(value, dict) or set(value) != {'kind', 'packet'} or value['kind'] != 'menu':
                send({'kind': 'not_started'})
                continue
            packet = decode(json.dumps(value['packet']), project)
            if not packet['text'].startswith('@@BENCH_MENU@@'):
                send({'kind': 'not_started'})
                continue
        except (ValueError, TypeError):
            send({'kind': 'not_started'})
            continue
        # Original intake still checks maintenance, HMAC, owner and deduplication.
        # Never retry after entering it, including when response delivery fails.
        try:
            send({'kind': 'result', 'response': dispatch(packet, project, handle)})
        except Exception:
            send({'kind': 'unknown'})
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--health',action='store_true')
    parser.add_argument('--menu-stream',action='store_true')
    args=parser.parse_args()
    root = args.root.resolve(strict=True)
    if root != Path(__file__).resolve().parent:
        raise ValueError('Receiver root differs from its installed program')
    sys.path.insert(0, str(root))
    from bench_gateway.config import load
    cfg = load(root)
    if args.health:
        print(json.dumps({'transport':'desktop-ssh-v1','project':cfg['cc_project'],
            'business_state_on_workstation':Path(cfg['state_root']).is_dir(),
            'bot_fingerprint':hashlib.sha256(cfg['feishu_app_id'].encode()).hexdigest()[:12]}));return
    from bench_gateway.cc_agent import main as original_intake

    def handle(session, project, text):
        original_stdin, original_argv = sys.stdin, sys.argv
        previous = {k:os.environ.get(k) for k in ('CC_SESSION_KEY', 'CC_PROJECT')}
        output = io.StringIO()
        try:
            os.environ.update(CC_SESSION_KEY=session, CC_PROJECT=project)
            sys.argv = [str(root/'gateway_agent.py'), 'exec', '--json', '-']
            from bench_gateway.maintenance import notice
            from desktop_file_ingress import intake
            # Maintenance is checked before any attachment download. The old
            # intake still supplies its own maintenance response and policy.
            if notice(cfg):
                sys.stdin=io.StringIO(text)
                with redirect_stdout(output):original_intake()
            else:
                with intake(cfg,session,text) as normalized:
                    sys.stdin=io.StringIO(normalized)
                    with redirect_stdout(output):original_intake()
        except Exception:
            # Avoid exposing request text, credentials or paths from transport errors.
            output=io.StringIO()
            for event in [{'type':'thread.started','thread_id':'desktop-intake-'+uuid.uuid4().hex},
                          {'type':'item.completed','item':{'type':'agent_message','text':'附件接收或工作站处理未确认，未自动重发。请先核对原任务卡和已收材料；尚不能据此提交台架。'}},
                          {'type':'turn.completed'}]:output.write(json.dumps(event,ensure_ascii=False)+'\n')
        finally:
            sys.stdin, sys.argv = original_stdin, original_argv
            for key, old in previous.items():
                if old is None:os.environ.pop(key, None)
                else:os.environ[key]=old
        return output.getvalue()

    if args.menu_stream:
        menu_stream(root, cfg['cc_project'], handle, sys.stdin, sys.stdout)
    else:
        value = decode(sys.stdin.read(MAX_PACKET+1), cfg['cc_project'])
        sys.stdout.write(dispatch(value, cfg['cc_project'], handle));sys.stdout.flush()


if __name__ == '__main__':main()
