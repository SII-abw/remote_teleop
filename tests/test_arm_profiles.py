"""Arm role contracts, migration and inert persisted profile libraries."""

import asyncio
import copy
import json
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

from remote_teleop.arm_profiles import builtin_arm_profiles
from remote_teleop.config import AppConfig, ArmProfiles, ConfigStore, atomic_json
from remote_teleop.config_sync import ConfigSynchronizer, ROLES
from remote_teleop.security import Credentials, create_credentials
from remote_teleop.webapp import create_client_app


def custom_catalog():
    catalog = builtin_arm_profiles()
    for profiles in catalog.values():
        for profile in profiles:
            profile["id"] = f"custom-{profile['id']}"
    return catalog


async def asgi_request(app, path, *, method="GET", body=None, cookie=""):
    """Exercise the real ASGI middleware without optional HTTP client packages."""
    encoded = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"localhost"), (b"content-length", str(len(encoded)).encode())]
    if body is not None:
        headers.extend([(b"content-type", b"application/json"), (b"x-teleop-request", b"1")])
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 80),
    }
    received, messages, complete = False, [], asyncio.Event()

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        await complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            complete.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    start = next(message for message in messages if message["type"] == "http.response.start")
    content = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return SimpleNamespace(
        status_code=start["status"],
        headers={key.decode(): value.decode() for key, value in start["headers"]},
        payload=json.loads(content),
    )


