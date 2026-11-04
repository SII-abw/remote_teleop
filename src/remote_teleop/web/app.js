import { strings, fieldZh } from "./i18n.js";
import { AlertCenter } from "./alerts.js";
const $ = (id) => document.getElementById(id);
let lang = localStorage.getItem("teleop-language") || "zh";
let page = "operate",
  authenticated = false,
  busy = false,
  lastStatus = {},
  config = null,
  activeConfig = null,
  revision = null,
  schema = null,
  category = "general",
  detail = null,
  armProfileBuiltins = { robot: [], leader: [] },
  armProfileChoice = {},
  armProfileNames = {};
const statusStaleMs = 1500;
const pollNow = () => typeof performance !== "undefined" ? performance.now() : Date.now();
let statusReceivedAt = 0, pollPending = false, pollStartedAt = 0, pollController = null, pollGeneration = 0;
let lastPollTickAt = pollNow(), pollGraceUntil = 0, statusNeedsRefresh = false;
let pollFailureAt = null, pollFailureDetail = "";
const urgentPending = new Set();
let recordNameStatus = null,
  recordNamePending = false,
  recordNameRevision = 0,
  recordNameTimer = null,
  recordRequestPending = false;
let taskCatalog = null, taskCatalogPending = false, taskRequestPending = false;
let taskMutationVersion = 0;
const categories = [
  "general",
  "frequencies",
  "network",
  "security",
  "safety",
  "robot",
  "leader",
  "cameras",
  "policy",
  "recording",
  "ui",
];
const frequencyFields = {
  "safety.control_hz": {title: "controlFrequencyTitle", help: "controlFrequencyHelp", rates: [20, 30, 50, 60, 100, 120, 200, 250]},
  "policy.action_hz": {title: "policyFrequencyTitle", help: "policyFrequencyHelp", rates: [10, 20, 30, 50, 60, 100]},
  "leader.feedback_hz": {title: "leaderFrequencyTitle", help: "leaderFrequencyHelp", rates: [10, 20, 30, 50, 60, 100]},
  "network.telemetry_hz": {title: "telemetryFrequencyTitle", help: "telemetryFrequencyHelp", rates: [20, 30, 50, 60, 100]},
  "network.observation_hz": {title: "observationFrequencyTitle", help: "observationFrequencyHelp", rates: [10, 20, 30, 50, 60, 100]},
};
const dangerFields = {
  robot: ["stop_strategy"],
  leader: ["passive_supported", "serial_support_validated"],
  policy: ["request_timeout_seconds", "max_queued_actions", "max_chunk_age_ms"],
  recording: ["queue_size", "minimum_free_gb", "max_episode_seconds", "max_sync_skew_ms", "require_recording_for_hil"],
};
const t = (key) => strings[lang][key] || key;
const alerts = new AlertCenter(t, renderNotice);
function renderNotice(notice, count) {
  const dialog = $("fault-dialog"), wasHidden = dialog.classList.contains("hidden");
  dialog.classList.toggle("hidden", !notice);
  if (!notice) return;
  $("fault-title").textContent = notice.title;
  $("fault-count").textContent = t("faultCount").replace("{count}", count);
  $("fault-message").textContent = notice.message;
  $("fault-detail").textContent = [notice.code, notice.detail].filter(Boolean).join("\n");
  $("fault-recovery").textContent = notice.recovery;
  $("fault-stop").classList.toggle("hidden", !authenticated);
  $("fault-stop").disabled = !authenticated;
  if (wasHidden) $("fault-dismiss").focus?.();
}
$("fault-dismiss").onclick = () => alerts.dismissAll();
$("fault-stop").onclick = () => urgentControl("estop");
$("fault-diagnostics").onclick = () => {
  alerts.dismissAll();
  for (const button of document.querySelectorAll("[data-page]"))
    if (button.dataset.page === "operate") button.onclick();
  $("diagnostics-panel").open = true;
  $("diagnostics-panel").scrollIntoView?.({block: "nearest"});
};
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function toast(message, error = false) {
  if (error) { alerts.request({message}); return; }
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 5500);
}
function translate() {
  document.documentElement.lang = lang;
  document
    .querySelectorAll("[data-i18n]")
    .forEach((e) => (e.textContent = t(e.dataset.i18n)));
  document
    .querySelectorAll("[data-placeholder]")
    .forEach((e) => (e.placeholder = t(e.dataset.placeholder)));
  $("page-title").textContent = t(page);
  $("language").textContent = lang === "zh" ? "English" : "中文";
  if (config) renderConfig();
  if (detail) renderDetail(detail);
  renderStatus(lastStatus);
  alerts.refresh();
}
$("language").onclick = () => {
  lang = lang === "zh" ? "en" : "zh";
  localStorage.setItem("teleop-language", lang);
  translate();
};
async function api(path, method = "GET", body, suppliedController = null) {
  const controller = suppliedController || (typeof AbortController === "undefined" ? null : new AbortController());
  const timeout = controller ? setTimeout(() => controller.abort(), path === "/status" ? 5000 :
    path === "/datasets/inspect" ? 90000 : 35000) : null;
  let response, data;
  try {
    response = await fetch("/api" + path, {
      method,
      headers: { "Content-Type": "application/json", "X-Teleop-Request": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller?.signal,
    });
    try {
      data = await response.json();
      if (data === null || typeof data !== "object") data = {error: "INVALID_SERVER_RESPONSE"};
    }
    catch { data = {error: "INVALID_SERVER_RESPONSE"}; }
  } catch (cause) {
    const error = new Error(t(cause.name === "AbortError" ? "REQUEST_TIMEOUT" : "NETWORK_ERROR"));
    error.code = cause.name === "AbortError" ? "REQUEST_TIMEOUT" : "NETWORK_ERROR";
    throw error;
  } finally { if (timeout !== null) clearTimeout(timeout); }
  if (!response.ok) {
    let notified = false;
    if (response.status === 401) {
      const wasAuthenticated = authenticated;
      authenticated = false;
      alerts.condition("poll-stale", false, "STATUS_STALE", {clearOnRecovery: true});
      pollFailureAt = null;
      pollFailureDetail = "";
      $("login-overlay").classList.remove("hidden");
      stopVideos();
      renderStatus({...lastStatus, connected: false, mode: "OFFLINE", reason: "AUTH_REQUIRED", telemetry_age_ms: null});
      if (wasAuthenticated) { alerts.push(`session:${++alerts.serial}`, "AUTH_REQUIRED"); notified = true; }
    }
    const code = typeof (data.error || data.detail) === "string" ? data.error || data.detail : "REQUEST_FAILED";
    const error = new Error(t(code));
    error.code = code; error.httpStatus = response.status;
    error.notified = notified;
    throw error;
  }
  if (data.error === "INVALID_SERVER_RESPONSE") {
    const error = new Error(t(data.error)); error.code = data.error; throw error;
  }
  return data;
}
async function perform(fn) {
  if (busy) return;
  busy = true;
  document.body.classList.add("busy");
  try {
    await fn();
  } catch (e) {
    alerts.request(e);
  } finally {
    busy = false;
    document.body.classList.remove("busy");
  }
}
function startVideos() {
  for (const name of ["main", "left_hand", "right_hand"]) {
    const image = $("video-" + name);
    image.onerror = () => { if (authenticated) alerts.condition(`image:${name}`, true, "VIDEO_UNAVAILABLE", {detail: t(name)}); };
    image.onload = () => alerts.condition(`image:${name}`, false, "VIDEO_UNAVAILABLE");
    image.src = "/api/video/" + name;
  }
}
function stopVideos() {
  for (const name of ["main", "left_hand", "right_hand"])
    $("video-" + name).removeAttribute("src");
}
$("login-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await api("/login", "POST", { token: $("login-token").value });
    $("login-token").value = "";
    $("login-overlay").classList.add("hidden");
    authenticated = true;
    statusReceivedAt = 0;
    alerts.queue = []; alerts.conditions.clear(); alerts.refresh();
    startVideos();
    poll();
  } catch (e) {
    $("login-error").textContent = e.message;
    alerts.request(e);
  }
};
$("logout").onclick = () => perform(async () => {
  await api("/logout", "POST", {});
  authenticated = false;
  $("login-overlay").classList.remove("hidden");
  stopVideos();
  renderStatus({...lastStatus, connected: false, mode: "OFFLINE", reason: "AUTH_REQUIRED", telemetry_age_ms: null});
});
document.querySelectorAll("[data-page]").forEach(
  (button) =>
    (button.onclick = () => {
      page = button.dataset.page;
      document
        .querySelectorAll(".page")
        .forEach((e) => e.classList.toggle("hidden", e.id !== "page-" + page));
      document
        .querySelectorAll("[data-page]")
        .forEach((e) => e.classList.toggle("selected", e === button));
      $("page-title").textContent = t(page);
      if (page === "configure" && !config) perform(loadConfig);
      if (page === "datasets") perform(loadDatasets);
    }),
);
function renderStatus(status) {
  const operatorPaused = status.mode === "SAFE_HOLD" && status.reason === "OPERATOR_PAUSE";
  if (["RECORDING", "SAVING"].includes(status.recorder?.state) && status.recorder.dataset)
    $("dataset-name").value = status.recorder.dataset;
  const online =
    status.connected &&
    status.telemetry_age_ms !== null &&
    status.telemetry_age_ms < 1500;
  $("connection").textContent = t(online ? "online" : "offline");
  $("connection-dot").classList.toggle("online", online);
  $("control-mode").textContent = t(operatorPaused ? "operatorPaused" : status.mode || "OFFLINE");
  $("reason").textContent = t(online && status.config_in_sync === false ? "CONFIG_NOT_SYNCHRONIZED" :
    status.policy_inference_waiting && ["POLICY_ACTIVE", "RESUME_PENDING"].includes(status.mode) ?
      "POLICY_INFERENCE_WAIT" : status.reason || "OFFLINE");
  $("telemetry-age").textContent = Number.isFinite(status.telemetry_age_ms)
    ? Math.round(status.telemetry_age_ms) + " ms"
    : "—";
  $("recorded-frames").textContent = (
    status.recorder?.frames || 0
  ).toLocaleString();
  $("record-state").textContent = t(status.recorder?.state || "IDLE");
  $("recorded-frames-label").textContent = t(
    status.recorder?.state === "IDLE" && status.recorder?.frames > 0
      ? "lastRecordedFrames" : "recordedFrames",
  );
  $("operation-guide").textContent = t(
    status.operation_mode === "hil" && status.recording_required_for_hil !== false
      ? "hilOperationGuide" : "operationGuide",
  );
  $("runner").textContent = t(status.runner_location || "robot");
  $("leader-health").textContent = t(
    online ? status.leader_error || "leaderHealthy" : "leaderUnavailable",
  );
  const failedJob = status.record_job?.state === "FAILED" ? status.record_job : null;
  const jobApplies = failedJob?.operation !== "record_start" ||
    failedJob?.dataset === $("dataset-name").value;
  const recordError =
    status.recorder?.error ||
    (jobApplies ? failedJob?.error : null);
  $("record-detail").textContent = recordError
    ? t(recordError)
    : status.recorder?.dataset
      ? `${t(status.recorder.state === "IDLE" ? "lastRecording" : "currentRecording")}: ${status.recorder.dataset}`
      : "";
  $("simulation").textContent = t(
    status.simulation === false
      ? "hardware"
      : status.simulation === true
        ? "simulation"
        : "environmentPending",
  );
  $("simulation-controls").classList.toggle(
    "hidden",
    status.simulation !== true,
  );
  $("diagnostics").textContent = JSON.stringify(status, null, 2);
  $("hardware-readiness").classList.toggle(
    "hidden",
    status.simulation !== false,
  );
  $("hardware-readiness").textContent = status.attended_test
    ? attendedSummary(status.attended_limits)
    : status.motion_permitted === false
      ? t("HARDWARE_COMMISSIONING_REQUIRED")
      : status.stop_confirmation === "position_hold"
        ? t("softwareHold")
        : "";
  const disabledLimits = [
    ["position_limits_enabled", "positionLimitLabel"],
    ["velocity_limits_enabled", "velocityLimitLabel"],
    ["acceleration_limits_enabled", "accelerationLimitLabel"],
  ].filter(([field]) => status[field] === false).map(([, label]) => t(label));
  $("velocity-readiness").classList.toggle("hidden", disabledLimits.length === 0);
  $("velocity-readiness").textContent = t("motionLimitsDisabled").replace("{limits}", disabledLimits.join(" / "));
  const waitingGrippers = (status.gripper_takeover || []).filter(item => item.state === "waiting_for_match");
  $("gripper-takeover-guidance").classList.toggle("hidden", waitingGrippers.length === 0);
  $("gripper-takeover-guidance").textContent = waitingGrippers.map(item => t("gripperPickupWaiting")
    .replace("{arm}", t(item.arm === "left" ? "transitionLeftGripper" : "transitionRightGripper"))
    .replace("{leader}", (item.leader_m * 1000).toFixed(1))
    .replace("{target}", (item.target_m * 1000).toFixed(1))
    .replace("{pickup}", ((item.pickup_m ?? item.target_m) * 1000).toFixed(1))
    + (item.needs_reentry ? " " + t("gripperPickupReentry") : "")).join("\n");
  $("toggle-key").textContent = (
    status.shortcuts?.intervention_key || "i"
  ).toUpperCase();
  const allowed = {
    arm: ["DISARMED"],
    start: ["READY"],
    toggle: ["POLICY_ACTIVE", "HUMAN_ACTIVE"],
    pause: [
      "POLICY_ACTIVE",
      "HUMAN_ACTIVE",
      "TAKEOVER_PENDING",
      "RESUME_PENDING",
      "READY",
      "HOMING",
    ],
    disarm: ["READY", "SAFE_HOLD", "POLICY_ACTIVE", "HUMAN_ACTIVE", "HOMING"],
    reset: ["SAFE_HOLD", "ESTOP_LATCHED"],
    home_cancel: ["HOMING"],
  };
  document
    .querySelectorAll("[data-control]")
    .forEach(
      (b) => {
        const operation = b.dataset.control;
        if (operation === "start") b.textContent = t(operatorPaused ? "resumeTask" : "start");
        b.disabled =
          b.dataset.control === "estop"
            ? !authenticated
            : !online ||
              (["arm", "start", "toggle"].includes(b.dataset.control) && status.config_in_sync !== true) ||
              (!(operation === "start" && operatorPaused) && !allowed[operation]?.includes(status.mode)) ||
              (operation === "reset" && operatorPaused) ||
              (b.dataset.control === "arm" &&
                status.motion_permitted === false) ||
              (b.dataset.control === "start" && status.operation_mode === "hil" &&
                status.recording_required_for_hil !== false && status.recorder?.state !== "RECORDING");
      },
    );
  const home = status.homing;
  for (const button of document.querySelectorAll("[data-home]")) {
    button.disabled = !online || status.config_in_sync !== true || status.mode !== "DISARMED" ||
      status.recorder?.state !== "IDLE" || status.motion_permitted === false ||
      (button.dataset.home === "leader" && (status.leader_homing_supported === false ||
        !Number.isFinite(status.leader_state_age_ms) || status.leader_state_age_ms > (status.homing_state_max_age_ms || 150))) ||
      (button.dataset.home === "robot" && (!Number.isFinite(status.state_age_ms) || status.state_age_ms > (status.homing_state_max_age_ms || 150)));
  }
  $("homing-status").textContent = home
    ? [t(home.target === "leader" ? "homeLeader" : "homeRobot"), t(home.state),
      home.method ? t(home.method) : "",
      home.phase ? t(`homingPhase_${home.phase}`) : "",
      home.state === "RUNNING" ? `${Math.round((home.progress || 0) * 100)}%` : "",
      Number.isFinite(home.elapsed_ms) ? `${(home.elapsed_ms / 1000).toFixed(1)} s` : "",
      home.joint_residual ? t("homingJointResidual").replace("{joint}", home.joint_residual.joint)
        .replace("{error}", Number(home.joint_residual.error_degrees).toFixed(1)) : "",
      home.gripper_required === false ? t("homingPassiveGrippers") : "",
      home.error ? t(home.error) : ""].filter(Boolean).join(" · ")
    : t("homingIdle");
  const nameStatus = recordNameStatus?.name === $("dataset-name").value ? recordNameStatus : null;
  const jobBusy = Boolean(recordRequestPending || ["QUEUED", "RUNNING"].includes(status.record_job?.state));
  renderTaskSession(status, online, jobBusy);
  const recordingView = recordRequestPending
    ? { ...status, record_job: { ...status.record_job, operation: recordRequestPending } } : status;
  const blocker = recordingBlocker(recordingView, online, nameStatus, jobBusy);
  $("record-start").disabled = blocker !== null;
  $("record-guidance").textContent = t(blocker || "recordingNameAvailable");
  $("dataset-name").disabled = jobBusy || !["IDLE"].includes(status.recorder?.state || "IDLE");
  $("dataset-name").readOnly = Boolean(status.task_session?.automatic_naming);
  const suggestion = nameStatus?.available === false ? nameStatus.suggested_name : null;
  $("record-name-suggestion").classList.toggle("hidden", !suggestion);
  $("record-name-suggestion").textContent = suggestion ? `${t("useAvailableName")}: ${suggestion}` : t("useAvailableName");
  $("record-name-suggestion").disabled = !online || jobBusy || (status.recorder?.state || "IDLE") !== "IDLE";
  document
    .querySelectorAll("[data-outcome]")
    .forEach(
      (b) =>
        (b.disabled =
          !online || jobBusy || !["RECORDING", "ERROR"].includes(status.recorder?.state)),
    );
  for (const e of document.querySelectorAll(".video-age")) {
    const age = status.video?.[e.dataset.camera]?.age_ms;
    const stale = !Number.isFinite(age) || age > 500;
    e.textContent = stale ? t("stale") : Math.round(age) + " ms";
    e.closest(".video-card").classList.toggle("stale", stale);
  }
}
function renderTaskSession(status, online, jobBusy) {
  const session = status.task_session;
  $("task-session").classList.toggle("hidden", !session);
  const canChange = Boolean(session && online && status.mode === "DISARMED" &&
    status.recorder?.state === "IDLE" && !jobBusy && !taskRequestPending);
  $("new-task").disabled = !canChange;
  $("task-select").disabled = !canChange;
  $("task-create").disabled = !canChange;
  if (!session) return;
  $("task-current-prompt").textContent = session.active.prompt;
  $("task-guidance").textContent = t(canChange ? "taskAutomaticHint" : "TASK_CHANGE_REQUIRES_IDLE");
  const known = taskCatalog?.tasks || [];
  const tasks = known.some(task => task.id === session.active.id) ? known : [...known, session.active];
  const fingerprint = JSON.stringify([session.active.id, tasks.map(task => [task.id, task.prompt])]);
  if ($("task-select").dataset.catalog !== fingerprint) {
    $("task-select").replaceChildren(...tasks.map(task => {
      const option = node("option", `${task.id} · ${task.prompt.slice(0, 32)}`);
      option.value = task.id;
      return option;
    }));
    $("task-select").dataset.catalog = fingerprint;
  }
  $("task-select").value = session.active.id;
  if (!jobBusy && status.recorder?.state === "IDLE" && !recordNamePending)
    $("dataset-name").value = session.next_dataset_name;
}
async function loadTaskCatalog() {
  if (taskCatalogPending) return;
  taskCatalogPending = true;
  const version = taskMutationVersion;
  try {
    const result = await api("/tasks");
    if (version === taskMutationVersion) taskCatalog = result;
    renderStatus(lastStatus);
  }
  catch (error) { alerts.request(error, t("currentTask")); }
  finally { taskCatalogPending = false; }
}
function recordingBlocker(status, online, nameStatus, jobBusy) {
  if (!online) return "recordingOffline";
  if (status.config_in_sync !== true) return "CONFIG_NOT_SYNCHRONIZED";
  if (jobBusy || ["STARTING", "SAVING"].includes(status.recorder?.state))
    return status.record_job?.operation === "record_finish" || status.recorder?.state === "SAVING"
      ? "recordingSaving" : "recordingInitializing";
  if (status.recorder?.state === "ERROR") return "recordingErrorRecovery";
  if (status.recorder?.state === "RECORDING")
    return status.mode === "SAFE_HOLD" && status.reason === "OPERATOR_PAUSE" ? "recordingOperatorPaused" :
      ["SAFE_HOLD", "ESTOP_LATCHED"].includes(status.mode) ? "recordingPausedFault" :
      status.recorder.frames ? "recordingInProgress" : "recordingWaitMotion";
  if (status.recording_permitted === false || (status.recording_permitted === undefined && status.attended_test))
    return "recordingPermissionBlocked";
  if (!["READY", "DISARMED", "SAFE_HOLD"].includes(status.mode)) return "recordingModeBlocked";
  if (!nameStatus) return "recordingNameChecking";
  if (nameStatus.error === "NAME_CHECK_FAILED") return "recordingNameCheckFailed";
  if (!nameStatus.valid) return "INVALID_DATASET_NAME";
  return nameStatus.available ? null : "recordingNameExists";
}
async function checkRecordName(advance = false) {
  const revision = ++recordNameRevision;
  const name = $("dataset-name").value;
  const automatic = Boolean(lastStatus.task_session?.automatic_naming);
  const taskId = lastStatus.task_session?.active.id;
  recordNamePending = true;
  recordNameStatus = null;
  renderStatus(lastStatus);
  try {
    const checked = await api(automatic ? "/recording/name" : "/recording/name?name=" + encodeURIComponent(name));
    if (revision !== recordNameRevision || (automatic
      ? taskId !== lastStatus.task_session?.active.id : name !== $("dataset-name").value)) return null;
    if (automatic) {
      if (checked.task_session?.active.id !== taskId) throw new Error("NAME_CHECK_FAILED");
      lastStatus = {...lastStatus, task_session: checked.task_session};
      $("dataset-name").value = checked.name;
    } else if (checked.name !== name) throw new Error("NAME_CHECK_FAILED");
    if (advance && checked.valid && !checked.available && checked.suggested_name) {
      $("dataset-name").value = checked.suggested_name;
      return await checkRecordName();
    }
    recordNameStatus = checked;
    alerts.condition("record-name", false, "NAME_CHECK_FAILED");
    return checked;
  } catch (error) {
    if (revision === recordNameRevision) {
      recordNameStatus = { name, error: "NAME_CHECK_FAILED" };
      if (authenticated) alerts.condition("record-name", true, "NAME_CHECK_FAILED", {detail: error.message});
    }
    return null;
  } finally {
    if (revision === recordNameRevision) {
      recordNamePending = false;
      renderStatus(lastStatus);
    }
  }
}
function renderUnknownStatus() {
  renderStatus({...lastStatus, connected: false, mode: "OFFLINE", reason: "STATUS_STALE", telemetry_age_ms: null});
}
function restartStatusPolling(now = pollNow()) {
  lastPollTickAt = now;
  pollGraceUntil = now + statusStaleMs;
  statusNeedsRefresh = true;
  pollGeneration += 1;
  pollController?.abort();
  if (authenticated) renderUnknownStatus();
  poll();
}
function checkStatusPolling(now = pollNow()) {
  if (!authenticated) return;
  if (!statusNeedsRefresh && (!statusReceivedAt || now - statusReceivedAt <= statusStaleMs)) return;
  renderUnknownStatus();
  // A delayed UI timer is not evidence of a failed network request. Keep motion
  // controls unavailable until a fresh response, then warn only for a request
  // that stalls or repeated failures while the page is actually running.
  if (document.visibilityState === "hidden" || now < pollGraceUntil) return;
  const stalled = pollPending && now - pollStartedAt >= statusStaleMs;
  const failed = pollFailureAt !== null && now - pollFailureAt >= statusStaleMs;
  if (stalled || failed)
    alerts.condition("poll-stale", true, "STATUS_STALE", {detail: pollFailureDetail, clearOnRecovery: true});
}
async function poll() {
  if (pollPending) return;
  pollPending = true;
  const generation = pollGeneration, started = pollNow(), taskVersion = taskMutationVersion;
  pollStartedAt = started;
  const controller = typeof AbortController === "undefined" ? null : new AbortController();
  pollController = controller;
  try {
    const previous = lastStatus;
    const response = await api("/status", "GET", undefined, controller);
    if (generation !== pollGeneration) return;
    if (!authenticated) {
      authenticated = true;
      $("login-overlay").classList.add("hidden");
      startVideos();
    }
    const received = pollNow();
    if (received - lastPollTickAt > statusStaleMs) {
      restartStatusPolling(received);
      return;
    }
    if (received - started > statusStaleMs) throw new Error(t("STATUS_STALE"));
    lastStatus = taskVersion === taskMutationVersion ? response : {...response, task_session: lastStatus.task_session};
    if (lastStatus.task_session && !taskCatalog && !taskCatalogPending) loadTaskCatalog();
    statusReceivedAt = received;
    statusNeedsRefresh = false;
    pollFailureAt = null;
    pollFailureDetail = "";
    alerts.condition("poll-stale", false, "STATUS_STALE", {clearOnRecovery: true});
    if (previous.connected === false && lastStatus.connected === true) startVideos();
    const completed = lastStatus.recorder?.state === "IDLE" &&
      ["RECORDING", "STARTING", "SAVING"].includes(previous.recorder?.state) &&
      lastStatus.recorder?.dataset === $("dataset-name").value;
    const nameRace = (lastStatus.record_job?.id !== previous.record_job?.id || previous.record_job?.state !== "FAILED") &&
      lastStatus.record_job?.state === "FAILED" && lastStatus.record_job?.error === "DATASET_EXISTS";
    if (completed) checkRecordName(true);
    else if (nameRace || (!recordNameStatus && !recordNamePending) ||
      (!previous.connected && lastStatus.connected)) checkRecordName();
    renderStatus(lastStatus);
    alerts.observe(lastStatus, {datasetName: $("dataset-name").value});
  } catch (e) {
    if (generation === pollGeneration && authenticated) {
      pollFailureAt ??= started;
      pollFailureDetail = e.message;
      checkStatusPolling();
    }
  } finally {
    pollPending = false;
    if (pollController === controller) pollController = null;
    if (generation !== pollGeneration && authenticated) poll();
  }
}
setInterval(() => {
  const now = pollNow(), delayed = now - lastPollTickAt > statusStaleMs;
  lastPollTickAt = now;
  if (!authenticated) return;
  if (delayed) { restartStatusPolling(now); return; }
  checkStatusPolling(now);
  poll();
}, 500);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && authenticated) restartStatusPolling();
});
poll();
async function control(operation, data = {}) {
  if (operation === "reset" && !confirm(t("resetConfirm"))) return;
  await api("/control/" + operation, "POST", {
    epoch: lastStatus.epoch,
    ...data,
  });
  toast(t("requestSent"));
  await poll();
}
async function urgentControl(operation) {
  if (urgentPending.has(operation)) return;
  urgentPending.add(operation);
  try { await control(operation); }
  catch (error) { alerts.request(error, t(operation)); }
  finally { urgentPending.delete(operation); }
}
document.querySelectorAll("[data-control]").forEach(
  (button) =>
    (button.onclick = () => {
      if (["estop", "pause", "toggle"].includes(button.dataset.control)) {
        urgentControl(button.dataset.control);
        return;
      }
      perform(() => control(button.dataset.control));
    }),
);
document.querySelectorAll("[data-home]").forEach((button) => {
  button.onclick = () => perform(async () => {
    const target = button.dataset.home;
    if (!confirm(`${t(target === "leader" ? "homeLeader" : "homeRobot")}\n${t("homingConfirm")}`)) return;
    await control("home", { target });
  });
});
$("dataset-name").oninput = () => {
  if (lastStatus.task_session?.automatic_naming) return;
  ++recordNameRevision;
  recordNameStatus = null;
  recordNamePending = true;
  clearTimeout(recordNameTimer);
  renderStatus(lastStatus);
  recordNameTimer = setTimeout(() => checkRecordName(), 250);
};
$("record-name-suggestion").onclick = () => {
  if (!recordNameStatus?.suggested_name) return;
  $("dataset-name").value = recordNameStatus.suggested_name;
  checkRecordName();
};
$("record-start").onclick = () => perform(async () => {
  await checkRecordName();
  if ($("record-start").disabled) {
    const code = recordNameStatus?.error || recordingBlocker(lastStatus, Boolean(lastStatus.connected), recordNameStatus, false);
    if (code && code !== "NAME_CHECK_FAILED") {
      const error = new Error(t(code)); error.code = code; alerts.request(error, t("recordStart"));
    }
    return;
  }
  recordRequestPending = "record_start";
  renderStatus(lastStatus);
  try {
    await control("record_start", lastStatus.task_session?.automatic_naming ? {} : { name: $("dataset-name").value });
  } finally {
    recordRequestPending = false;
    renderStatus(lastStatus);
  }
});
$("new-task").onclick = () => {
  if ($("new-task").disabled) return;
  $("task-prompt").value = "";
  $("task-error").textContent = "";
  $("task-dialog").classList.remove("hidden");
  $("task-prompt").focus?.();
};
$("task-cancel").onclick = () => $("task-dialog").classList.add("hidden");
$("task-form").onsubmit = (event) => {
  event.preventDefault();
  const prompt = $("task-prompt").value.trim();
  if (!prompt || prompt.length > 1000) { $("task-error").textContent = t("INVALID_TASK_PROMPT"); return; }
  if ($("task-create").disabled) { $("task-error").textContent = t("TASK_CHANGE_REQUIRES_IDLE"); return; }
  return perform(async () => {
    taskRequestPending = true; renderStatus(lastStatus);
    try {
      taskCatalog = await api("/tasks", "POST", {prompt});
      ++taskMutationVersion;
      lastStatus = {...lastStatus, task_session: taskCatalog};
      recordNameStatus = null;
      $("task-dialog").classList.add("hidden");
      await poll(); await checkRecordName();
    } finally { taskRequestPending = false; renderStatus(lastStatus); }
  });
};
$("task-select").onchange = () => perform(async () => {
  const task_id = $("task-select").value;
  taskRequestPending = true; renderStatus(lastStatus);
  try {
    taskCatalog = await api("/tasks/select", "POST", {task_id});
    ++taskMutationVersion;
    lastStatus = {...lastStatus, task_session: taskCatalog};
    recordNameStatus = null;
    await poll(); await checkRecordName();
  } finally { taskRequestPending = false; renderStatus(lastStatus); }
});
async function finish(outcome) {
  if (outcome === "discard" && !confirm(t("discardConfirm"))) return;
  recordRequestPending = "record_finish";
  renderStatus(lastStatus);
  try {
    await control("record_finish", { outcome });
  } finally {
    recordRequestPending = false;
    renderStatus(lastStatus);
  }
}
document
  .querySelectorAll("[data-outcome]")
  .forEach((b) => (b.onclick = () => perform(() => finish(b.dataset.outcome))));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && alerts.queue.length) { event.preventDefault(); alerts.dismissAll(); return; }
  if (!$("task-dialog").classList.contains("hidden")) {
    if (event.key === "Escape") { event.preventDefault(); $("task-dialog").classList.add("hidden"); }
    return;
  }
  if (
    page !== "operate" ||
    event.repeat ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) ||
    !authenticated
  )
    return;
  const keys = lastStatus.shortcuts || {};
  const key = event.key.toLowerCase();
  if (key === (keys.intervention_key || "i")) {
    event.preventDefault();
    if (!$("takeover").disabled) urgentControl("toggle");
    return;
  }
  if (alerts.queue.length) return;
  for (const [name, outcome] of [
    ["success_key", "success"],
    ["failure_key", "failure"],
    ["discard_key", "discard"],
  ])
    if (
      key ===
      (keys[name] || { success: "s", failure: "f", discard: "a" }[outcome])
    ) {
      event.preventDefault();
      if (lastStatus.recorder?.state === "RECORDING")
        perform(() => finish(outcome));
    }
});
if (typeof window !== "undefined") {
  window.addEventListener("unhandledrejection", event => {
    event.preventDefault();
    alerts.push(`script:${++alerts.serial}`, "UI_SCRIPT_ERROR", {detail: event.reason?.message || event.reason});
  });
  window.addEventListener("error", event => {
    alerts.push(`script:${++alerts.serial}`, "UI_SCRIPT_ERROR", {detail: event.message});
  });
}
for (const id of ["sim-left", "sim-right"])
  $(id).oninput = async () => {
    if (lastStatus.mode !== "HUMAN_ACTIVE") return;
    const q = [...(lastStatus.q || Array(14).fill(0))];
    q[0] = Number($("sim-left").value);
    q[7] = Number($("sim-right").value);
    try {
      await api("/simulation/leader", "POST", { q });
    } catch (e) {
      toast(e.message, true);
    }
  };
