"""Read-only discovery and confirmation boundaries; no live CI or hardware calls."""
import copy
import json
import io
import tarfile
import urllib.error
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_build_discovery as discovery
import pcm_workflow as workflow
from test_workflow import context


class Reader:
    def __init__(self, *, max_product=False, mismatch=False, dynamic=False, image=False):
        self.calls = []
        self.commit = 'a'*40
        self.image = image
        self.refs = {n: n+'/1.9.0@livc/stable' for n in discovery.CORE}
        self.recipe = '\n'.join('self.requires('+repr(v)+')' for v in self.refs.values())
        if dynamic:
            self.recipe = self.recipe.replace(repr(self.refs['xlidar']), 'os.getenv("RADAR_REF")')
        self.builds, self.locks, self.locations = [], {}, {}
        for number, product in [(101, 'ULTRA')] + ([(102, 'MAX')] if max_product else []):
            filename = 'CCU_LIVC_AVM-release-SW00AF_'+product+'_ID0123.20261201-'+str(number)+'.lock'
            uri = discovery.RELEASE+filename
            refs = dict(self.refs)
            if mismatch:
                refs['xlidar'] = 'xlidar/9.0.0@livc/stable'
            nodes = {str(i): {'ref': v+'#'+'b'*32} for i, v in enumerate(refs.values())}
            nodes['integration'] = {'ref': 'livc_integrate/1.2.0-avm@livc/stable#'+'c'*32}
            raw = json.dumps({'graph_lock': {'nodes': nodes}}).encode()
            self.locks[uri] = raw
            self.locations[number] = uri
            params = {'CONTROLLER': 'CCU_M100_AVMLIVC', 'BASE_LINE': '00AF',
                      'SOFTWARE_BRANCH': '0123', 'BUILDVARIANT': 'release',
                      'MANIFEST_NAME': 'an-old-baseline-name',
                      'APP_VERSION': 'conan_shared/8.4.0@livc/release-schu', 'LOCKED_LIBS': ''}
            self.builds.append({'number': number, 'building': False, 'result': 'SUCCESS',
                'url': discovery.JOB_URL+str(number)+'/',
                'actions': [{'parameters': [{'name': k, 'value': v} for k, v in params.items()]}]})

    def get(self, url, limit):
        self.calls.append(url)
        path = unquote(urlsplit(url).path)
        query = parse_qs(urlsplit(url).query)
        if '/repository/branches/' in path:
            result = {'commit': {'id': self.commit}}
        elif '/repository/files/conanfile.py/raw' in path:
            return self.recipe.encode()
        elif '/repository/files/CMakeLists.txt/raw' in path:
            return b'option(WITH_EVAL_DUMP "offline" OFF)'
        elif '/api/search/artifact' in path:
            number = int(query['name'][0].split('-')[-1].split('.')[0])
            result = {'results': [{'uri': self.locations[number]}]}
        elif url.endswith('export/package.lst'):
            return b'uroad_m100\nxlidar\nxcamera\n'
        elif url in self.locks:
            result = {'size': str(len(self.locks[url])), 'checksums': {'sha256': discovery.sha(self.locks[url])}}
        elif url.endswith('?properties'):
            result = {'properties': {'liware_url': ['https://example.invalid/image']} if self.image else {}}
        elif url.replace('/artifactory/', '/artifactory/api/storage/', 1) in self.locks:
            return self.locks[url.replace('/artifactory/', '/artifactory/api/storage/', 1)]
        elif path.endswith('/api/json'):
            before = path[:-len('/api/json')].rstrip('/').split('/')[-1]
            result = next(b for b in self.builds if b['number'] == int(before)) if before.isdigit() else {'builds': self.builds}
        else:
            raise AssertionError('Unexpected endpoint '+url)
        return json.dumps(result).encode()


