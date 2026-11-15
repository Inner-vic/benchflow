"""Standalone Linux input/validation entry. No build/confirm/bench command exists."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

from source_build_discovery import discover, LiveReader, verify_recipe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway-root', type=Path, required=True, help='Existing encrypted authentication only')
    parser.add_argument('--state-root', type=Path, required=True, help='Separate workstation feature state directory')
    parser.add_argument('--owner', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--branch-url', required=True)
    parser.add_argument('--components-file', type=Path)
    parser.add_argument('--baseline-build', type=int)
    parser.add_argument('--software-version', required=True, help='Explicit software version; never inherited from baseline')
    parser.add_argument('--software-id', choices=['0111'], default='0111', help=argparse.SUPPRESS)
    parser.add_argument('--build-variant', choices=['release', 'debug'], default='release')
    parser.add_argument('--card-output', type=Path, required=True)
    args = parser.parse_args()
    business_root = Path('/mnt/data/bench-eval')
    if sys.platform == 'win32' or business_root not in args.card_output.resolve().parents:
        raise ValueError('Feature state and cards must stay on the Linux workstation')
    module = Path(__file__).resolve().parents[3]/'workstation/gateway/bench_gateway/pcm_dual_build.py'
    spec = importlib.util.spec_from_file_location('pcm_dual_feature', module)
    feature = importlib.util.module_from_spec(spec);spec.loader.exec_module(feature)
    store = feature.live_drafts(args.state_root)
    draft = store.open(args.owner, args.session)
    text = args.components_file.read_text() if args.components_file else draft['component_versions']
    release = {'BASE_LINE': args.software_version, 'SOFTWARE_BRANCH': args.software_id, 'BUILDVARIANT': args.build_variant}
    ticket, started = store.begin_validation(args.owner, args.session, draft['id'], draft['nonce'], draft['revision'], args.branch_url, text, args.baseline_build, release)
    if started:
        reader = LiveReader(args.gateway_root, args.owner)
        result = {}
        def resolve(request):
            if args.baseline_build is not None:
                request = dict(request, baseline_build=args.baseline_build)
            value = discover(request, reader);result['discovery'] = value
            return value
        def verify(ref):
            value = result.get('discovery', {})
            locked = value.get('baseline', {}).get('lock_refs', {}).get(ref.split('/')[0], '')
            revision = locked.split('#', 1)[1] if locked.split('#')[0] == ref and '#' in locked else None
            return verify_recipe(reader, ref, revision)
        report, accepted = feature.run_validation(store, ticket, resolve, verify)
        if not accepted:
            raise ValueError('Validation result became stale; inputs retained')
    current = store.open(args.owner, args.session)
    with args.card_output.open('x') as output:
        json.dump(feature.render_card(current), output, ensure_ascii=False, indent=2)
    print(json.dumps({'feature': feature.FEATURE, 'state': current['state'],
                      'message': (current['report'] or {}).get('message'),
                      'jenkins_submitted': False, 'bench_tasks': 0}, ensure_ascii=False))


if __name__ == '__main__': main()
