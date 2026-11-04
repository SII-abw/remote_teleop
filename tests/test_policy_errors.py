"""Policy failures retain precise, credential-free causes and no motion authority."""

from collections import deque
import json
from types import SimpleNamespace
import threading
import unittest

from remote_teleop.config import AppConfig
from remote_teleop.control import Controller, Mode, Rejected
from remote_teleop.policy import PolicyRunner
from remote_teleop.runtime import ClientRuntime


class PolicyErrorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig()
        self.now = 100.0
        self.c = Controller(self.cfg, lambda: self.now)
        self.q = [0.0] * 14
        self.c.recording = True
        self.c.heartbeat(True)
        self.c.request('arm', self.q, True)
        self.c.request('start', self.q, True)
        self.c.mode_ready(self.c.epoch, self.q)

    def packet(self):
        return {'type': 'policy_error', 'epoch': self.c.epoch, 'seq': 1,
                'observation_id': self.c.observe(),
                'error': {'error_type': 'InvalidStatus', 'http_status': 404, 'error_code': None}}

    def test_http_404_stops_immediately_with_specific_cause(self):
        self.c.offer(self.packet(), 'robot')
        self.assertEqual((self.c.mode, self.c.reason), (Mode.HOLD, 'POLICY_SERVICE_ERROR'))
        self.assertEqual(self.c.status()['policy_error']['http_status'], 404)
        self.assertIsNone(self.c.tick(self.q, True))

    def test_error_source_checked_for_each_runner_location(self):
        for location in ('robot', 'client', 'cloud'):
            self.setUp()
            self.cfg.policy.runner_location = location
            with self.assertRaisesRegex(Rejected, 'WRONG_SOURCE'):
                self.c.offer(self.packet(), 'wrong')
            self.c.offer(self.packet(), location)
            self.assertEqual(self.c.reason, 'POLICY_SERVICE_ERROR')

    def test_error_from_previous_epoch_cannot_interrupt_human(self):
        old = self.packet()
        self.c.offer({**old, 'type': 'policy_action', 'action': self.q}, 'robot')
        self.c.request('toggle', self.q, True)
        self.c.mode_ready(self.c.epoch, self.q)
        with self.assertRaisesRegex(Rejected, 'STALE_EPOCH'):
            self.c.offer(old, 'robot')
        self.assertEqual(self.c.mode, Mode.HUMAN)

    def test_error_details_are_validated_and_extra_data_never_exposed(self):
        packet = self.packet()
        packet['error']['message'] = 'wss://private.invalid/?token=secret'
        self.c.offer(packet, 'robot')
        self.assertNotIn('secret', json.dumps(self.c.status()))
        for bad in ('wss://private.invalid/?token=secret', '', 'x' * 65):
            self.setUp()
            packet = self.packet()
            packet['error']['error_type'] = bad
            with self.assertRaisesRegex(Rejected, 'INVALID_POLICY_ERROR'):
                self.c.offer(packet, 'robot')

    def test_reset_clears_old_policy_error_without_starting(self):
        self.c.offer(self.packet(), 'robot')
        self.c.request('reset', self.q, True)
        self.assertIsNone(self.c.status()['policy_error'])
        self.assertEqual(self.c.mode, Mode.DISARMED)

    def test_runner_emits_only_safe_error_metadata(self):
        cfg = AppConfig()
        cfg.policy.backend = 'openpi'
        done = threading.Event()
        packets = []

        class InvalidStatus(RuntimeError):
            response = SimpleNamespace(status_code=404)

        class Client:
            _action_queue = deque()
            def predict_action(self, **kwargs):
                raise InvalidStatus('wss://private.invalid/?token=secret')
            def reset_action_queue(self):
                self._action_queue.clear()
            def _close(self):
                pass

        def emit(packet):
            packets.append(packet)
            if packet['type'] == 'policy_error':
                done.set()

        runner = PolicyRunner(cfg, 'robot', emit)
        runner._create_client = Client
        runner.update({'status': {'mode': 'POLICY_ACTIVE', 'epoch': 'current', 'runner_location': 'robot'},
                       'q': self.q, 'images': {}, 'observation_id': 'observed'})
        runner.start()
        try:
            self.assertTrue(done.wait(2))
        finally:
            runner.close()
        error = next(packet for packet in packets if packet['type'] == 'policy_error')
        self.assertEqual(error['error']['http_status'], 404)
        self.assertNotIn('secret', json.dumps(packets))
        self.assertFalse(any(packet['type'] == 'policy_action' for packet in packets))

    def test_disabling_velocity_limit_keeps_acceleration_and_position_checks(self):
        self.cfg.safety.velocity_max = [0.01] * 7
        self.cfg.safety.acceleration_max = [0.16] * 7
        self.cfg.safety.checks.velocity_limits = False
        target = self.q.copy()
        target[0] = 0.4
        peak, previous_velocity = 0.0, 0.0
        for seq in range(1, 101):
            self.now += 0.01
            self.c.heartbeat(True)
            self.c.offer({'type': 'policy_action', 'epoch': self.c.epoch, 'seq': seq,
                          'observation_id': self.c.observe(), 'action': target}, 'robot')
            applied = self.c.tick(self.q, True)
            self.assertIsNotNone(applied)
            self.assertLessEqual(abs(self.c.velocity[0] - previous_velocity), 0.0016 + 1e-9)
            previous_velocity = self.c.velocity[0]
            peak = max(peak, previous_velocity)
            self.q = applied.action
        self.assertGreater(peak, 0.05)
        self.assertFalse(self.c.status()['velocity_limits_enabled'])
        illegal = target.copy()
        illegal[0] = 4.0
        with self.assertRaisesRegex(Rejected, 'POSITION_LIMIT'):
            self.c.offer({'type': 'policy_action', 'epoch': self.c.epoch, 'seq': 102,
                          'observation_id': self.c.observe(), 'action': illegal}, 'robot')

    def test_leader_velocity_limiter_uses_the_same_switch(self):
        self.cfg.safety.checks.velocity_limits = False
        runtime = ClientRuntime(SimpleNamespace(current=self.cfg))
        target = self.q.copy()
        target[0] = 0.4
        self.assertEqual(runtime._feedback_step(self.q, target), target)


if __name__ == '__main__':
    unittest.main()
