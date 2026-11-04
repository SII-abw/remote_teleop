/* Run with TELEOP_TEST_PYTHON=<project Python> node tests/arm_profiles_ui.cjs.
 * Exercises the shipped frontend against its real configuration schema/catalogue.
 * A small DOM stub covers state, event and persistence contracts; browser layout
 * acceptance is separate. No network, hardware or service configuration is used.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const { webcrypto } = require("node:crypto");
const root = path.resolve(__dirname, "..");
const fixture = process.env.TELEOP_TEST_FIXTURE ? JSON.parse(fs.readFileSync(process.env.TELEOP_TEST_FIXTURE, "utf8")) : JSON.parse(execFileSync(process.env.TELEOP_TEST_PYTHON || "python3", ["-c", `
import json
from remote_teleop.config import AppConfig
from remote_teleop.arm_profiles import builtin_arm_profiles
print(json.dumps(dict(config=AppConfig().model_dump(), schema=AppConfig.model_json_schema(), profiles=builtin_arm_profiles())))
`], { cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, "src") } }));
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
    this.className = ""; this._value = ""; this.validityMessage = "";
    this.classList = {
      toggle: (name, force) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !values.has(name) : force;
        add ? values.add(name) : values.delete(name);
        this.className = [...values].join(" ");
      },
      add: (name) => this.classList.toggle(name, true),
      remove: (name) => this.classList.toggle(name, false),
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }
  append(...items) { for (const item of items) { item.parentElement = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(key, value) { this[key] = value; }
  removeAttribute(key) { delete this[key]; }
  setCustomValidity(message) { this.validityMessage = message; }
  reportValidity() {
    return this.disabled || (!this.validityMessage && (!this.required || this.value !== "") && this.children.every((child) => child.reportValidity()));
  }
  get value() { return this._value; }
  set value(value) {
    this._value = String(value ?? "");
    if (this.tagName === "SELECT" && !descendants(this).some((item) => item.tagName === "OPTION" && item.value === this._value)) this._value = "";
  }
}
function descendants(element) { return element.children.flatMap((child) => [child, ...descendants(child)]); }
const body = new Element("body");
const document = {
  body, documentElement: {}, createElement: (tag) => new Element(tag), addEventListener() {},
  getElementById: (id) => descendants(body).find((element) => element.id === id) || null,
  querySelectorAll: (selector) => descendants(body).filter((element) => selector.startsWith(".")
    ? element.className.split(/\s+/).includes(selector.slice(1))
    : selector.startsWith("[data-") && element.dataset[selector.slice(6, -1)] !== undefined),
};
const html = fs.readFileSync(path.join(root, "src/remote_teleop/web/index.html"), "utf8");
for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const element = new Element(match[1]); element.id = match[2]; body.append(element);
}
const $ = (id) => document.getElementById(id);
$("config-target").append(Object.assign(new Element("option"), { value: "client" }));
$("config-target").value = "client";
$("home-robot").dataset.home = "robot";
$("home-leader").dataset.home = "leader";
$("home-cancel").dataset.control = "home_cancel";
const requests = [];
const context = vm.createContext({
  document, localStorage: { getItem() { return "zh"; }, setItem() {} },
  structuredClone, crypto: webcrypto, confirm: () => true,
  setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  fetch: async (url, request) => {
    const payload = request.body ? JSON.parse(request.body) : undefined;
    requests.push({ url, method: request.method, payload });
    const data = url === "/api/config/schema" ? fixture.schema
      : url === "/api/arm-profiles" ? fixture.profiles
      : url.startsWith("/api/config/") && url !== "/api/config/validate"
        ? { config: structuredClone(payload?.config || fixture.config), revision: "fixture-revision" }
        : {};
    return { ok: true, json: async () => data };
  },
});
const evaluate = (code) => vm.runInContext(code, context);
evaluate(fs.readFileSync(path.join(root, "src/remote_teleop/web/i18n.js"), "utf8").replaceAll("export const", "const"));
evaluate(fs.readFileSync(path.join(root, "src/remote_teleop/web/alerts.js"), "utf8").replace("export class", "class"));
evaluate(fs.readFileSync(path.join(root, "src/remote_teleop/web/app.js"), "utf8").replace(/^import .*\n/gm, ""));
const state = () => JSON.parse(evaluate("JSON.stringify(config)"));
function choose(id) { $("arm-profile-select").value = id; $("arm-profile-select").onchange(); }
function change(id, value) { const input = $(id); assert.ok(input, id); input.type === "checkbox" ? input.checked = value : input.value = value; input.onchange(); }
function name(value) { $("arm-profile-name").value = value; $("arm-profile-name").oninput(); }
(async () => {
  await evaluate("loadConfig()");
  evaluate('category = "robot"; renderConfig()');
  assert.ok(requests.some((request) => request.url === "/api/arm-profiles"));
  assert.ok($("field-robot-backend"));
  assert.equal($("field-robot-driver"), null, "simulation hides hardware fields");
  evaluate('config.general.simulation = false; config.robot = structuredClone(armProfileBuiltins.robot[0].config); renderConfig()');
  assert.equal(descendants($("field-robot-backend")).some((element) => element.value === "sim"), false);
  assert.ok($("field-robot-driver"));
  assert.ok($("field-robot-arms-0-joint_names"));
  assert.ok($("field-robot-arms-0-command_topic"));
  assert.equal($("field-robot-arms-0-port"), null);
  const originalMode = state().robot.arms[0].mode_topic;
  change("field-robot-arms-0-mode_switch_enabled", false);
  assert.equal($("field-robot-arms-0-mode_topic"), null);
  assert.equal(state().robot.arms[0].mode_topic, originalMode, "conditional fields retain data");
  change("field-robot-arms-0-mode_switch_enabled", true);
  change("field-robot-arms-0-mode_confirmation", "topic");
  assert.ok($("field-robot-arms-0-mode_state_topic"));
  assert.equal($("field-robot-arms-0-driver_node"), null);
  change("field-robot-arms-0-mode_confirmation", "openeai_graph");
  assert.ok($("field-robot-arms-0-driver_node"));
  assert.equal($("field-robot-arms-0-mode_state_topic"), null);
  choose("arx5-ros");
  assert.equal(state().robot.driver, "eaiarm", "choosing alone does not replace current draft");
  assert.equal($("arm-profile-update").disabled, true);
  assert.equal($("arm-profile-delete").disabled, true);
  $("arm-profile-apply").onclick();
  assert.equal(state().robot.driver, "arx5");
  assert.equal(state().arm_profiles.selected_robot, "arx5-ros");
  assert.equal($("field-robot-arms-0-mode_topic"), null, "ARX preset mode switching is optional and off");
  change("field-robot-arms-0-state_topic", "/custom/follower/state");
  assert.ok($("arm-profile-status").className.includes("modified"));
  name(""); $("arm-profile-add").onclick();
  assert.equal(state().arm_profiles.robot.length, 0, "empty profile names are rejected");
  name("工位 A 从臂"); $("arm-profile-add").onclick();
  const robotId = state().arm_profiles.selected_robot;
  assert.match(robotId, /^custom-[a-f0-9-]+$/);
  assert.equal(state().arm_profiles.robot[0].config.arms[0].state_topic, "/custom/follower/state");
  assert.equal($("arm-profile-update").disabled, false);
  assert.equal(evaluate('armProfileBuiltins.robot[1].config.arms[0].state_topic'), "/left/ARX_joint_states", "builtins remain unchanged");
  name("工位 A 从臂（更新）"); $("arm-profile-update").onclick();
  assert.equal(state().arm_profiles.robot.length, 1);
  assert.equal(state().arm_profiles.robot[0].name, "工位 A 从臂（更新）");
  evaluate('category = "leader"; config.leader = structuredClone(armProfileBuiltins.leader[0].config); renderConfig()');
  assert.ok($("field-leader-arms-0-state_topic"));
  assert.ok($("field-leader-arms-0-target_topic"));
  assert.ok($("field-leader-ros_domain_id"));
  assert.ok($("field-leader-arms-0-directions"));
  assert.ok($("field-leader-arms-0-offsets"));
  assert.ok($("field-leader-arms-0-gripper_feedback_scale"));
  assert.equal($("field-leader-arms-0-port"), null);
  choose("printed-serial"); $("arm-profile-apply").onclick();
  assert.equal(state().leader.backend, "sii_serial");
  assert.ok($("field-leader-arms-0-port"));
  assert.ok($("field-leader-arms-0-motor_ids"));
  assert.ok($("field-leader-calibration_file"));
  assert.equal($("field-leader-driver"), null);
  assert.equal($("field-leader-arms-0-state_topic"), null);
  assert.equal($("field-leader-arms-0-mode_switch_enabled"), null);
  const port = state().leader.arms[0].port;
  name("打印遥操臂"); $("arm-profile-add").onclick();
  assert.equal(state().arm_profiles.leader.length, 1);
  change("field-leader-backend", "ros");
  assert.ok($("field-leader-driver"));
  assert.equal($("field-leader-arms-0-port"), null);
  assert.equal(state().leader.arms[0].port, port);
  change("field-leader-driver", "arx5");
  assert.equal(state().leader.driver, "arx5");
  change("field-leader-arms-0-joint_names", "not JSON");
  const before = requests.length;
  await $("validate-config").onclick();
  assert.equal(requests.length, before, "invalid visible arrays block deployment validation");
  change("field-leader-arms-0-joint_names", JSON.stringify(state().leader.arms[0].joint_names));
  await $("save-config").onclick();
  const saved = requests.findLast((request) => request.url === "/api/config/client" && request.method === "POST");
  assert.equal(saved.payload.config.arm_profiles.robot.length, 1);
  assert.equal(saved.payload.config.arm_profiles.leader.length, 1);
  assert.equal(saved.payload.config.arm_profiles.selected_robot, robotId);
  assert.equal(state().arm_profiles.robot.length, 1, "server reload retains saved profiles");
  evaluate('category = "robot"; renderConfig()');
  choose(robotId); $("arm-profile-delete").onclick();
  assert.equal(state().arm_profiles.robot.length, 0);
  assert.equal(state().arm_profiles.leader.length, 1);
  assert.equal(state().robot.arms[0].state_topic, "/custom/follower/state", "delete retains effective connection parameters");
  assert.equal(state().arm_profiles.selected_robot, "");
  evaluate('lang = "en"; renderConfig()');
  assert.equal($("arm-profile-apply").textContent, "Load into current draft");
  const ready = { connected: true, telemetry_age_ms: 1, config_in_sync: true, mode: "DISARMED", epoch: "home-epoch", recorder: {state:"IDLE"}, motion_permitted:true, state_age_ms:1, leader_state_age_ms:1, leader_homing_supported:true };
  context.readyForHome = ready;
  evaluate('lastStatus = readyForHome; renderStatus(lastStatus)');
  assert.equal($("home-robot").disabled, false);
  assert.equal($("home-leader").disabled, false);
  assert.equal($("home-cancel").disabled, true);
  const postsBefore = requests.filter((r) => r.url === "/api/control/home").length;
  context.confirm = () => false;
  await $("home-robot").onclick();
  assert.equal(requests.filter((r) => r.url === "/api/control/home").length, postsBefore, "cancelled confirmation sends no homing request");
  context.confirm = () => true;
  await $("home-robot").onclick();
  const homeRequest = requests.findLast((r) => r.url === "/api/control/home");
  assert.equal(homeRequest.payload.target, "robot");
  assert.equal(homeRequest.payload.epoch, "home-epoch");
  evaluate('renderStatus({...readyForHome, mode:"HOMING", homing:{target:"robot",state:"RUNNING",method:"trajectory",progress:.4}})');
  assert.equal($("home-robot").disabled, true);
  assert.equal($("home-leader").disabled, true);
  assert.equal($("home-cancel").disabled, false);
  assert.ok($("homing-status").textContent.includes("40%"));
  evaluate('renderStatus({...readyForHome, leader_homing_supported:false})');
  assert.equal($("home-leader").disabled, true);
  evaluate('renderStatus({...readyForHome, leader_state_age_ms:10000})');
  assert.equal($("home-leader").disabled, true);
  console.log("Arm profile UI contracts passed (schema, transport filters, immutable presets, custom CRUD, draft and save semantics, validation, translations, homing confirmation and controls).");
})().catch((error) => { console.error(error); process.exitCode = 1; });