async function loadConfig() {
  if (!schema) {
    const [configSchema, profiles] = await Promise.all([api("/config/schema"), api("/arm-profiles")]);
    schema = configSchema;
    armProfileBuiltins = profiles;
  }
  const result = await api("/config/" + $("config-target").value);
  showConfig(result);
  $("config-result").textContent = result.hot_update_pending ? t("CONFIG_APPLY_PENDING") : "";
}
function showConfig(result) {
  config = result.config;
  activeConfig = result.active_config || null;
  revision = result.revision;
  armProfileChoice = {};
  armProfileNames = {};
  $("restart-banner").classList.toggle("hidden", !configNeedsRestart(result));
  $("defaults-info").textContent = t("defaultsInfo") + (result.defaults_created_at
    ? ` ${new Date(result.defaults_created_at * 1000).toLocaleString()}` : "");
  renderConfig();
}
function configNeedsRestart(result) {
  return result.restart_required || result.restart_targets?.length > 0;
}
function attendedSummary(limits) {
  if (!limits) return t("attendedTest");
  const pieces = [t("manualSession")];
  pieces.push(limits.duration_seconds == null ? t("noTimeLimit") : `${limits.duration_seconds} ${t("seconds")}`);
  pieces.push(limits.single_use ? t("singleUse") : t("rearmAllowed"));
  pieces.push(limits.gripper_commands == null ? t("gripperFollows") : t("gripperFixed"));
  if (limits.joint_delta == null) pieces.push(t("noRelativeLimit"));
  if (limits.expires_at != null) pieces.push(`${t("expiresAt")} ${new Date(limits.expires_at * 1000).toLocaleString()}`);
  return pieces.join(" · ");
}
function resolve(def) {
  return def?.$ref ? schema.$defs[def.$ref.split("/").pop()] : def;
}
function setPath(path, value) {
  let parent = config;
  for (const key of path.slice(0, -1)) parent = parent[key];
  parent[path.at(-1)] = value;
  $("config-json").value = JSON.stringify(config, null, 2);
  updateArmProfileStatus();
}
function label(key, path = []) {
  if (key === "duration_seconds" && path.includes("homing")) return t("homingDuration");
  return lang === "zh"
    ? fieldZh[key] || t(key)
    : key.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}