class SourceDiscoveryTests(unittest.TestCase):
    def request(self, **extra):
        return dict(repository='uroad', branch='feature/new-branch', **extra)

    def test_m100_recipe_basis_is_retained_outside_common_app_components(self):
        ref = 'uroad_m100/3.7.0@livc/stable'
        result = discovery.discover(self.request(component_overrides=[ref, 'dag_compiler/2.0@livc/stable']), Reader())
        self.assertEqual(result['state'], 'RESOLVED_FOR_REVIEW')
        value = discovery.common_inputs(result, {'BASE_LINE': '00B1', 'BUILDVARIANT': 'release'})
        self.assertEqual(value['m100_recipe_ref'], ref)
        self.assertEqual(value['common_parameters']['SOFTWARE_BRANCH'], '0111')
        self.assertNotIn(ref, value['common_app_refs']+value['common_locked_refs'])
        self.assertIn('dag_compiler/2.0@livc/stable', value['common_locked_refs'])

    def test_auto_resolves_new_versions_without_current_run_defaults(self):
        reader = Reader()
        result = discovery.discover(self.request(), reader)
        self.assertEqual(result['state'], 'RESOLVED_FOR_REVIEW')
        value = discovery.common_inputs(result, {'BASE_LINE': '00B1', 'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release'})
        self.assertEqual(value['source']['branch'], 'feature/new-branch')
        self.assertEqual(value['common_parameters']['BASE_LINE'], '00B1')
        self.assertEqual(value['common_parameters']['SOFTWARE_BRANCH'], '0111')
        self.assertIn('conan_shared/8.4.0@livc/release-schu', value['common_app_refs'])
        self.assertIn(reader.refs['xlidar'], value['common_app_refs'])
        self.assertNotIn(reader.refs['xlidar'], value['common_locked_refs'])
        self.assertTrue(value['common_parameters']['MANIFEST_NAME'].endswith('-101'))
        self.assertFalse(result['jenkins_submitted'])

    def test_default_https_port_in_real_artifactory_results_is_normalized(self):
        reader = Reader()
        class WithPort:
            def get(self, url, limit):
                raw = reader.get(url, limit)
                if '/api/search/artifact?' in url:
                    return raw.replace(b'https://artifactory.ep.chehejia.com/', b'https://artifactory.ep.chehejia.com:443/')
                return raw
        self.assertEqual(discovery.discover(self.request(), WithPort())['state'], 'RESOLVED_FOR_REVIEW')
        self.assertIsNone(discovery.storage_uri('https://evil.invalid/artifactory/api/storage/xcu-acore-test-local/Acore/Civu/Release/f.lock'))

    def test_real_short_revision_and_packaged_export_are_supported_without_extraction(self):
        reader = Reader()
        for uri, raw in list(reader.locks.items()):
            reader.locks[uri] = raw.replace(('c'*32).encode(), ('c'*31).encode())
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            raw = b'uroad_m100\nxlidar\n';entry = tarfile.TarInfo('package.lst');entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
        class ExportReader:
            def get(self, url, limit):
                if url.endswith('export/package.lst'):
                    raise urllib.error.HTTPError(url, 404, 'Not found', {}, None)
                if url.endswith('export/conan_export.tgz'):
                    return data.getvalue()
                return reader.get(url, limit)
        self.assertEqual(discovery.discover(self.request(), ExportReader())['state'], 'RESOLVED_FOR_REVIEW')

    def test_product_ambiguity_does_not_silently_choose_ultra_or_latest(self):
        result = discovery.discover(self.request(), Reader(max_product=True))
        self.assertEqual(result['state'], 'NEEDS_BASELINE_SELECTION')
        self.assertNotIn('baseline', result)
        with self.assertRaises(ValueError): discovery.common_inputs(result, {'BASE_LINE': '00B1', 'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release'})

    def test_explicit_baseline_or_product_resolves_ambiguity(self):
        for extra in ({'baseline_build': 101}, {'product_variant': 'ULTRA'}):
            with self.subTest(extra=extra):
                result = discovery.discover(self.request(**extra), Reader(max_product=True))
                self.assertEqual(result['baseline']['number'], 101)

    def test_mismatched_actual_lock_is_not_accepted_from_parameters(self):
        result = discovery.discover(self.request(), Reader(mismatch=True))
        self.assertEqual(result['state'], 'NO_VERIFIED_BASELINE')

    def test_supported_range_is_pinned_from_real_lock_not_latest_guess(self):
        reader = Reader()
        reader.recipe = reader.recipe.replace('function-message/1.9.0@livc/stable',
                                              'function-message/[>=1.0.0 <2.0.0,include_prerelease=True]@livc/stable')
        result = discovery.discover(self.request(), reader)
        self.assertEqual(result['state'], 'RESOLVED_FOR_REVIEW')
        self.assertIn('[>=1.0.0', result['dependency_requirements']['function-message'])
        self.assertEqual(result['resolved_dependencies']['function-message'], 'function-message/1.9.0@livc/stable')
        self.assertTrue(discovery.satisfies('function-message/[>0.0.0]@ad/release',
                                          'function-message/v0.0.341.1@ad/release'))
        self.assertFalse(discovery.satisfies('function-message/[>1.0.0]@ad/release',
                                           'function-message/v0.0.341.1@ad/release'))

    def test_unhandled_range_needs_real_conan_solver_instead_of_approximation(self):
        reader = Reader()
        reader.recipe = reader.recipe.replace('xlidar/1.9.0@livc/stable', 'xlidar/[~1.0]@livc/stable')
        self.assertEqual(discovery.discover(self.request(), reader)['state'], 'DEPENDENCY_RESOLUTION_REQUIRED')

    def test_mode_follows_current_source_or_explicit_variant_never_old_goal(self):
        self.assertEqual(discovery.source_mode('pass', 'option(WITH_EVAL_DUMP "x" ON)'), 'ON')
        self.assertIsNone(discovery.source_mode('pass', 'set(WITH_EVAL_DUMP ON)'))
        a = discovery.discover(self.request(base_app_mode='ON'), Reader())
        self.assertEqual(a['base_app_mode'], 'ON')
        self.assertEqual(a['base_app_mode_evidence']['origin'], 'explicit_build_variant')

    def test_editable_component_versions_are_not_ignored_or_inherited_silently(self):
        request = self.request(component_overrides=['xlidar/9.0.0@livc/stable'])
        result = discovery.discover(request, Reader())
        self.assertEqual(result['state'], 'DEPENDENCY_OVERRIDE_CONFLICT')
        self.assertEqual(result['override_conflicts']['xlidar']['requested'], 'xlidar/9.0.0@livc/stable')
        result = discovery.discover(self.request(component_overrides=['sensor_extra/2.0@livc/stable']), Reader())
        self.assertIn('sensor_extra/2.0@livc/stable', discovery.common_inputs(result, {'BASE_LINE': '00B1', 'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release'})['common_locked_refs'])
        result = discovery.discover(self.request(component_overrides=['conan_shared/9.9.0@livc/release-schu']), Reader())
        self.assertEqual(result['state'], 'NO_VERIFIED_BASELINE')

    def test_explicit_successful_baseline_can_pin_replay_lidar_override(self):
        reader = Reader(mismatch=True)
        requested = 'xlidar/9.0.0@livc/stable'
        result = discovery.discover(self.request(component_overrides=[requested], baseline_build=101), reader)
        self.assertEqual(result['state'], 'RESOLVED_FOR_REVIEW')
        proof = result['dependency_adjustments']['xlidar']
        self.assertEqual(proof['baseline'], 101)
        self.assertEqual(proof['locked_ref'].split('#')[0], requested)
        self.assertEqual(proof['lock_sha256'], result['baseline']['lock_sha256'])
        self.assertEqual(discovery.discover(self.request(component_overrides=[requested], baseline_build=101), Reader())['state'], 'NO_VERIFIED_BASELINE')
        reader = Reader(mismatch=True);reader.builds[0]['result'] = 'FAILURE'
        self.assertEqual(discovery.discover(self.request(component_overrides=[requested], baseline_build=101), reader)['state'], 'NO_VERIFIED_BASELINE')
        self.assertEqual(discovery.discover(self.request(component_overrides=['xcamera/9.0@livc/stable'], baseline_build=101), Reader())['state'], 'DEPENDENCY_OVERRIDE_CONFLICT')

    def test_dynamic_or_conflicting_requirements_stop_before_baseline_search(self):
        reader = Reader(dynamic=True)
        result = discovery.discover(self.request(), reader)
        self.assertEqual(result['state'], 'DEPENDENCY_RESOLUTION_REQUIRED')
        self.assertIn('xlidar', result['unresolved_dependencies'])
        self.assertFalse(any('api/search' in url for url in reader.calls))
        reader = Reader(); reader.recipe += '\nself.requires("xlidar/2.0.0@livc/stable")'
        self.assertEqual(discovery.discover(self.request(), reader)['state'], 'DEPENDENCY_RESOLUTION_REQUIRED')

    def test_unknown_repository_and_missing_branch_never_use_old_input(self):
        for request, state in [({'repository': 'uroad'}, 'NEEDS_BRANCH'),
                ({'repository': 'https://gitlab.chehejia.com/other/repo', 'branch': 'new'}, 'REPOSITORY_COMPATIBILITY_CHECK_REQUIRED')]:
            reader = Reader(); result = discovery.discover(request, reader)
            self.assertEqual(result['state'], state)
            self.assertEqual(reader.calls, [])

    def test_source_head_is_frozen_and_change_invalidates_discovery_digest(self):
        reader = Reader(); first = discovery.discover(self.request(), reader)
        reader.commit = 'd'*40; second = discovery.discover(self.request(), reader)
        self.assertNotEqual(first['discovery_sha256'], second['discovery_sha256'])
        first['source']['commit'] = 'e'*40
        with self.assertRaises(ValueError): discovery.common_inputs(first)

    def test_inherited_image_requires_review_instead_of_silently_dropping(self):
        result = discovery.discover(self.request(), Reader(image=True))
        self.assertEqual(result['state'], 'IMAGE_BASELINE_REQUIRES_REVIEW')
        with self.assertRaises(ValueError): discovery.common_inputs(result, {'BASE_LINE': '00B1', 'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release'})

    def test_bounded_incomplete_search_does_not_claim_unique_baseline(self):
        result = discovery.discover(self.request(), Reader(max_product=True), max_lock_checks=1)
        self.assertEqual(result['state'], 'NEEDS_BASELINE_SELECTION')

    def test_live_reader_refuses_cross_host_or_embedded_credentials_before_io(self):
        reader = discovery.LiveReader.__new__(discovery.LiveReader)
        reader.headers = {'gitlab.chehejia.com': {'PRIVATE-TOKEN': 'synthetic'}}
        for url in ('https://evil.invalid/path', 'http://gitlab.chehejia.com/path',
                    'https://user:pass@gitlab.chehejia.com/path'):
            with self.subTest(url=url), self.assertRaises(ValueError): reader.get(url, 100)

    def test_recipe_validation_pins_revision_and_checks_remote_content(self):
        raw = b'class Synthetic: pass\n'
        class RecipeReader:
            def get(self, url, limit):
                if url.endswith('/export/conanfile.py') and '/api/storage/' not in url: return raw
                if url.endswith('/export/conanfile.py'):
                    return json.dumps({'checksums': {'sha256': discovery.sha(raw)}}).encode()
                return json.dumps({'children': [{'uri': '/'+'e'*31, 'folder': True}]}).encode()
        result = discovery.verify_recipe(RecipeReader(), 'component/2.0@livc/stable')
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(result['recipe_revision'], 'e'*31)
        class Ambiguous:
            def get(self, url, limit):
                return json.dumps({'children': [{'uri': '/a', 'folder': True}, {'uri': '/b', 'folder': True}]}).encode()
        self.assertEqual(discovery.verify_recipe(Ambiguous(), 'component/2.0@livc/stable')['state'], 'NEEDS_REVISION_SELECTION')

    def test_live_reader_caches_only_within_attempt_and_respects_smaller_limits(self):
        reader = discovery.LiveReader.__new__(discovery.LiveReader)
        reader.headers = {'gitlab.chehejia.com': {'PRIVATE-TOKEN': 'synthetic'}}
        reader.deadline = 100;reader.cache = {};reader.opener = MagicMock()
        response = reader.opener.open.return_value.__enter__.return_value
        response.read1.side_effect = [b'fixture', b'']
        with patch.object(discovery.time, 'monotonic', return_value=1):
            self.assertEqual(reader.get('https://gitlab.chehejia.com/api/v4/example', 100), b'fixture')
            self.assertEqual(reader.get('https://gitlab.chehejia.com/api/v4/example', 100), b'fixture')
            with self.assertRaises(ValueError): reader.get('https://gitlab.chehejia.com/api/v4/example', 2)
        self.assertEqual(reader.opener.open.call_count, 1)
        self.assertEqual(reader.opener.open.call_args.kwargs['timeout'], 10)

    def test_expired_attempt_does_not_make_network_call_or_reuse_cache(self):
        reader = discovery.LiveReader.__new__(discovery.LiveReader)
        reader.headers = {'gitlab.chehejia.com': {'PRIVATE-TOKEN': 'synthetic'}}
        reader.deadline = 10;reader.cache = {};reader.opener = Mock()
        with patch.object(discovery.time, 'monotonic', return_value=11), self.assertRaises(TimeoutError):
            reader.get('https://gitlab.chehejia.com/api/v4/example', 100)
        reader.opener.open.assert_not_called()


