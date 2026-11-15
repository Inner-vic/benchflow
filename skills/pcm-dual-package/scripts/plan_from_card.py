"""Bind confirmed card input to two original Jenkins parameter plans; no network.

The agent still resolves source/recipes, reviews forms, and performs the original
Jenkins workflow. Local confirmation is never permission to click Build.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pcm_existing_job as pair


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('pcm_release_card_input',
    ROOT / 'modules/jenkins-input-card/jenkins_input_card.py')
card = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = card
spec.loader.exec_module(card)


def plan_from_confirmed(confirmed, context):
    """Internal snapshot from InputCards.export_confirmed plus resolved evidence."""
    if confirmed.get('schema') != 'pcm-confirmed-input/v1':
        raise ValueError('Export the authenticated owner\'s confirmed card input first')
    fields = {k: confirmed[k] for k in ('branch_link', 'app_version')}
    if confirmed.get('configuration_sha256') != card.digest(fields):
        raise ValueError('Confirmed input changed after export')
    owner = confirmed['owner']
    card.Identity(owner['tenant'], owner['user'])
    if type(confirmed.get('configuration_revision')) is not int or confirmed['configuration_revision'] < 1:
        raise ValueError('Confirmed configuration revision is missing')
    checked = card.validate(**fields)
    if checked['errors']:
        raise ValueError('Confirmed input is incomplete or invalid')
    source = context['source']
    if source.get('repository') != pair.UROAD or source.get('branch') != checked['branch']:
        raise ValueError('Resolved context belongs to another repository or branch')
    selected = pair.references(checked['references'])
    # This binding is read from the selected base recipe/source by the agent;
    # it must not be inferred from a version's spelling or a branch name.
    evidence = context.get('input_component', {})
    if (evidence.get('ref') != selected['uroad_m100']
            or evidence.get('source_commit') != source.get('commit')):
        raise ValueError('Verify the entered M100 reference against the pinned source commit')
    resolved = copy.deepcopy(context)
    apps = pair.references(resolved['common_app_refs'])
    locked = pair.references(resolved['common_locked_refs'])
    if set(selected) & set(locked):
        raise ValueError('Entered APP components overlap LOCKED_LIBS; resolve the context before review')
    for name, ref in selected.items():
        if name != 'uroad_m100':
            apps[name] = ref  # Explicit user versions override baseline APP defaults.
    resolved['common_app_refs'] = list(apps.values())
    state = pair.make_plan(resolved)
    state['input_confirmation'] = {
        'owner': dict(owner), 'configuration_revision': confirmed['configuration_revision'],
        'configuration_sha256': confirmed['configuration_sha256'],
        'branch_link': fields['branch_link'], 'selected_m100_ref': selected['uroad_m100'],
    }
    state['plan_sha256'] = pair.plan_hash(state)
    return state


def plan_from_card(cards, identity, session_id, context):
    """Preferred host API: ownership and current-confirmation checks run first."""
    return plan_from_confirmed(cards.export_confirmed(identity, session_id), context)


def write_plan(state, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'pair.json').write_bytes(pair.canonical(state))
    for mode, item in state['builds'].items():
        (output / (mode + '.parameters.json')).write_bytes(pair.canonical(item['parameters']))
    (output / 'package-index.json').write_bytes(pair.canonical(pair.delivery_manifest(state)))
    (output / 'package-index.md').write_text(pair.summary(state), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirmed-input', type=Path, required=True,
                        help='Internal export from authenticated InputCards, never a raw callback')
    parser.add_argument('--context', type=Path, required=True, help='Agent-resolved current evidence')
    parser.add_argument('--output-dir', type=Path, required=True, help='New private pair directory')
    args = parser.parse_args()
    state = plan_from_confirmed(pair.load(args.confirmed_input), pair.load(args.context))
    write_plan(state, args.output_dir)
    print(json.dumps({'status': state['status'], 'output': str(args.output_dir),
                      'builds_submitted': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
