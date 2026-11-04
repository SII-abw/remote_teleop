"""Inference latency is distinct from loss of control authority or transport."""

import asyncio
import unittest

from remote_teleop.config import AppConfig
from remote_teleop.control import Controller, Mode, Rejected
from remote_teleop.protocol import Outbox


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def step(self, seconds):
        self.now += seconds


class InferenceWaitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig()
        self.clock = Clock()
        self.control = Controller(self.cfg, self.clock)
        self.q = [0.0] * 14
        self.control.recording = True
        self.control.heartbeat(True)
        self.control.request("arm", self.q, True)
        self.control.request("start", self.q, True)
        self.control.mode_ready(self.control.epoch, self.q)

    def packet(self, kind="policy_action"):
        return dict(type=kind, epoch=self.control.epoch, seq=self.control.last_seq + 1,
                    observation_id=self.control.observe(), action=self.q.copy())

    def activate(self):
        self.control.offer(self.packet(), "robot")
        self.assertEqual(self.control.mode, Mode.POLICY)

    def begin(self):
        request = self.packet("policy_request")
        request.pop("action")
        self.control.offer(request, "robot")
        return request

    def advance(self, seconds):
        self.clock.step(seconds)
        self.control.heartbeat(True)

    def response(self, request):
        return {**request, "type": "policy_action", "action": self.q.copy()}

    def test_500ms_inference_is_normal_and_resumes_on_result(self):
        self.activate()
        old_command_time = self.control.last_command
        request = self.begin()
        self.advance(0.5)
        self.assertIsNone(self.control.tick(self.q, True))
        self.assertEqual(self.control.mode, Mode.POLICY)
        self.assertEqual(self.control.last_command, old_command_time)
        self.assertTrue(self.control.status()["policy_inference_waiting"])
        self.control.offer(self.response(request), "robot")
        self.assertIsNotNone(self.control.tick(self.q, True))
        self.assertFalse(self.control.status()["policy_inference_waiting"])

    def test_initial_request_can_wait_500ms_before_policy_commit(self):
        request = self.begin()
        self.advance(0.5)
        self.assertIsNone(self.control.tick(self.q, True))
        self.assertEqual(self.control.mode, Mode.RESUME)
        self.control.offer(self.response(request), "robot")
        self.assertEqual(self.control.mode, Mode.POLICY)

    def test_inference_timeout_is_bounded_and_has_specific_reason(self):
        self.activate()
        self.begin()
        self.advance(0.751)
        self.assertIsNone(self.control.tick(self.q, True))
        self.assertEqual((self.control.mode, self.control.reason), (Mode.HOLD, "POLICY_INFERENCE_TIMEOUT"))

    def test_late_result_cannot_win_race_with_watchdog_tick(self):
        self.activate()
        request = self.begin()
        self.advance(0.751)
        with self.assertRaisesRegex(Rejected, "POLICY_INFERENCE_TIMEOUT"):
            self.control.offer(self.response(request), "robot")
        self.assertEqual(self.control.mode, Mode.HOLD)

    def test_request_retries_cannot_extend_deadline_without_action_progress(self):
        self.activate()
        request = self.begin()
        deadline = self.control.inference_wait["deadline"]
        self.advance(0.3)
        retry = self.packet("policy_request")
        retry["seq"] += 100
        with self.assertRaisesRegex(Rejected, "POLICY_REQUEST_NO_PROGRESS"):
            self.control.offer(retry, "robot")
        self.assertEqual(self.control.inference_wait["deadline"], deadline)
        self.control.offer(self.response(request), "robot")
        self.begin()  # A successfully consumed result permits the next request.

    def test_old_chunk_cannot_advance_robot_during_inference_wait(self):
        self.activate()
        old = self.packet()
        self.begin()
        with self.assertRaisesRegex(Rejected, "POLICY_REQUEST_PENDING"):
            self.control.offer(old, "robot")
        self.assertIsNone(self.control.pending)

    def test_normal_command_and_human_watchdog_stay_250ms(self):
        self.activate()
        self.advance(0.251)
        self.control.tick(self.q, True)
        self.assertEqual(self.control.reason, "COMMAND_TIMEOUT")
        self.control.request("reset", self.q, True)
        self.control.request("arm", self.q, True)
        self.control.request("start", self.q, True)
        self.control.mode_ready(self.control.epoch, self.q)
        self.control.offer(self.packet(), "robot")
        self.control.request("toggle", self.q, True)
        self.control.mode_ready(self.control.epoch, self.q)
        self.control.offer(self.packet("human_action"), "client")
        self.advance(0.251)
        self.control.tick(self.q, True)
        self.assertEqual(self.control.reason, "COMMAND_TIMEOUT")

    def test_takeover_invalidates_pending_inference_and_late_results(self):
        self.activate()
        request = self.begin()
        self.control.request("toggle", self.q, True)
        self.assertIsNone(self.control.inference_wait)
        self.control.mode_ready(self.control.epoch, self.q)
        with self.assertRaisesRegex(Rejected, "STALE_EPOCH"):
            self.control.offer(self.response(request), "robot")
        self.assertEqual(self.control.mode, Mode.HUMAN)

    def test_wait_does_not_mask_robot_health_recording_or_leader_failure(self):
        self.activate()
        self.begin()
        self.advance(0.3)
        self.control.recording = False
        self.control.tick(self.q, True)
        self.assertEqual(self.control.reason, "RECORDING_REQUIRED")
        for healthy, reason in [(False, "ROBOT_HEALTH_LOST"), (True, "LEADER_LEASE_EXPIRED")]:
            self.setUp()
            self.activate()
            self.begin()
            self.clock.step(0.501)
            self.control.tick(self.q, healthy)
            self.assertEqual(self.control.reason, reason)

    def test_source_and_fresh_observation_are_required_for_wait_window(self):
        self.activate()
        packet = self.packet("policy_request")
        with self.assertRaisesRegex(Rejected, "WRONG_SOURCE"):
            self.control.offer(packet, "client")
        packet["epoch"] = "old"
        with self.assertRaisesRegex(Rejected, "STALE_EPOCH"):
            self.control.offer(packet, "robot")
        packet["epoch"] = self.control.epoch
        self.advance(0.501)
        self.cfg.safety.checks.command_freshness = False
        with self.assertRaisesRegex(Rejected, "STALE_OBSERVATION"):
            self.control.offer(packet, "robot")
        self.assertIsNone(self.control.inference_wait)

    def test_wait_does_not_authorize_expired_model_observations(self):
        self.cfg.policy.inference_wait_ms = 1500
        self.activate()
        request = self.begin()
        self.advance(1.01)
        with self.assertRaisesRegex(Rejected, "STALE_OBSERVATION"):
            self.control.offer(self.response(request), "robot")

    def test_outbox_request_drops_unsent_tail_and_stop_keeps_priority(self):
        async def scenario():
            outbox = Outbox()
            outbox.put({"type": "policy_action", "seq": 5})
            outbox.put({"type": "policy_request", "seq": 6})
            self.assertNotIn("policy_action", outbox.latest)
            self.assertEqual((await outbox.get())["type"], "policy_request")
            outbox.put({"type": "policy_request", "seq": 7})
            outbox.put({"type": "stop"})
            self.assertEqual((await outbox.get())["type"], "stop")
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
