"""Repeated private-chat commands retain the current frozen inputs and goal."""
import hashlib
import json
import unittest

from bench_gateway.core import GatewayError
from bench_gateway.intake import Intake
from bench_gateway.service import Service
import test_bindings as fixtures


class RepeatIntakeGoalTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.BindingTests()
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def submit(self, **intent):
        c = self.case
        return c.service.handle(c.session, '执行回灌', {'action': 'replay', **intent})

    def jobs(self):
        return self.case.store.jobs(self.case.user)

    def bind_with_evaluator(self):
        """Explicitly provision the fake capability required by evaluation tests."""
        c=self.case;baseline=c.bind()
        baseline['data']['evaluator']={'argv':['test-reviewed-evaluator-must-not-run']}
        with c.store.connect() as connection:
            connection.execute('UPDATE baselines SET data=? WHERE id=?',
                               (json.dumps(baseline['data']),baseline['id']))
        return baseline

    def test_same_frozen_inputs_and_goal_remain_idempotent_after_restart(self):
        c = self.case
        c.bind()
        self.submit()
        first = self.jobs()[0]
        c.service = Service(c.cfg)
        self.assertIn('已受理过', self.submit())
        self.assertEqual([first['id']], [job['id'] for job in self.jobs()])

    def test_new_preparation_and_scene_scope_accept_same_command(self):
        c = self.case
        old_binding = c.bind()
        self.submit()
        first = self.jobs()[0]
        c.store.finish(first, 'SUCCEEDED', {'task': '/workstation/old-result'})
        Intake(c.store).update(c.user, {**Intake(c.store).get(c.user),'scene_scope': '2'},new=True)
        preparation = c.make_preparation()
        with c.store.connect() as connection:
            row = connection.execute('SELECT result FROM preparations WHERE id=?', (preparation['id'],)).fetchone()
            result = json.loads(row['result'])
            result['scenes'] = [2]
            connection.execute('UPDATE preparations SET result=? WHERE id=?',
                               (json.dumps(result), preparation['id']))
        receipt = {**c.receipt, 'input_digest': 'd' * 64,
                   'path': c.receipt['path'].replace(c.preparation['id'], preparation['id'])}
        new_binding = c.bind(receipt)
        self.assertNotEqual(old_binding['id'], new_binding['id'])
        self.submit()
        jobs = self.jobs()
        self.assertEqual(2, len(jobs))
        new_job = next(job for job in jobs if job['id'] != first['id'])
        self.assertEqual('QUEUED', new_job['state'])
        self.assertEqual([2], new_job['payload']['scenes'])
        self.assertEqual(new_binding['id'], new_job['payload']['baseline']['id'])

    def test_evaluation_choice_is_part_of_submission_identity(self):
        self.bind_with_evaluator()
        self.submit(evaluate_after=False)
        self.submit(evaluate_after=True)
        self.assertEqual(2, len(self.jobs()))
        self.assertEqual({False, True}, {job['payload']['evaluate_after'] for job in self.jobs()})

    def test_saved_evaluation_goal_survives_schema_default_false(self):
        c = self.case
        Intake(c.store).update(c.user, {'goal': 'replay_and_evaluate'})
        c.make_preparation()
        self.bind_with_evaluator()
        self.submit(evaluate_after=False)
        self.assertIs(True, self.jobs()[0]['payload']['evaluate_after'])
        self.assertEqual('replay_and_evaluate', Intake(c.store).get(c.user)['goal'])

    def test_default_goal_still_performs_replay_only(self):
        self.case.bind()
        self.submit(evaluate_after=False)
        self.assertIs(False, self.jobs()[0]['payload']['evaluate_after'])

    def test_explicit_goal_change_is_saved_and_requires_preparation(self):
        c = self.case
        Intake(c.store).update(c.user, {'goal': 'replay_and_evaluate'})
        c.make_preparation()
        c.bind()
        with self.assertRaises(GatewayError):
            c.service.handle(c.session, '改成仅回灌再执行',
                             {'action': 'replay', 'inputs': {'goal': 'replay'}})
        self.assertEqual([], self.jobs())
        self.assertEqual('replay', Intake(c.store).get(c.user)['goal'])

    def test_legacy_text_key_reuses_matching_frozen_request(self):
        c = self.case
        c.bind()
        payload = c.service.prepared_replay(c.user, {'action': 'replay'}, '执行回灌')
        legacy_key = hashlib.sha256((c.session + '\0执行回灌').encode()).hexdigest()
        old, _ = c.store.submit(c.user, c.session, legacy_key, payload)
        self.assertIn('已受理过', self.submit())
        self.assertEqual([old['id']], [job['id'] for job in self.jobs()])

    def test_legacy_text_key_does_not_swallow_different_evaluation_goal(self):
        c = self.case
        self.bind_with_evaluator()
        payload = c.service.prepared_replay(c.user, {'action': 'replay'}, '执行回灌')
        legacy_key = hashlib.sha256((c.session + '\0执行回灌').encode()).hexdigest()
        c.store.submit(c.user, c.session, legacy_key, payload)
        self.submit(evaluate_after=True)
        self.assertEqual(2, len(self.jobs()))

    def test_saved_evaluation_goal_without_an_evaluator_cannot_be_downgraded(self):
        c=self.case
        Intake(c.store).update(c.user,{'goal':'replay_and_evaluate'})
        c.make_preparation();c.bind()
        with self.assertRaisesRegex(GatewayError,'评测入口'):
            self.submit(evaluate_after=False)
        self.assertEqual(self.jobs(),[])
        self.assertEqual(Intake(c.store).get(c.user)['goal'],'replay_and_evaluate')

    def test_dependency_order_cannot_repeat_the_same_frozen_request(self):
        c = self.case
        Intake(c.store).update(c.user, {'goal': 'replay'}, new=True)
        dependencies = list(c.sos.values())
        c.store.add_baseline(c.user, 'legacy', {'gate_verified': True, 'asset_ids': {
            'package': c.package['id'], 'dataset': c.dataset['id'],
            'dependencies': sorted(asset['id'] for asset in dependencies)}})
        names = [asset['name'] for asset in dependencies]
        intent = {'action': 'replay', 'package': 'A', 'dataset': 'D',
                  'baseline': 'legacy', 'dependencies': names, 'scenes': [1, 2]}
        c.service.handle(c.session, '执行已有软件组合回灌', intent)
        c.service.handle(c.session, '执行已有软件组合回灌', {**intent, 'dependencies': names[::-1]})
        self.assertEqual(1, len(self.jobs()))


if __name__ == '__main__':
    unittest.main()