function showArmField(path) {
  const role = path[0];
  if (!["robot", "leader"].includes(role)) return true;
  const connection = config[role];
  const key = path.at(-1);
  if (path.length === 2) {
    if (key === "backend") return true;
    if (connection.backend === "sim") return false;
    if (role === "leader" && connection.backend === "sii_serial")
      return ["arms", "feedback_hz", "calibration_file"].includes(key);
    return !["calibration_file", "serial_support_validated", "passive_supported"].includes(key);
  }
  if (path[1] !== "arms" || path.length !== 4) return true;
  if (connection.backend === "sii_serial")
    return ["name", "port", "baudrate", "motor_ids", "directions", "offsets", "gripper_scale", "gripper_offset", "gripper_range_degrees"].includes(key);
  if (["port", "baudrate", "motor_ids", "gripper_scale", "gripper_offset", "gripper_range_degrees"].includes(key)) return false;
  const arm = connection.arms[path[2]];
  if (key.startsWith("mode_") && key !== "mode_switch_enabled" && !arm.mode_switch_enabled) return false;
  if (key === "driver_node") return arm.mode_switch_enabled && arm.mode_confirmation === "openeai_graph";
  if (key === "mode_state_topic" && arm.mode_confirmation === "openeai_graph") return false;
  return true;
}
function armProfiles(role) {
  return [
    ...(armProfileBuiltins[role] || []).map((profile) => ({ ...profile, builtin: true })),
    ...(config.arm_profiles?.[role] || []),
  ];
}
function profileConfigMatches(profile, role) {
  return profile && JSON.stringify(profile.config) === JSON.stringify(config[role]);
}
function updateArmProfileStatus() {
  if (!["robot", "leader"].includes(category)) return;
  const status = $("arm-profile-status");
  if (!status) return;
  const selected = armProfiles(category).find((profile) => profile.id === config.arm_profiles?.[`selected_${category}`]);
  status.textContent = selected
    ? `${selected.name} · ${t(profileConfigMatches(selected, category) ? "profileLoadedDraft" : "profileModifiedDraft")}`
    : t("profileCurrentDraft");
  status.classList.toggle("modified", !!selected && !profileConfigMatches(selected, category));
}
function newArmProfileId() {
  const random = crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
  return `custom-${random}`;
}
function profileButton(textKey, id, action) {
  const button = node("button", t(textKey));
  button.type = "button";
  button.id = id;
  button.onclick = action;
  return button;
}
function markProfileDraft(message) {
  $("config-json").value = JSON.stringify(config, null, 2);
  $("config-result").textContent = t("profileDeploymentDraft");
  renderConfig();
  toast(t(message));
}
function renderArmProfileEditor(role) {
  const panel = node("section", undefined, "arm-profiles");
  panel.setAttribute("aria-labelledby", "arm-profiles-title");
  const title = node("h3", t("armProfiles"));
  title.id = "arm-profiles-title";
  panel.append(title, node("p", t(role === "robot" ? "followerPrinciple" : "leaderPrinciple"), "muted"));
  const profiles = armProfiles(role);
  const selectedKey = `selected_${role}`;
  const choice = armProfileChoice[role] ?? config.arm_profiles?.[selectedKey] ?? "";
  const selected = profiles.find((profile) => profile.id === choice);
  const row = node("div", undefined, "profile-controls");
  const profileField = node("div", undefined, "profile-field");
  const selectLabel = node("label", t("chooseArmProfile"));
  selectLabel.htmlFor = "arm-profile-select";
  const select = node("select");
  select.id = "arm-profile-select";
  const blank = node("option", t("profileChoose"));
  blank.value = "";
  select.append(blank);
  for (const builtin of [true, false]) {
    const items = profiles.filter((profile) => !!profile.builtin === builtin);
    if (!items.length) continue;
    const group = node("optgroup");
    group.label = t(builtin ? "builtinProfiles" : "customProfiles");
    for (const profile of items) {
      const option = node("option", profile.name);
      option.value = profile.id;
      group.append(option);
    }
    select.append(group);
  }
  select.value = selected ? choice : "";
  select.onchange = () => {
    armProfileChoice[role] = select.value;
    const profile = profiles.find((item) => item.id === select.value);
    armProfileNames[role] = profile?.builtin ? "" : profile?.name || "";
    renderConfig();
  };
  profileField.append(selectLabel, select);
  const apply = profileButton("applyArmProfile", "arm-profile-apply", () => {
    if (!selected) return;
    config[role] = structuredClone(selected.config);
    config.arm_profiles ||= { robot: [], leader: [], selected_robot: "", selected_leader: "" };
    config.arm_profiles[selectedKey] = selected.id;
    markProfileDraft("profileApplied");
  });
  apply.disabled = !selected;
  row.append(profileField, apply);
  panel.append(row);
  const status = node("p", undefined, "profile-status");
  status.id = "arm-profile-status";
  const active = profiles.find((profile) => profile.id === config.arm_profiles?.[selectedKey]);
  status.textContent = active
    ? `${active.name} · ${t(profileConfigMatches(active, role) ? "profileLoadedDraft" : "profileModifiedDraft")}`
    : t("profileCurrentDraft");
  status.classList.toggle("modified", !!active && !profileConfigMatches(active, role));
  panel.append(status);
  const saveRow = node("div", undefined, "profile-controls profile-save");
  const nameField = node("div", undefined, "profile-field");
  const nameLabel = node("label", t("profileName"));
  nameLabel.htmlFor = "arm-profile-name";
  const name = node("input");
  name.id = "arm-profile-name";
  name.type = "text";
  name.maxLength = 80;
  name.value = armProfileNames[role] ?? (selected && !selected.builtin ? selected.name : "");
  name.placeholder = t("profileNamePlaceholder");
  name.oninput = () => { armProfileNames[role] = name.value; name.setCustomValidity(""); };
  nameField.append(nameLabel, name);
  const save = (update) => {
    const trimmed = name.value.trim();
    if (!trimmed) { name.setCustomValidity(t("profileNameRequired")); name.reportValidity(); return; }
    name.setCustomValidity("");
    if (!$("config-form").reportValidity()) return;
    if (config[role].backend === "sim") { toast(t("profileHardwareOnly"), true); return; }
    config.arm_profiles ||= { robot: [], leader: [], selected_robot: "", selected_leader: "" };
    const custom = config.arm_profiles[role];
    if (update && (!selected || selected.builtin)) return;
    if (!update && custom.length >= 32) { toast(t("profileLimit"), true); return; }
    const id = update ? selected.id : newArmProfileId();
    const item = { id, name: trimmed, config: structuredClone(config[role]) };
    if (update) custom[custom.findIndex((profile) => profile.id === id)] = item;
    else custom.push(item);
    config.arm_profiles[selectedKey] = id;
    armProfileChoice[role] = id;
    armProfileNames[role] = trimmed;
    markProfileDraft("profileSavedDraft");
  };
  const add = profileButton("saveNewArmProfile", "arm-profile-add", () => save(false));
  const update = profileButton("updateArmProfile", "arm-profile-update", () => save(true));
  update.disabled = !selected || !!selected.builtin;
  const remove = profileButton("deleteArmProfile", "arm-profile-delete", () => {
    if (!selected || selected.builtin) return;
    config.arm_profiles[role] = config.arm_profiles[role].filter((profile) => profile.id !== selected.id);
    if (config.arm_profiles[selectedKey] === selected.id) config.arm_profiles[selectedKey] = "";
    armProfileChoice[role] = "";
    armProfileNames[role] = "";
    markProfileDraft("profileDeletedDraft");
  });
  remove.disabled = !selected || !!selected.builtin;
  remove.classList.add("subtle");
  saveRow.append(nameField, add, update, remove);
  panel.append(saveRow, node("p", t("profilePersistenceHint"), "tiny"));
  if (config[role].backend === "sim") panel.append(node("p", t("profileSimulationHint"), "tiny"));
  if (config[role].backend === "ros") panel.append(node("p", t(config[role].driver === "arx5" ? "rosArxContractHint" : "rosContractHint"), "tiny"));
  if (config[role].backend === "sii_serial") panel.append(node("p", t("printedCommissioningHint"), "tiny"));
  return panel;
}
function renderFrequencyField(parent, def, value, path) {
  const setting = frequencyFields[path.join(".")];
  const id = "field-" + path.join("-");
  const field = node("div", undefined, "config-field frequency-field");
  field.dataset.search = `${t(setting.title)} ${label(path.at(-1), path)} ${path.join(".")}`;
  const title = node("label", t(setting.title));
  title.htmlFor = id;
  field.append(title, node("small", path.join(".")));
  const minimum = path[0] === "safety" ? Math.max(def.minimum, config.recording.fps) : def.minimum;
  const maximum = def.maximum;
  const preset = node("select");
  preset.id = id;
  preset.required = true;
  const caption = rate => `${rate} Hz · ${(1000 / rate).toFixed(2)} ms`;
  for (const rate of setting.rates) {
    const option = node("option", caption(rate));
    option.value = String(rate);
    option.disabled = rate < minimum || rate > maximum;
    preset.append(option);
  }
  const customOption = node("option", t("customFrequency"));
  customOption.value = "custom";
  preset.append(customOption);
  const custom = node("input");
  custom.id = `${id}-custom`;
  custom.type = "number";
  custom.step = "1";
  custom.min = minimum;
  custom.max = maximum;
  custom.required = true;
  custom.value = value;
  custom.setAttribute("aria-label", `${t(setting.title)} · ${t("customFrequency")}`);
  preset.value = setting.rates.includes(value) ? String(value) : "custom";
  const updateCustom = () => {
    const visible = preset.value === "custom";
    custom.disabled = !visible;
    custom.classList.toggle("hidden", !visible);
  };
  updateCustom();
  const effective = node("small", undefined, "frequency-effective");
  const refreshEffective = () => {
    const active = activeConfig?.[path[0]]?.[path[1]];
    effective.textContent = t("effectiveFrequency").replace("{rate}", Number.isFinite(active) ? caption(active) : t("frequencyUnknown"));
  };
  refreshEffective();
  const assign = (input, raw) => {
    input.setCustomValidity("");
    const number = Number(raw);
    if (raw === "" || !Number.isInteger(number) || number < minimum || number > maximum) {
      input.setCustomValidity(t("frequencyRange").replace("{min}", minimum).replace("{max}", maximum));
      input.reportValidity();
      return false;
    }
    setPath(path, number);
    return true;
  };
  preset.onchange = () => {
    preset.setCustomValidity("");
    custom.setCustomValidity("");
    updateCustom();
    if (preset.value === "custom") custom.focus?.();
    else if (assign(preset, preset.value)) custom.value = config[path[0]][path[1]];
  };
  custom.onchange = () => assign(custom, custom.value);
  field.append(preset, custom, effective, node("small", t(setting.help)));
  if (path[0] === "safety")
    field.append(node("small", t("frequencyRecordingFloor").replace("{fps}", config.recording.fps)));
  parent.append(field);
}
function renderFields(parent, def, value, path, inDanger = false) {
  def = resolve(def);
  if (frequencyFields[path.join(".")]) {
    renderFrequencyField(parent, def, value, path);
    return;
  }
  if (def.type === "object") {
    if (inDanger && path.length > 1) parent.append(node("h3", label(path.at(-1)), "danger-group"));
    for (const [key, child] of Object.entries(def.properties || {})) {
      if (!inDanger && dangerFields[path[0]]?.includes(key)) continue;
      if (!inDanger && !showArmField([...path, key])) continue;
      renderFields(parent, child, value[key], [...path, key], inDanger);
    }
    return;
  }
  if (def.type === "array" && resolve(def.items)?.type === "object") {
    value.forEach((item, index) => {
      const box = node("div", undefined, "nested-fields");
      box.append(node("h3", label(path.at(-1)) + " · " + (item.name || index)));
      const grid = node("div", undefined, "config-fields");
      renderFields(grid, def.items, item, [...path, index], inDanger);
      box.append(grid);
      parent.append(box);
    });
    return;
  }
  const field = node("div", undefined, "config-field");
  const nullable = def.anyOf?.some((option) => option.type === "null");
  const fullDef = def;
  if (nullable) def = resolve(def.anyOf.find((option) => option.type !== "null"));
  const key = path.at(-1);
  field.dataset.search = label(key, path) + " " + path.join(".");
  const id = "field-" + path.join("-");
  const title = node("label", label(key, path));
  title.htmlFor = id;
  field.append(title, node("small", path.join(".")));
  let input;
  if (def.enum) {
    input = node("select");
    for (const option of def.enum) {
      if (key === "backend" && ["robot", "leader"].includes(path[0]) && option === "sim" && !config.general.simulation) continue;
      const item = node("option", t(String(option)));
      item.value = option;
      input.append(item);
    }
    input.value = value;
    input.required = true;
  } else if (def.type === "array") {
    input = node("textarea");
    input.rows = 3;
    input.value = JSON.stringify(value);
    input.title = t("arraysHint");
  } else {
    input = node("input");
    input.type =
      def.type === "boolean"
        ? "checkbox"
        : ["integer", "number"].includes(def.type)
          ? "number"
          : "text";
    if (input.type === "checkbox") input.checked = value;
    else input.value = value ?? "";
    if (input.type === "number") {
      input.step = def.type === "integer" ? "1" : "any";
      if (def.minimum !== undefined) input.min = def.minimum;
      if (def.maximum !== undefined) input.max = def.maximum;
    }
  }
  input.id = id;
  if (["integer", "number"].includes(def.type)) input.required = !nullable;
  if (def.type === "string") {
    if (def.minLength !== undefined) input.minLength = def.minLength;
    if (def.maxLength !== undefined) input.maxLength = def.maxLength;
    if (def.pattern) input.pattern = def.pattern;
    input.required = (def.minLength || 0) > 0;
  }
  if (fullDef.description) field.append(node("small", fullDef.description));
  let unset;
  if (nullable) {
    const row = node("label", undefined, "nullable-toggle");
    unset = node("input");
    unset.type = "checkbox";
    unset.id = `${id}-unset`;
    unset.checked = value == null;
    input.disabled = unset.checked;
    row.append(unset, node("span", key === "gripper_commands" ? t("followGripperInput") : t("disableLimit")));
    unset.onchange = () => {
      input.disabled = unset.checked;
      input.setCustomValidity("");
      if (unset.checked) setPath(path, null);
      else {
        const initial = fullDef.default ?? (def.type === "array" ? [0, 0] : 1);
        input.value = def.type === "array" ? JSON.stringify(initial) : initial;
        setPath(path, initial);
      }
    };
    field.append(row);
  }
  input.onchange = () => {
    try {
      input.setCustomValidity("");
      if (!input.reportValidity()) return;
      let parsed =
        input.type === "checkbox"
          ? input.checked
          : def.type === "array"
            ? JSON.parse(input.value)
            : ["integer", "number"].includes(def.type)
              ? Number(input.value)
              : input.value;
      if (def.const !== undefined && parsed !== def.const)
        throw new Error("Constant: " + def.const);
      setPath(path, parsed);
      input.setCustomValidity("");
      if (["robot", "leader"].includes(path[0]) && ["backend", "driver", "mode_switch_enabled", "mode_confirmation"].includes(key)) renderConfig();
    } catch (e) {
      input.setCustomValidity(e.message);
      input.reportValidity();
      toast(t("invalidJson") + ": " + e.message, true);
    }
  };
  if (def.const !== undefined) {
    input.disabled = true;
  }
  field.append(input);
  parent.append(field);
}
function renderConfig() {
  $("config-categories").replaceChildren();
  for (const name of categories) {
    const b = node("button", t(name === "robot" ? "followerConnection" : name === "leader" ? "leaderConnection" : name), `${name === category ? "selected" : ""} ${name === "safety" ? "danger-category" : ""}`);
    b.onclick = () => {
      category = name;
      renderConfig();
    };
    $("config-categories").append(b);
  }
  $("config-category-title").textContent = t(category === "robot" ? "followerConnection" : category === "leader" ? "leaderConnection" : category);
  const grid = node("div", undefined, "config-fields");
  $("config-form").classList.toggle("danger-zone", category === "safety");
  if (category === "frequencies") {
    grid.append(node("p", t("frequencyIntro"), "frequency-intro"));
    for (const name of Object.keys(frequencyFields)) {
      const [group, key] = name.split(".");
      renderFields(grid, resolve(schema.properties[group]).properties[key], config[group][key], [group, key]);
    }
  } else if (category === "safety") {
    grid.append(node("p", t("dangerNotice"), "danger-notice"));
    renderFields(grid, schema.properties.safety, config.safety, ["safety"], true);
    for (const [group, keys] of Object.entries(dangerFields)) {
      if (group === "leader" && config.leader.backend !== "sii_serial") continue;
      if (group === "robot" && config.robot.backend !== "ros") continue;
      grid.append(node("h3", t(group === "robot" ? "followerConnection" : group === "leader" ? "leaderConnection" : group), "danger-group"));
      const properties = resolve(schema.properties[group]).properties;
      for (const key of keys) renderFields(grid, properties[key], config[group][key], [group, key], true);
    }
  } else {
    renderFields(grid, schema.properties[category], config[category], [category]);
  }
  const profileEditor = ["robot", "leader"].includes(category) ? renderArmProfileEditor(category) : null;
  $("config-form").replaceChildren(...(profileEditor ? [profileEditor, grid] : [grid]));
  $("config-json").value = JSON.stringify(config, null, 2);
  filterFields();
}
function filterFields() {
  const term = $("config-search").value.toLowerCase();
  document
    .querySelectorAll(".config-field")
    .forEach((e) =>
      e.classList.toggle(
        "hidden",
        !e.dataset.search.toLowerCase().includes(term),
      ),
    );
}
$("config-search").oninput = filterFields;
$("config-form").onsubmit = (e) => e.preventDefault();
$("config-target").onchange = () => perform(loadConfig);
$("reload-config").onclick = () => perform(loadConfig);
$("apply-json").onclick = () => {
  try {
    config = JSON.parse($("config-json").value);
    armProfileChoice = {};
    armProfileNames = {};
    renderConfig();
    toast(t("jsonApplied"));
  } catch (e) {
    toast(t("invalidJson") + ": " + e.message, true);
  }
};
$("validate-config").onclick = () =>
  perform(async () => {
    if (!$("config-form").reportValidity()) return;
    await api("/config/validate", "POST", { config });
    $("config-result").textContent = t("configValid");
    toast(t("configValid"));
  });