class BuildReviewTests(unittest.TestCase):
    def test_historical_naming_is_not_a_default_for_new_release(self):
        result = discovery.discover({'repository': 'uroad', 'branch': 'feature/new-branch'}, Reader())
        with self.assertRaisesRegex(ValueError, 'Explicit software version'):
            discovery.common_inputs(result)

    def test_packaged_component_in_locked_libs_is_blocked_before_ci(self):
        value = context()
        value.update(integration_package_names=['uroad_m100', 'xlidar'], integration_package_list_sha256='6'*64)
        value['common_locked_refs'] = ['xlidar/1.0@livc/stable']
        with self.assertRaises(ValueError): workflow.make_plan(value)
        value['common_app_refs'].append(value['common_locked_refs'].pop())
        self.assertEqual(workflow.make_plan(value)['jenkins_modes'], ['OFF'])

    def test_final_confirmation_binds_user_session_revision_head_and_displayed_parameters(self):
        plan = workflow.make_plan(context())
        review = workflow.build_review(plan, 'owner-a', 'session-a', 2)
        args = dict(actor='owner-a', session='session-a', revision=2,
                    shown_sha=review['review_sha256'], current_source=plan['source'])
        before = copy.deepcopy(review)
        self.assertEqual(workflow.confirm_build_review(review, **args), plan)
        self.assertEqual(review, before)
        for changes in ({'actor': 'owner-b'}, {'session': 'session-b'}, {'revision': 3},
                        {'shown_sha': '0'*64}, {'current_source': dict(plan['source'], commit='f'*40)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                workflow.confirm_build_review(review, **dict(args, **changes))
        altered = copy.deepcopy(review)
        altered['plan']['parameters']['OFF']['BASE_LINE'] = '00FF'
        with self.assertRaises(ValueError): workflow.confirm_build_review(altered, **args)

    def test_confirm_before_compilation_does_not_allow_early_jenkins_submission(self):
        value = context(published=False); value['compile_evidence'] = {}
        pending = workflow.make_plan(value)
        review = workflow.build_review(pending, 'a', 's', 1)
        approved = workflow.confirm_build_review(review, actor='a', session='s', revision=1,
                shown_sha=review['review_sha256'], current_source=pending['source'])
        with self.assertRaises(ValueError): workflow.require_app_review(pending, 'OFF', pending['plan_sha256'])
        ready = workflow.make_plan(context())
        self.assertEqual(workflow.ready_parameters_after_confirmation(approved, ready, 'OFF'), ready['parameters']['OFF'])
        changed = context(); changed['common_parameters']['BASE_LINE'] = '00FF'
        with self.assertRaises(ValueError): workflow.ready_parameters_after_confirmation(approved, workflow.make_plan(changed), 'OFF')
        changed = context(); changed['components']['OFF']['recipe_sha256'] = '9'*64
        changed['compile_evidence']['OFF']['recipe_sha256'] = '9'*64
        with self.assertRaises(ValueError): workflow.ready_parameters_after_confirmation(approved, workflow.make_plan(changed), 'OFF')

    def test_unverified_baseline_cannot_be_confirmed(self):
        value = context(); value['baseline_artifact_checks']['verified'] = False
        with self.assertRaises(ValueError): workflow.build_review(workflow.make_plan(value), 'a', 's', 1)


if __name__ == '__main__': unittest.main()
