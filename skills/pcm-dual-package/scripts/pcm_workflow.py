"""Plan component variants and optional APP delivery; never submit CI or bench jobs.

The legacy pair tracker is only for explicitly requested two-APP releases.
New requests choose delivery independently of the M100 compile/runtime switches.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

from pcm_existing_job import UROAD, references


MODES = ('OFF', 'ON')
DELIVERIES = ('components_only', 'single_app_with_variants', 'app_pair')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def compile_proof(component, mode, proof, commit):
    """Unknown or conflicting provenance is never promoted to a successful build."""
    if proof is None:
        return False
    if not isinstance(proof, dict):
        raise ValueError('Invalid compiler receipt')
    if (proof.get('state') != 'BUILT' or proof.get('source_commit') != commit
            or proof.get('component') != component['ref']
            or proof.get('recipe_sha256') != component['recipe_sha256']
            or proof.get('effective_eval_dump') != mode
            or proof.get('machine') != 'AArch64'
            or not re.fullmatch(r'[a-f0-9]{64}', proof.get('output_sha256', ''))
            or type(proof.get('output_bytes')) is not int or proof['output_bytes'] <= 0):
        raise ValueError('Compiler receipt does not prove this exact component and mode')
    return True


def app_parameters(context, component):
    common = copy.deepcopy(context['common_parameters'])
    required = ('CONTROLLER', 'BASE_LINE', 'SOFTWARE_BRANCH', 'BUILDVARIANT',
                'MANIFEST_NAME', 'BUILD_REPLAY', 'UPLOAD_ALL_PACKAGE', 'SILENT_BUILD')
    if any(k not in common for k in required):
        raise ValueError('Resolve existing Jenkins parameters before planning an APP')
    if common['CONTROLLER'] != 'CCU_M100_AVMLIVC' or common['BUILDVARIANT'] not in ('release', 'debug'):
        raise ValueError('Unsupported target')
    if (not re.fullmatch(r'[0-9A-Fa-f]{4}', common['BASE_LINE'])
            or not re.fullmatch(r'[0-9]{4}', common['SOFTWARE_BRANCH'])):
        raise ValueError('Invalid baseline version or software ID')
    if (common['BUILD_REPLAY'] is not True or common['UPLOAD_ALL_PACKAGE'] is not False
            or type(common['SILENT_BUILD']) is not bool):
        raise ValueError('Preserve reviewed baseline replay, upload and notification policy')
    if any(k in common for k in ('APP_VERSION', 'LOCKED_LIBS')):
        raise ValueError('Keep common references separate from existing Job parameters')
    if (not re.fullmatch(r'[a-f0-9]{64}', context.get('baseline_lock_sha256', ''))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', common['MANIFEST_NAME'])):
        raise ValueError('A verified baseline lock is required')
    if any(common.get(k) for k in ('LIWARE_URL', 'LIMESH_URL', 'PORTAL_TRIGGER_URL', 'PORTAL_RESOURCE_KEY')):
        raise ValueError('This workflow only plans APP packages, not image or Portal actions')
    apps = references(context['common_app_refs'])
    libs = references(context['common_locked_refs'])
    if set(apps) & set(libs) or 'uroad_m100' in apps or 'uroad_m100' in libs:
        raise ValueError('Common dependencies must not overlap or override M100')
    if 'integration_package_names' in context or 'source_discovery_sha256' in context:
        names = context.get('integration_package_names')
        if (not isinstance(names, list) or not names or len(set(names)) != len(names)
                or any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', n) for n in names)
                or 'uroad_m100' not in names
                or not re.fullmatch(r'[a-f0-9]{64}', context.get('integration_package_list_sha256', ''))):
            raise ValueError('A verified integration package list is required')
        if set(libs) & set(names):
            raise ValueError('Packaged components belong in APP_VERSION, not LOCKED_LIBS')
    return dict(common, APP_VERSION='\n'.join([*apps.values(), component['ref']]) + '\n',
                LOCKED_LIBS='\n'.join(libs.values()) + ('\n' if libs else ''))


def make_plan(context):
    source = context['source']
    if source.get('repository') != UROAD or not re.fullmatch(r'[a-f0-9]{40}', source.get('commit', '')):
        raise ValueError('Pin the uroad source commit first')
    modes = context.get('compile_modes')
    if (not isinstance(modes, list) or not modes or any(m not in MODES for m in modes)
            or len(set(modes)) != len(modes)):
        raise ValueError('Declare the requested M100 modes explicitly')
    modes = [m for m in MODES if m in modes]
    delivery = context.get('delivery')
    if delivery not in DELIVERIES:
        raise ValueError('Choose components, one APP with variants, or an explicit APP pair')
    if delivery == 'components_only':
        app_modes = []
    elif delivery == 'single_app_with_variants':
        base = context.get('base_app_mode')
        evidence = context.get('base_app_mode_evidence') or {}
        if (base not in modes or evidence.get('mode') != base
                or evidence.get('source_commit') != source['commit']
                or evidence.get('origin') not in ('verified_source_default', 'explicit_build_variant')):
            raise ValueError('Resolve the base APP mode from source evidence or an explicit request')
        app_modes = [base]
    else:
        if modes != list(MODES):
            raise ValueError('An explicit APP pair needs both M100 modes')
        app_modes = list(MODES)
    requested_publication = context.get('publish_modes', [])
    if (not isinstance(requested_publication, list) or len(set(requested_publication)) != len(requested_publication)
            or any(mode not in modes for mode in requested_publication)):
        raise ValueError('Publication scope must be a unique subset of requested components')
    publication_modes = [mode for mode in MODES if mode in app_modes or mode in requested_publication]
    components = context['components']
    compiled = []
    missing_remote = []
    parameters = {}
    missing_common = []
    if app_modes:
        for ref in context.get('common_app_refs', []) + context.get('common_locked_refs', []):
            if '/pcm-' not in ref:
                continue
            proof = (context.get('common_recipe_evidence') or {}).get(ref, {})
            if (proof.get('availability') != 'remote_recipe'
                    or not re.fullmatch(r'[a-f0-9]{32,64}', proof.get('recipe_revision', ''))
                    or not re.fullmatch(r'[a-f0-9]{64}', proof.get('recipe_sha256', ''))):
                missing_common.append(ref)
    for mode in modes:
        component = components[mode]
        reference = references([component['ref']])
        if (set(reference) != {'uroad_m100'} or component.get('source_commit') != source['commit']
                or component.get('cmake') != {'WITH_EVAL_DUMP': mode}
                or not re.fullmatch(r'[a-f0-9]{64}', component.get('recipe_sha256', ''))):
            raise ValueError('Component identity or compile mode is inconsistent')
        if compile_proof(component, mode, (context.get('compile_evidence') or {}).get(mode), source['commit']):
            compiled.append(mode)
        if mode in app_modes:
            parameters[mode] = app_parameters(context, component)
        if mode in publication_modes:
            if (component.get('availability') != 'remote_recipe'
                    or not re.fullmatch(r'[a-f0-9]{32,64}', component.get('recipe_revision', ''))):
                missing_remote.append(mode)
    if len(compiled) != len(modes):
        state = 'COMPILATION_REQUIRED'
    elif missing_remote:
        state = 'COMPONENT_PUBLICATION_REQUIRED'
    elif missing_common:
        state = 'COMMON_RECIPES_REQUIRED'
    elif app_modes:
        baseline = context.get('baseline_artifact_checks') or {}
        if (baseline.get('verified') is not True or baseline.get('liware_present') is not False
                or baseline.get('limesh_present') is not False):
            state = 'BASELINE_PROPERTIES_REQUIRED'
        else:
            state = 'READY_FOR_APP_REVIEW'
    else:
        state = 'COMPONENTS_COMPILED'
    result = {'schema': 'pcm-build-workflow/v2', 'source': copy.deepcopy(source),
              'delivery': delivery, 'compile_modes': modes, 'compiled_modes': compiled,
              'requested_app_modes': app_modes, 'app_count': len(app_modes),
              'publication_modes': publication_modes,
              'missing_remote_modes': missing_remote, 'parameters': parameters,
              'missing_common_refs': missing_common,
              'common_recipe_evidence': copy.deepcopy(context.get('common_recipe_evidence') or {}),
              'state': state, 'components': {m:copy.deepcopy(components[m]) for m in modes},
              'compile_evidence': copy.deepcopy(context.get('compile_evidence') or {}),
              'baseline_lock_sha256': context.get('baseline_lock_sha256'),
              'baseline_artifact_checks': copy.deepcopy(context.get('baseline_artifact_checks') or {}),
              'base_app_mode_evidence': copy.deepcopy(context.get('base_app_mode_evidence') or {}),
              'integration_package_names': copy.deepcopy(context.get('integration_package_names')),
              'integration_package_list_sha256': context.get('integration_package_list_sha256'),
              'source_discovery_sha256': context.get('source_discovery_sha256'),
              'jenkins_modes': app_modes if state == 'READY_FOR_APP_REVIEW' else [],
              'bench_task_count': 0, 'bench_execution_requires_material_confirmation': True}
    result['plan_sha256'] = digest(result)
    return result


def require_app_review(plan, mode, reviewed_sha):
    payload = {k:v for k,v in plan.items() if k != 'plan_sha256'}
    if (digest(payload) != reviewed_sha or plan.get('plan_sha256') != reviewed_sha
            or plan['state'] != 'READY_FOR_APP_REVIEW' or mode not in plan['jenkins_modes']):
        raise ValueError('Unreviewed, unpublished or unrequested APP build is blocked')
    return copy.deepcopy(plan['parameters'][mode])


def build_review(plan, owner, session, revision):
    """Build an immutable review payload; this does not create a user confirmation."""
    if not owner or not session or type(revision) is not int or revision < 1:
        raise ValueError('Bind review to the requesting user, session and draft revision')
    _reviewable(plan)
    value = {'schema': 'pcm-build-review/v1', 'owner': owner, 'session': session,
             'revision': revision, 'plan': copy.deepcopy(plan), 'state': 'AWAITING_BUILD_CONFIRMATION'}
    value['review_sha256'] = digest(value)
    return value


def _reviewable(plan):
    payload = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    checks = plan.get('baseline_artifact_checks') or {}
    if (digest(payload) != plan.get('plan_sha256')
            or plan.get('state') not in ('COMPILATION_REQUIRED', 'COMPONENT_PUBLICATION_REQUIRED', 'READY_FOR_APP_REVIEW')
            or not plan.get('requested_app_modes') or plan.get('missing_common_refs')
            or checks.get('verified') is not True or checks.get('liware_present') is not False
            or checks.get('limesh_present') is not False
            or set(plan.get('parameters', {})) != set(plan['requested_app_modes'])):
        raise ValueError('Resolve final parameter identities and baseline checks before confirmation')


def confirm_build_review(review, *, actor, session, revision, shown_sha, current_source):
    """For an authenticated real confirmation handler; never call from a timer/model.

    Pure guard only: production callback authentication, atomic consumption and
    exactly-once dispatch remain the responsibility of the existing service.
    """
    value = {k: v for k, v in review.items() if k != 'review_sha256'}
    if (digest(value) != shown_sha or review.get('review_sha256') != shown_sha
            or review.get('state') != 'AWAITING_BUILD_CONFIRMATION'
            or review['owner'] != actor or review['session'] != session
            or review['revision'] != revision or review['plan']['source'] != current_source):
        raise ValueError('Build confirmation is stale, changed or belongs to another user/session')
    _reviewable(review['plan'])
    return copy.deepcopy(review['plan'])


def ready_parameters_after_confirmation(confirmed_plan, ready_plan, mode):
    """A changed dependency/version requires new confirmation, never silent repair.

    The caller must obtain confirmed_plan from the authenticated, atomically
    consumed confirmation handler. This pure function does not authenticate IM.
    """
    _reviewable(confirmed_plan)
    keys = ('source', 'delivery', 'compile_modes', 'requested_app_modes', 'parameters',
            'baseline_lock_sha256', 'baseline_artifact_checks', 'base_app_mode_evidence',
            'common_recipe_evidence', 'integration_package_names',
            'integration_package_list_sha256', 'source_discovery_sha256')
    if any(confirmed_plan.get(k) != ready_plan.get(k) for k in keys):
        raise ValueError('Build inputs changed after confirmation; render a new review')
    for selected in confirmed_plan['compile_modes']:
        for key in ('ref', 'source_commit', 'cmake', 'recipe_sha256'):
            if confirmed_plan['components'][selected][key] != ready_plan['components'][selected][key]:
                raise ValueError('Component identity changed after confirmation')
    return require_app_review(ready_plan, mode, ready_plan['plan_sha256'])


def evaluation_requirements(compile_mode, runtime_enabled, goal, *, outputs_verified=False):
    """Capability/output checks only; not a replacement for GT or bench admission."""
    if compile_mode not in MODES or type(runtime_enabled) is not bool or goal not in ('replay', 'replay_and_evaluate'):
        raise ValueError('Invalid compile/runtime/evaluation choice')
    issues = []
    if compile_mode == 'OFF' and runtime_enabled:
        issues.append('COMPILE_RUNTIME_INCONSISTENT')
    if goal == 'replay_and_evaluate':
        if compile_mode != 'ON':
            issues.append('EVAL_CAPABILITY_REQUIRED')
        if not runtime_enabled:
            issues.append('EVAL_RUNTIME_CONFIGURATION_REQUIRED')
        if outputs_verified is not True:
            issues.append('PREDICTION_OUTPUTS_NOT_VERIFIED')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = make_plan(json.loads(args.context.read_text(encoding='utf-8')))
    # Existing plans are immutable; rerender under a new path after input changes.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'state': value['state'], 'app_count': value['app_count'],
                      'jenkins_modes': value['jenkins_modes'], 'bench_task_count': 0}))


if __name__ == '__main__':
    main()