$("save-config").onclick = () =>
  perform(async () => {
    if (!$("config-form").reportValidity()) return;
    await api("/config/validate", "POST", { config });
    if (!confirm(t("saveConfirm"))) return;
    const result = await api("/config/" + $("config-target").value, "POST", {
      config,
      revision,
    });
    showConfig(result);
    $("config-result").textContent = configNeedsRestart(result) ? t("syncedPending") : t("syncedActive");
    toast($("config-result").textContent);
  });
$("restore-config").onclick = () =>
  perform(async () => {
    const target = $("config-target").value;
    if (!confirm(`${t("restoreConfirm")}\n${t("restoreTarget")}: ${t(target)}`)) return;
    const baseline = await api("/config/defaults/" + target);
    const result = await api("/config/" + target, "POST", {
      config: baseline.config,
      revision,
    });
    showConfig(result);
    $("config-result").textContent = configNeedsRestart(result) ? t("defaultsRestored") : t("defaultsAlreadyActive");
    toast($("config-result").textContent);
  });
async function loadDatasets() {
  const data = await api("/datasets");
  $("dataset-list").replaceChildren();
  $("dataset-empty").classList.toggle("hidden", data.length > 0);
  for (const entry of data) {
    const row = node("tr");
    for (const value of [
      entry.name,
      entry.episodes,
      entry.frames,
      entry.fps,
      entry.robot_type,
    ])
      row.append(node("td", value ?? "—"));
    const cell = node("td");
    const button = node("button", t("inspect"));
    button.onclick = () =>
      perform(async () => {
        toast(t("loading"));
        detail = await api("/datasets/inspect", "POST", {
          id: entry.id,
          deep: true,
        });
        renderDetail(detail);
      });
    cell.append(button);
    row.append(cell);
    $("dataset-list").append(row);
  }
}
$("refresh-datasets").onclick = () => perform(loadDatasets);
function renderDetail(data) {
  $("dataset-detail").classList.remove("hidden");
  $("dataset-detail-title").textContent = data.id;
  $("dataset-compatibility").textContent = t(
    data.compatible ? "compatible" : "incompatible",
  );
  $("dataset-compatibility").className =
    "badge" + (data.compatible ? "" : " bad");
  $("dataset-issues").replaceChildren();
  if (!data.issues.length) $("dataset-issues").append(node("p", t("noIssues")));
  for (const issue of data.issues) {
    $("dataset-issues").append(
      node(
        "div",
        t(issue.code) +
          " · " +
          (issue.field || "") +
          (issue.property ? " / " + issue.property : "") +
          (issue.actual !== undefined
            ? " · " + JSON.stringify(issue.actual)
            : ""),
        "issue",
      ),
    );
  }
  $("dataset-issues").append(node("p", t("scanLimited"), "tiny"));
  $("dataset-metadata").textContent = JSON.stringify(data, null, 2);
  drawTrace(data.samples || []);
  $("episode-list").replaceChildren();
  for (const episode of data.episodes || []) {
    const row = node("tr");
    for (const value of [
      episode.episode_index,
      episode.length,
      episode.episode_success,
    ])
      row.append(node("td", value ?? "—"));
    $("episode-list").append(row);
  }
  $("dataset-previews").replaceChildren();
  for (const key of Object.keys(data.info.features || {}).filter((k) =>
    k.startsWith("observation.images."),
  )) {
    const camera = key.split(".").at(-1);
    const figure = node("figure");
    const image = node("img");
    image.alt = t(camera);
    image.src =
      "/api/datasets/preview?id=" +
      encodeURIComponent(data.id) +
      "&camera=" +
      encodeURIComponent(camera);
    image.onerror = () => {
      image.removeAttribute("src");
      image.alt = t("checkFailed");
    };
    figure.append(image, node("figcaption", t(camera)));
    $("dataset-previews").append(figure);
  }
}
function drawTrace(samples) {
  const svg = $("trace");
  svg.replaceChildren();
  const values = samples
    .flatMap((s) => [s.action?.[0], s["observation.state"]?.[0]])
    .filter(Number.isFinite);
  if (!values.length) return;
  const low = Math.min(...values) - 0.01,
    high = Math.max(...values) + 0.01;
  for (const [key, color] of [
    ["action", "#16765a"],
    ["observation.state", "#d09138"],
  ]) {
    const line = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "polyline",
    );
    line.setAttribute(
      "points",
      samples
        .map((s, i) =>
          [
            20 + (i * 860) / Math.max(1, samples.length - 1),
            160 - ((Number(s[key]?.[0] || 0) - low) * 140) / (high - low),
          ].join(","),
        )
        .join(" "),
    );
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "2");
    svg.append(line);
  }
}
translate();
