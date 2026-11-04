"""Client transition regression with measured fake leader; no hardware opened."""
import importlib.util
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace

from remote_teleop.config import AppConfig
from remote_teleop.control import Rejected

candidate = os.environ.get('TELEOP_RUNTIME_CANDIDATE')
if candidate:
    spec = importlib.util.spec_from_file_location('remote_teleop.runtime_candidate', candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ClientRuntime = module.ClientRuntime
else:
    from remote_teleop.runtime import ClientRuntime

class FakeLeader:
    def __init__(self, q, stalled=False):
        self.q, self.manual, self.stalled = q.copy(), True, stalled
        self.commands, self.events = [], []
    def read(self):
        return self.q.copy(), time.monotonic()
    def set_manual(self, manual):
        self.events.append(('mode', manual, self.q.copy()))
        self.manual = manual
    def feedback(self, q):
        assert not self.manual
        self.commands.append(q.copy())
        if not self.stalled:
            self.q = q.copy()

def client(mode='RESUME_PENDING', stalled=False, target=None):
    cfg = AppConfig()
    cfg.safety.velocity_max = [0.08]*6+[0.005]
    cfg.safety.joint_min = [-3.14]*6+[0.0]
    cfg.safety.joint_max = [3.14]*6+[0.085]
    cfg.safety.alignment_tolerance = 0.12
    runtime = object.__new__(ClientRuntime)
    runtime.cfg, runtime.lock = cfg, threading.RLock()
    q = [0.0]*14
    q[1] = -0.18
    runtime.leader = FakeLeader(q, stalled)
    runtime.status = {'q':target or [0.0]*14,'mode':mode,'epoch':'trial'}
    runtime.status_at = runtime.operator_seen_at = time.monotonic()
    runtime.prepared_epoch = runtime.last_epoch = None
    runtime.homing_report = runtime.error = None
    runtime.offset = [0.0]*14
    runtime._cancel_local_homing = lambda reason:None
    runtime._leader_homing = lambda status:False
    messages = []
    runtime.endpoint = SimpleNamespace(send=lambda msg:messages.append(msg))
    class Stop:
        count = 0
        def is_set(self):
            return self.count >= 250 or any(m['type']=='mode_ready' for m in messages)
        def wait(self, _):
            self.count += 1
    runtime.stop_event = Stop()
    return runtime, messages

class AlignmentTests(unittest.TestCase):
    def test_initial_policy_switches_position_then_aligns_before_ack(self):
        runtime, messages = client()
        runtime._leader_loop()
        ack = [m for m in messages if m['type']=='mode_ready']
        self.assertEqual(len(ack),1)
        self.assertFalse(runtime.leader.manual)
        self.assertGreater(len(runtime.leader.commands),1)
        self.assertLessEqual(abs(ack[0]['pose'][1]),0.06)
        self.assertEqual(sum(event[1] is False for event in runtime.leader.events),1)
        previous = -0.18
        for command in runtime.leader.commands:
            self.assertLessEqual(abs(command[1]-previous),0.08/runtime.cfg.leader.feedback_hz+1e-12)
            previous = command[1]
    def test_takeover_does_not_enable_drag_before_alignment(self):
        runtime, messages = client('TAKEOVER_PENDING')
        runtime._leader_loop()
        self.assertEqual(runtime.leader.events[0][1],False)
        self.assertEqual(runtime.leader.events[-1][1],True)
        self.assertLessEqual(abs(runtime.leader.events[-1][2][1]),0.06)
        self.assertTrue(any(m['type']=='mode_ready' for m in messages))
    def test_stalled_leader_never_gets_authority_ack(self):
        runtime, messages = client(stalled=True)
        runtime._leader_loop()
        self.assertFalse(any(m['type']=='mode_ready' for m in messages))
        self.assertEqual(runtime.leader.q[1],-0.18)
    def test_invalid_joint_target_is_rejected_before_mode_switch(self):
        q = [0.0]*14
        q[0] = 3.5
        runtime, messages = client(target=q)
        runtime._leader_loop()
        self.assertEqual(runtime.error,'LEADER_POSITION_LIMIT')
        self.assertEqual(runtime.leader.commands,[])
        self.assertEqual(runtime.leader.events,[])
        self.assertFalse(any(m['type']=='mode_ready' for m in messages))
    def test_closed_gripper_feedback_uses_legal_commands_preserves_measurement(self):
        runtime, _ = client()
        previous, target = [0.0]*14, [0.0]*14
        previous[6], target[6] = -0.0002,-0.0003
        output = runtime._feedback_step(previous,target)
        self.assertEqual(output[6],0.0)
        self.assertEqual(target[6],-0.0003)
        self.assertEqual(previous[6],-0.0002)

if __name__=='__main__':
    unittest.main(verbosity=2)