class ArmProfileTests(unittest.TestCase):
    def test_builtins_enforce_roles_and_verified_jointstate_mappings(self):
        catalog = builtin_arm_profiles()
        self.assertEqual([p["config"]["driver"] for p in catalog["robot"]], ["eaiarm", "arx5"])
        self.assertTrue(all(p["config"]["backend"] == "ros" for p in catalog["robot"]))
        self.assertEqual([p["config"]["backend"] for p in catalog["leader"]], ["ros", "sii_serial"])
        profiles = ArmProfiles.model_validate(custom_catalog())
        self.assertEqual(profiles.robot[1].config.arms[0].gripper_feedback_scale, 0.02)
        self.assertFalse(profiles.robot[1].config.arms[0].mode_switch_enabled)
        self.assertTrue(profiles.robot[0].config.arms[0].mode_switch_enabled)
        self.assertFalse(profiles.leader[1].config.passive_supported)
        self.assertFalse(profiles.leader[1].config.serial_support_validated)
        self.assertEqual(profiles.leader[0].config.arms[0].joint_names, [f"joint_{i}" for i in range(1, 8)])
        catalog["robot"][0]["config"]["arms"][0]["state_topic"] = "/changed"
        self.assertEqual(
            builtin_arm_profiles()["robot"][0]["config"]["arms"][0]["state_topic"], "/left/joint_states"
        )

    def test_legacy_arm_modes_and_ros_domain_are_preserved(self):
        raw = {
            "robot": {"ros_domain_id": 42},
            "leader": {
                "arms": [
                    {
                        "name": side,
                        "state_topic": f"/{side}/joint_states",
                        "target_topic": f"/{side}/targets",
                        "mode_topic": f"/{side}/mode",
                    }
                    for side in ("left", "right")
                ]
            },
        }
        original = copy.deepcopy(raw)
        cfg = AppConfig.model_validate(raw)
        self.assertEqual(raw, original)
        self.assertEqual(cfg.leader.ros_domain_id, 42)
        self.assertEqual(cfg.robot.driver, "arx5")
        self.assertEqual(cfg.leader.driver, "eaiarm")
        self.assertFalse(cfg.robot.arms[0].mode_switch_enabled)
        self.assertTrue(cfg.leader.arms[0].mode_switch_enabled)
        raw["leader"]["ros_domain_id"] = 7
        self.assertEqual(AppConfig.model_validate(raw).leader.ros_domain_id, 7)
        self.assertEqual(AppConfig.model_validate_json(cfg.model_dump_json()), cfg)

    def test_legacy_leader_inherits_same_side_joint_names_without_aliasing(self):
        raw = AppConfig().model_dump()
        for arm in raw["robot"]["arms"]:
            arm["joint_names"] = [f"{arm['name']}_motor_{i}" for i in range(7)]
        for arm in raw["leader"]["arms"]:
            arm.pop("joint_names")
        original = copy.deepcopy(raw)
        migrated = AppConfig.model_validate(raw)
        self.assertEqual(raw, original)
        self.assertEqual(migrated.leader.arms[0].joint_names, migrated.robot.arms[0].joint_names)
        self.assertEqual(migrated.leader.arms[1].joint_names, migrated.robot.arms[1].joint_names)
        self.assertIsNot(migrated.leader.arms[0].joint_names, migrated.robot.arms[0].joint_names)
        raw["leader"]["arms"][0]["joint_names"] = [f"leader_{i}" for i in range(7)]
        explicit = AppConfig.model_validate(raw)
        self.assertEqual(explicit.leader.arms[0].joint_names, [f"leader_{i}" for i in range(7)])
        del raw["leader"]
        self.assertEqual(
            AppConfig.model_validate(raw).leader.arms[0].joint_names, raw["robot"]["arms"][0]["joint_names"]
        )

    def test_dormant_profiles_validate_structurally_without_enabling_hardware(self):
        cfg = AppConfig.model_validate({"arm_profiles": custom_catalog()})
        self.assertTrue(cfg.general.simulation)
        self.assertEqual(cfg.robot.backend, "sim")
        self.assertEqual(cfg.leader.backend, "sim")
        self.assertEqual(cfg.arm_profiles.leader[1].config.backend, "sii_serial")
        cfg.leader = cfg.arm_profiles.leader[1].config
        with self.assertRaisesRegex(ValueError, "simulation must use simulated"):
            AppConfig.model_validate(cfg.model_dump())

    def test_profile_count_and_selection_are_bounded_per_role(self):
        catalog = custom_catalog()
        template = catalog["robot"][0]
        catalog["robot"] = [{**template, "id": f"custom-{i}"} for i in range(33)]
        with self.assertRaises(ValueError):
            ArmProfiles.model_validate(catalog)
        ArmProfiles.model_validate({"selected_robot": "arx5-ros", "selected_leader": "printed-serial"})

    def test_invalid_saved_mappings_and_follower_backends_are_rejected(self):
        edits = [
            lambda c: c["robot"][0]["config"].update(backend="sim"),
            lambda c: c["robot"][0]["config"].update(backend="sii_serial"),
            lambda c: c["robot"][0]["config"]["arms"][0].update(state_topic=" "),
            lambda c: c["leader"][0]["config"]["arms"][0].update(target_topic=""),
            lambda c: c["leader"][0]["config"]["arms"][0].update(joint_names=["same"] * 7),
            lambda c: c["robot"][0]["config"]["arms"].reverse(),
            lambda c: c["leader"][1]["config"]["arms"][0].update(motor_ids=[1] * 7),
            lambda c: c["robot"].append(copy.deepcopy(c["robot"][0])),
            lambda c: c["leader"][0]["config"]["arms"][0].update(mode_topic=""),
            lambda c: c["leader"][0]["config"]["arms"][0].update(
                mode_message_type="int32", mode_mit_value=1.5
            ),
            lambda c: c["leader"][0]["config"]["arms"][0].update(mode_message_type="bool", mode_mit_value=2),
            lambda c: c["robot"][0].update(id="eaiarm-ros"),
            lambda c: c.update(selected_robot="does-not-exist"),
            lambda c: c["robot"][0]["config"].update(driver="arx5"),
            lambda c: c["leader"][0]["config"].update(driver="arx5"),
            lambda c: c["leader"][0]["config"]["arms"][0].update(mode_message_type="int32"),
            lambda c: c["leader"][0]["config"]["arms"][0].update(mode_position_value=2),
        ]
        for edit in edits:
            with self.subTest(edit=edit):
                catalog = custom_catalog()
                edit(catalog)
                with self.assertRaises(ValueError):
                    ArmProfiles.model_validate(catalog)

    def test_custom_mode_protocol_uses_explicit_topic_acknowledgement(self):
        catalog = custom_catalog()
        leader = catalog["leader"][0]["config"]
        leader["driver"] = "arx5"
        for arm in leader["arms"]:
            arm.update(
                mode_message_type="int32",
                mode_position_value=3,
                mode_mit_value=8,
                mode_confirmation="topic",
                mode_state_topic=f"/{arm['name']}/mode_state",
            )
        validated = ArmProfiles.model_validate(catalog)
        self.assertEqual(validated.leader[0].config.arms[0].mode_mit_value, 8)

    def test_disabled_modes_need_no_mode_topics_or_acknowledgements(self):
        cfg = AppConfig()
        cfg.general.simulation = False
        cfg.robot.backend, cfg.robot.stop_strategy = "ros", "position_hold"
        cfg.leader.backend = "ros"
        for arm in cfg.leader.arms:
            arm.mode_switch_enabled, arm.mode_topic = False, ""
        for camera in cfg.cameras:
            camera.backend, camera.device = "usb", "/dev/video0"
        cfg.policy.backend = "openpi"
        cfg.network.gateway_url = "wss://localhost:8800"
        validated = AppConfig.model_validate(cfg.model_dump())
        validated.leader.arms[0].mode_switch_enabled = True
        validated.leader.arms[0].mode_topic = "/left/mode"
        with self.assertRaisesRegex(ValueError, "requires mode acknowledgement"):
            AppConfig.model_validate(validated.model_dump())
        cfg.leader = ArmProfiles.model_validate(custom_catalog()).leader[1].config
        with self.assertRaisesRegex(ValueError, "serial leader requires validated"):
            AppConfig.model_validate(cfg.model_dump())
        validated.leader.arms[0].mode_switch_enabled = False
        validated.robot.arms[0].mode_switch_enabled = True
        validated.robot.arms[0].mode_topic = "/left/mode"
        with self.assertRaisesRegex(ValueError, "requires mode acknowledgement"):
            AppConfig.model_validate(validated.model_dump())

    def test_library_save_survives_restart_without_changing_effective_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}")
            store = ConfigStore(path)
            previous = store.current.model_copy(deep=True)
            changed = store.read()
            changed["config"]["arm_profiles"] = custom_catalog()
            result = store.save(changed["config"], changed["revision"])
            self.assertFalse(result["restart_required"])
            self.assertEqual(store.current, previous)
            self.assertEqual(
                store.current.control_contract, AppConfig.model_validate(result["config"]).control_contract
            )
            restored = ConfigStore(path).current
            self.assertEqual(len(restored.arm_profiles.robot), 2)
            self.assertEqual(restored.robot, previous.robot)
            self.assertEqual(restored.leader, previous.leader)
            changed = store.read()
            changed["config"]["leader"]["arms"][0]["target_topic"] = "/different/targets"
            applied = store.save(changed["config"], changed["revision"])
            self.assertTrue(applied["restart_required"])
            self.assertNotEqual(
                store.current.control_contract, AppConfig.model_validate(applied["config"]).control_contract
            )

    def test_profile_catalog_requires_auth_and_schema_exposes_shared_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            cfg = AppConfig()
            cfg.security.credentials_file = str(Path(directory) / "credentials.json")
            create_credentials(cfg.security.credentials_file, cfg.general.robot_id)
            atomic_json(path, cfg.model_dump())
            app = create_client_app(ConfigStore(path), SimpleNamespace())
            token = Credentials(cfg).token("ui")

            async def exercise():
                self.assertEqual((await asgi_request(app, "/api/arm-profiles")).status_code, 401)
                login = await asgi_request(app, "/api/login", method="POST", body={"token": token})
                self.assertEqual(login.status_code, 200)
                cookies = SimpleCookie(login.headers["set-cookie"])
                cookie = "; ".join(f"{key}={value.value}" for key, value in cookies.items())
                response = await asgi_request(app, "/api/arm-profiles", cookie=cookie)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.payload, builtin_arm_profiles())
                response = await asgi_request(app, "/api/config/schema", cookie=cookie)
                self.assertEqual(response.status_code, 200)
                return response.payload

            schema = asyncio.run(exercise())
            self.assertIn("arm_profiles", schema["properties"])
            for model in ("Arm", "LeaderArm"):
                for field in ("mode_switch_enabled", "mode_message_type", "mode_mit_value", "joint_names"):
                    self.assertIn(field, schema["$defs"][model]["properties"])
            self.assertIn("ros_domain_id", schema["$defs"]["Leader"]["properties"])


class ProfileSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_libraries_sync_to_every_role_without_restarting_motion_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            stores = {}
            for index, role in enumerate(ROLES):
                path = Path(directory) / f"{role}.json"
                cfg = AppConfig()
                cfg.ui.port = 18810 + index
                atomic_json(path, cfg.model_dump())
                stores[role] = ConfigStore(path)

            async def read(role):
                return stores[role].read()

            async def save(role, config, revision):
                return stores[role].save(config, revision)

            async def apply(role, revision):
                return stores[role].apply_safety(revision)

            changed = stores["client"].read()
            changed["config"]["arm_profiles"] = custom_catalog()
            changed["config"]["arm_profiles"]["selected_robot"] = "arx5-ros"
            result = await ConfigSynchronizer(read, save, apply).update(
                "client", changed["config"], changed["revision"]
            )
            self.assertEqual(result["restart_targets"], [])
            for role, store in stores.items():
                self.assertEqual(store.current.arm_profiles.selected_robot, "arx5-ros")
                self.assertEqual(
                    store.read()["config"]["arm_profiles"],
                    ArmProfiles.model_validate(changed["config"]["arm_profiles"]).model_dump(),
                )
                self.assertEqual(store.current.robot.backend, "sim")
                self.assertFalse(store.read()["restart_required"])
