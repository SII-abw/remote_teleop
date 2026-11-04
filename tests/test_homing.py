"""Zero return contracts with measured fake devices; never publishes real ROS."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote_teleop.config import AppConfig, ConfigStore, Homing
from remote_teleop.control import Controller, Mode, Rejected
from remote_teleop.homing import HomingSession
from remote_teleop.runtime import RobotRuntime


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def advance(self, dt=0.01):
        self.now += dt


class Publisher:
    def __init__(self, node, topic):
        self.node, self.topic, self.count = node, topic, 2

    def get_subscription_count(self):
        return self.count

    def publish(self, message):
        self.node.messages.append((self.topic, message.data))


class Device:
    def __init__(self, clock, q=None):
        self.clock = clock
        self.q = q or [0.2] * 6 + [0.02] + [0.2] * 6 + [0.02]
        self.commands, self.events, self.messages = [], [], []
        self.stopped, self.manual, self.stale = True, True, False
        self.pubs = {}
        self.io = SimpleNamespace(Int32=lambda: SimpleNamespace(data=0), node=self)

    def read(self):
        return self.q.copy(), self.clock() - (2 if self.stale else 0)

    def enable(self):
        self.events.append("enable")
        self.stopped = False

    def command(self, q):
        if self.stopped:
            raise Rejected("DRIVER_NOT_ENABLED")
        self.commands.append(q.copy())
        self.q = q.copy()

    def set_manual(self, manual):
        self.events.append(("manual", manual))
        self.manual = manual

    def feedback(self, q):
        if self.manual:
            raise Rejected("LEADER_MANUAL")
        self.commands.append(q.copy())
        self.q = q.copy()

    def stop(self):
        self.stopped = True

    def stop_confirmed(self):
        return self.stopped

    def create_publisher(self, message_type, topic, qos):
        self.pubs[topic] = Publisher(self, topic)
        return self.pubs[topic]

    def destroy_publisher(self, pub):
        self.pubs.pop(pub.topic, None)


class HomingTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.clock = AppConfig(), Clock()
        for role in ("robot", "leader"):
            getattr(self.cfg, role).homing.duration_seconds = 1
        self.device = Device(self.clock)

    def native(self):
        self.cfg.leader.backend = "ros"
        self.cfg.leader.homing.reset_topic = "/robot_reset"
        self.cfg.leader.homing.reset_prepare_topic = "/robot_prepare"
        return HomingSession(self.cfg, self.device, "leader", clock=self.clock)

    def advance(self, session, count=1, dt=0.01):
        for _ in range(count):
            self.clock.advance(dt)
            session.tick()

    def test_default_schema_optional_topic_and_limits(self):
        self.assertEqual(Homing().reset_topic, "")
        for values in (
            {"velocity_max": [0] * 7},
            {"acceleration_max": [1]},
            {"duration_seconds": 10, "timeout_seconds": 10},
        ):
            with self.assertRaises(ValueError):
                Homing.model_validate(values)
        dormant = Homing(reset_prepare_topic="/prepare")
        self.assertEqual(dormant.reset_topic, "")

    def test_passive_construction_and_bounded_smooth_trajectory(self):
        session = HomingSession(self.cfg, self.device, "robot", clock=self.clock)
        self.assertEqual(self.device.events, [])
        self.assertEqual(self.device.commands, [])
        session.tick()
        for _ in range(1600):
            self.advance(session)
            if session.state == "COMPLETE":
                break
        self.assertEqual(session.state, "COMPLETE")
        self.assertTrue(self.device.stopped)
        self.assertEqual(self.device.commands[-1], [0] * 14)
        samples = [[0.2] * 6 + [0.02] + [0.2] * 6 + [0.02]] + self.device.commands
        velocities = [
            [(b - a) / 0.01 for a, b in zip(left, right)] for left, right in zip(samples, samples[1:])
        ]
        for row in velocities:
            self.assertTrue(
                all(abs(v) <= self.cfg.robot.homing.velocity_max[i % 7] + 1e-6 for i, v in enumerate(row))
            )
        for left, right in zip(velocities, velocities[1:]):
            self.assertTrue(
                all(
                    abs(b - a) / 0.01 <= self.cfg.robot.homing.acceleration_max[i % 7] + 1e-4
                    for i, (a, b) in enumerate(zip(left, right))
                )
            )

    def test_scheduler_stall_does_not_jump_trajectory(self):
        session = HomingSession(self.cfg, self.device, "robot", clock=self.clock)
        session.tick()
        self.advance(session, 30)
        previous = self.device.commands[-1]
        self.advance(session, dt=1)
        self.assertTrue(
            all(
                abs(a - b) <= self.cfg.robot.homing.velocity_max[i % 7] * 0.02
                for i, (a, b) in enumerate(zip(previous, self.device.commands[-1]))
            )
        )

    def test_limits_missing_feedback_and_commissioning_rejected_before_motion(self):
        for kind in ("zero", "pose", "stale", "range", "gripper"):
            self.setUp()
            if kind == "zero":
                self.cfg.safety.joint_min[0] = 0.1
            if kind == "pose":
                self.device.q[0] = 4
            if kind == "stale":
                self.device.stale = True
            if kind in ("range", "gripper"):
                self.cfg.safety.attended_test.enabled = True
                if kind == "gripper":
                    self.cfg.safety.attended_test.gripper_commands = [0.01, 0]
            session = HomingSession(self.cfg, self.device, "robot", clock=self.clock)
            with self.assertRaises(Rejected):
                session.tick()
            self.assertEqual(self.device.commands, [])
            self.assertEqual(self.device.events, [])

    def test_native_reset_matches_both_subscribers_and_exits_twice(self):
        session = self.native()
        session.tick()
        self.assertEqual(self.device.messages, [])
        self.device.pubs["/robot_reset"].count = 1
        self.advance(session)
        self.assertEqual(self.device.messages, [])
        self.device.pubs["/robot_reset"].count = 2
        self.advance(session)
        self.assertEqual(self.device.messages, [("/robot_reset", 0), ("/robot_prepare", 0)])
        self.advance(session, dt=0.3)
        self.assertIn(("/robot_reset", 1), self.device.messages)
        self.device.q = [0] * 14
        self.advance(session, 3)
        self.assertEqual(session.phase, "RELEASING")
        self.advance(session, dt=2.01)
        self.assertEqual(session.state, "COMPLETE")
        self.assertEqual([v for topic, v in self.device.messages if topic == "/robot_reset"], [0, 1, 0, 0])
        self.assertEqual(self.device.commands, [])
        self.assertEqual(self.device.events, [])
        session.close()
        self.assertEqual(self.device.pubs, {})

    def test_native_stale_feedback_and_cancel_always_exit(self):
        session = self.native()
        session.tick()
        self.advance(session)
        self.advance(session, dt=0.3)
        self.device.stale = True
        with self.assertRaisesRegex(Rejected, "HOMING_STALE_STATE"):
            self.advance(session)
        session.cancel("feedback lost", failed=True)
        self.assertEqual(session.state, "FAILED")
        self.assertEqual(self.device.messages[-2:], [("/robot_reset", 0)] * 2)
        before = list(self.device.messages)
        session.tick()
        self.assertEqual(self.device.messages, before)

    def test_native_reset_does_not_bypass_trial_velocity_limits(self):
        session = self.native()
        self.cfg.safety.attended_test.enabled = True
        self.cfg.safety.attended_test.velocity_cap = 0.05
        with self.assertRaisesRegex(Rejected, "HOMING_NATIVE_LIMIT_UNVERIFIED"):
            session.tick()
        self.assertEqual(self.device.messages, [])

    def test_native_refuses_missing_subscribers_and_repeated_feedback(self):
        session = self.native()
        session.tick()
        self.advance(session)
        self.advance(session, dt=0.3)
        self.device.q = [0] * 14
        self.advance(session)
        for _ in range(5):
            session.tick()
        self.assertNotEqual(session.phase, "RELEASING")
        self.device.pubs["/robot_reset"].count = 1
        with self.assertRaisesRegex(Rejected, "HOMING_RESET_SUBSCRIBERS"):
            self.advance(session)
        session.cancel()
        self.assertEqual(self.device.messages[-1], ("/robot_reset", 0))

    def test_authority_loss_cancels_leader_and_prevents_further_targets(self):
        allowed = [True]

        def guard():
            if not allowed[0]:
                raise Rejected("cancelled")

        session = HomingSession(self.cfg, self.device, "leader", guard=guard, clock=self.clock)
        session.tick()
        self.advance(session, 10)
        allowed[0] = False
        with self.assertRaises(Rejected):
            self.advance(session)
        session.cancel()
        total = len(self.device.commands)
        self.advance(session, 10)
        self.assertEqual(len(self.device.commands), total)
        self.assertTrue(self.device.manual)

    def test_cancellation_cleanup_failure_is_reported_without_raising(self):
        session = self.native()
        session.tick()
        self.advance(session)
        self.advance(session, dt=0.3)

        def broken(_):
            raise RuntimeError("driver unavailable")

        self.device.pubs["/robot_reset"].publish = broken
        session.cancel()
        self.assertEqual(session.state, "FAILED")
        self.assertIn("HOMING_EXIT_FAILED", session.last_error)


class HomingAuthorityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text("{}")
        self.store = ConfigStore(path)
        r = self.runtime = RobotRuntime.__new__(RobotRuntime)
        r.cfg, r.store = self.store.current, self.store
        r.control = Controller(r.cfg)
        r.driver = Device(time.monotonic)
        r.lock = threading.RLock()
        r.supervisor_fault = threading.Event()
        r.recorder = SimpleNamespace(state="IDLE")
        r.record_job = None
        r.homing = r.homing_status = r.leader_home_report = None
        r.home_operator_at = time.monotonic()
        r.home_operator_ok = True

    def test_homing_excludes_arm_and_returns_to_disarmed_on_cancel(self):
        r = self.runtime
        job = r._operation("home", {"target": "robot", "epoch": r.control.epoch})
        self.assertEqual(r.control.mode, Mode.HOMING)
        with self.assertRaisesRegex(Rejected, "HOMING_BUSY"):
            r._operation("arm", {"epoch": r.control.epoch})
        r._advance_homing()
        self.assertFalse(r.driver.stopped)
        r._operation("home_cancel", {"epoch": r.control.epoch})
        self.assertEqual(r.control.mode, Mode.DISARMED)
        self.assertEqual(r.homing_status["state"], "CANCELLED")
        self.assertTrue(r.driver.stopped)
        self.assertEqual(job["id"], r.homing_status["id"])

    def test_control_loop_releases_authority_lock_before_waiting(self):
        r = self.runtime
        acquired = []

        class ProbeEvent:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                def probe():
                    available = r.lock.acquire(blocking=False)
                    acquired.append(available)
                    if available:
                        r.lock.release()

                thread = threading.Thread(target=probe)
                thread.start()
                thread.join(timeout=1)
                self.stopped = True

        r.stop_event = ProbeEvent()
        r.control.mode = Mode.HOMING
        r._advance_homing = lambda: None
        r._control_loop()
        self.assertEqual(acquired, [True])

    def test_homing_preconditions_reject_without_enabling(self):
        r = self.runtime
        for change, error in (
            (lambda: setattr(r.control, "mode", Mode.READY), "DISARM_REQUIRED"),
            (lambda: setattr(r.recorder, "state", "RECORDING"), "RECORDER_BUSY"),
            (lambda: setattr(r, "home_operator_ok", False), "HOMING_OPERATOR_REQUIRED"),
        ):
            r.control.mode = Mode.DISARMED
            r.recorder.state = "IDLE"
            r.home_operator_ok = True
            change()
            with self.assertRaisesRegex(Rejected, error):
                r._operation("home", {"epoch": r.control.epoch, "target": "robot"})
        self.assertEqual(r.driver.events, [])

    def test_operator_loss_aborts_and_never_autostarts_again(self):
        r = self.runtime
        r._operation("home", {"epoch": r.control.epoch, "target": "robot"})
        r._advance_homing()
        r.home_operator_ok = False
        r._advance_homing()
        self.assertEqual(r.control.mode, Mode.HOLD)
        self.assertEqual(r.homing_status["state"], "FAILED")
        self.assertTrue(r.driver.stopped)

    def test_old_leader_report_cannot_complete_new_job(self):
        r = self.runtime
        r._operation("home", {"epoch": r.control.epoch, "target": "leader"})
        self.assertIsNone(r.leader_home_report)
        r._advance_homing()
        self.assertEqual(r.control.mode, Mode.HOMING)
        asyncio.run(
            r.on_message(
                {
                    "type": "leader_heartbeat",
                    "healthy": True,
                    "operator_healthy": True,
                    "homing": {"id": "old-home", "state": "COMPLETE"},
                }
            )
        )
        self.assertIsNone(r.leader_home_report)
        asyncio.run(
            r.on_message(
                {
                    "type": "leader_heartbeat",
                    "healthy": True,
                    "operator_healthy": True,
                    "homing": {"id": r.homing_status["id"], "state": "COMPLETE"},
                }
            )
        )
        r._advance_homing()
        self.assertEqual(r.control.mode, Mode.DISARMED)
        self.assertEqual(r.homing_status["state"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
