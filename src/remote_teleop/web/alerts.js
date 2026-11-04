/* Fault notifications are edge-triggered; acknowledging never changes control. */
export class AlertCenter {
  constructor(translate, render, now = () => Date.now()) {
    this.t = translate;
    this.render = render;
    this.now = now;
    this.queue = [];
    this.seen = new Set();
    this.conditions = new Map();
    this.serial = 0;
  }

  clean(value) {
    return String(value ?? "").replace(/(?:https?|wss?):\/\/[^\s<>"']+/gi, this.t("hiddenAddress")).slice(0, 1800);
  }

  recovery(code) {
    if (code === "UI_SCRIPT_ERROR") return this.t("recoverPage");
    if (code === "TRANSITION_TIMEOUT" || code === "LEADER_PREPARE_FAILED") return this.t("recoverTransition");
    if (/INVALID_DATASET_NAME|DATASET_EXISTS/.test(code)) return this.t("recoverDatasetName");
    if (/^REQUEST_FAILED$|INVALID_JSON|INVALID_SERVER_RESPONSE|INVALID_CONFIG/.test(code)) return this.t("recoverRequest");
    if (/^POLICY_/.test(code)) return this.t("recoverPolicy");
    if (/HOMING|LEADER|ALIGNMENT|FEEDBACK|STATE_STALE/.test(code)) return this.t("recoverFeedback");
    if (/RECORD|EPISODE|WRITE|DISK|DATASET|INCOMPLETE/.test(code)) return this.t("recoverRecording");
    if (/CONFIG|DISARM_REQUIRED/.test(code)) return this.t("recoverConfig");
    if (/AUTH|LOGIN|SESSION/.test(code)) return this.t("recoverLogin");
    if (/OFFLINE|TRANSPORT|STATUS_STALE|NETWORK|REQUEST_TIMEOUT|VIDEO|NAME_CHECK_FAILED/.test(code)) return this.t("recoverConnection");
    return this.t("recoverGeneral");
  }

  push(key, code, options = {}) {
    if (this.seen.has(key)) return;
    this.seen.add(key);
    if (this.seen.size > 256) this.seen.delete(this.seen.values().next().value);
    this.queue.push({key, code: this.clean(code), title: options.title || this.t("faultTitle"),
      conditionKey: options.conditionKey,
      titleKey: options.title ? null : "faultTitle", messageKey: options.messageKey || (!options.message ? code : null),
      recoveryKey: options.recoveryKey, customRecovery: Boolean(options.recovery), detailFactory: options.detailFactory,
      message: this.clean(options.message || this.t(code)), detail: this.clean(options.detail),
      recovery: options.recovery || this.recovery(code)});
    // Keep distinct recent incidents without stacking overlays or growing forever.
    if (this.queue.length > 32) this.queue.splice(1, 1);
    this.refresh();
  }

  refresh() {
    const notice = this.queue[0];
    this.render(notice ? {...notice, title: notice.titleKey ? this.t(notice.titleKey) : notice.title,
      message: notice.messageKey ? this.clean(this.t(notice.messageKey)) : notice.message,
      detail: notice.detailFactory ? this.clean(notice.detailFactory()) : notice.detail,
      recovery: notice.recoveryKey ? this.t(notice.recoveryKey) :
        notice.customRecovery ? notice.recovery : this.recovery(notice.code)} : null, this.queue.length);
  }
  dismiss() { this.queue.shift(); this.refresh(); }
  dismissAll() { this.queue = []; this.refresh(); }

  transitionDetail(detail) {
    if (!detail) return this.t("transitionNoDetail");
    const number = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : "—";
    const phase = this.t(`transitionStage_${detail.stage}`);
    const parts = [this.t("transitionStageDetail").replace("{phase}", phase)
      .replace("{elapsed}", number(detail.elapsed_ms, 0))];
    if (detail.joint && Number.isFinite(detail.error_degrees)) {
      parts.push(this.t(detail.alignment_enabled === false ? "transitionAlignmentDisabled" : "transitionJointDetail")
        .replace("{joint}", detail.joint).replace("{error}", number(detail.error_degrees))
        .replace("{limit}", number(detail.tolerance_degrees)));
    }
    if (detail.error_code) parts.push(this.t("transitionDriverError").replace("{error}", this.t(detail.error_code)));
    parts.push(detail.progress_age_ms == null ? this.t("transitionNoProgress") :
      this.t("transitionProgressAge").replace("{age}", number(detail.progress_age_ms, 0)));
    if (Array.isArray(detail.grippers) && detail.grippers.length) {
      parts.push(this.t("transitionGrippersSeparate").replace("{values}", detail.grippers.map(gripper =>
        `${this.t(gripper.arm === "left" ? "transitionLeftGripper" : "transitionRightGripper")} ${number(gripper.difference_mm)} mm`).join(" / ")));
    }
    return parts.join("\n");
  }

  request(error, context = "") {
    if (error.notified) return;
    const code = error.code || (error.name === "AbortError" ? "REQUEST_TIMEOUT" : "REQUEST_FAILED");
    this.push(`request:${++this.serial}`, code, {message: error.message || this.t(code),
      messageKey: error.code && this.t(code) !== code ? code : null,
      detail: [context, error.httpStatus ? `HTTP ${error.httpStatus}` : ""].filter(Boolean).join(" · ")});
  }

  condition(key, active, code, options = {}, delay = 0, token = code) {
    if (!active) {
      const previous = this.conditions.get(key);
      this.conditions.delete(key);
      if (previous?.clearOnRecovery || options.clearOnRecovery) {
        const length = this.queue.length;
        this.queue = this.queue.filter(notice => notice.conditionKey !== key);
        if (this.queue.length !== length) this.refresh();
      }
      return;
    }
    let condition = this.conditions.get(key);
    if (!condition || condition.token !== token) {
      condition = {since: this.now(), notified: false, token, clearOnRecovery: Boolean(options.clearOnRecovery)};
      this.conditions.set(key, condition);
    }
    if (!condition.notified && this.now() - condition.since >= delay) {
      condition.notified = true;
      this.push(`condition:${key}:${++this.serial}`, code, {...options, conditionKey: key});
    }
  }

  observe(status, context = {}) {
    const online = status.connected === true && Number.isFinite(status.telemetry_age_ms) && status.telemetry_age_ms < 1500;
    const expectedHolds = ["OPERATOR_PAUSE", "EPISODE_FINISHED", "HOMING_CANCELLED"];
    const faultHold = ["SAFE_HOLD", "ESTOP_LATCHED"].includes(status.mode) && !expectedHolds.includes(status.reason);
    if (faultHold) {
      const code = status.reason || status.mode;
      const policy = status.policy_error;
      let messageKey = code, detail = "", detailFactory = null;
      if (policy) {
        messageKey = policy.error_code || (policy.http_status === 404 ? "POLICY_HTTP_404" :
          [401, 403].includes(policy.http_status) ? "POLICY_HTTP_AUTH" : "POLICY_SERVICE_ERROR");
        detail = [policy.error_type, policy.http_status ? `HTTP ${policy.http_status}` : ""].filter(Boolean).join(" · ");
      } else if (code === "TRANSITION_TIMEOUT" || code === "LEADER_PREPARE_FAILED"
                 || status.transition_detail && code === "LEADER_LEASE_EXPIRED") {
        detailFactory = () => this.transitionDetail(status.transition_detail);
        detail = detailFactory();
      } else if (code === "POLICY_INFERENCE_TIMEOUT") {
        detail = this.t("inferenceBudgetDetail").replace("{inference}", status.policy_inference_budget_ms ?? 500)
          .replace("{transport}", status.command_transport_allowance_ms ?? 250);
        detailFactory = () => this.t("inferenceBudgetDetail").replace("{inference}", status.policy_inference_budget_ms ?? 500)
          .replace("{transport}", status.command_transport_allowance_ms ?? 250);
      } else if (status.homing?.error && code === "HOMING_FAILED") {
        detailFactory = () => [this.t(status.homing.error),
          status.homing.phase ? this.t(`homingPhase_${status.homing.phase}`) : "",
          status.homing.joint_residual ? this.t("homingJointResidual")
            .replace("{joint}", status.homing.joint_residual.joint)
            .replace("{error}", Number(status.homing.joint_residual.error_degrees).toFixed(1)) : ""
        ].filter(Boolean).join(" · ");
        detail = detailFactory();
        this.seen.add(`homing:${status.homing.id}:${status.homing.error}`);
      } else if (code === "TRACKING_ERROR" && status.tracking_error_detail) {
        const tracking = status.tracking_error_detail;
        detailFactory = () => this.t("trackingErrorDetail").replace("{joint}", tracking.joint)
          .replace("{error}", Number(tracking.error_degrees).toFixed(1))
          .replace("{measured}", Number(tracking.measured).toFixed(3))
          .replace("{target}", Number(tracking.target).toFixed(3))
          .replace("{elapsed}", Math.round(tracking.elapsed_ms))
          .replace("{limit}", (Number(tracking.limit_rad) * 180 / Math.PI).toFixed(1))
          .replace("{duration}", tracking.duration_ms)
          .replace("{trigger}", this.t(tracking.trigger === "hard_limit" ? "trackingHardLimit" : "trackingSustained"));
        detail = detailFactory();
      } else if (status.limit_detail) {
        detail = JSON.stringify(status.limit_detail);
      }
      // Repeated stop requests may rotate the control epoch while the same
      // incident remains latched. Acknowledgement lasts until recovery or a
      // different fault, independently of the motion-authority epoch.
      const token = JSON.stringify([code, messageKey, policy?.error_type, policy?.http_status,
        code === "HOMING_FAILED" ? status.homing?.id : null]);
      this.condition("hold", true, code, {message: this.t(messageKey), messageKey, detail, detailFactory}, 0, token);
    } else {
      this.condition("hold", false);
    }
    this.condition("transport", !online, "OFFLINE", {}, 1500);
    this.condition("config", online && status.config_in_sync === false, "CONFIG_NOT_SYNCHRONIZED", {}, 1500);
    this.condition("leader", online && Boolean(status.leader_error), status.leader_error || "LEADER_OR_FEEDBACK_STALE", {}, 1500);
    this.condition("health", online && status.healthy === false, "ROBOT_HEALTH_LOST", {}, 1500);
    const staleCameras = Object.entries(status.video || {}).filter(([, value]) =>
      !Number.isFinite(value.age_ms) || value.age_ms > 1500).map(([name]) => name);
    this.condition("video", online && staleCameras.length > 0, "VIDEO_UNAVAILABLE",
      {detail: staleCameras.map(name => this.t(name)).join(" · ")}, 2000, staleCameras.join(","));
    const recorder = status.recorder || {};
    this.condition("recorder", recorder.state === "ERROR", recorder.error || "RECORDER_ERROR", {}, 0,
      `${recorder.dataset}:${recorder.error}`);
    const job = status.record_job || {};
    if (job.state === "FAILED" && (job.operation !== "record_start" || job.dataset === context.datasetName)) {
      this.push(`job:${job.id}:${job.error}`, job.error || "RECORD_JOB_FAILED",
        {detail: [this.t(job.operation), job.dataset].filter(Boolean).join(" · ")});
    }
    this.condition("record-job", ["QUEUED", "RUNNING"].includes(job.state), "RECORD_JOB_SLOW",
      {detail: [this.t(job.operation), job.dataset].filter(Boolean).join(" · "), recoveryKey: "recoverSlowRecording"},
      60000, job.id);
    const home = status.homing;
    if (home?.state === "FAILED" && status.reason !== "HOMING_FAILED")
      this.push(`homing:${home.id}:${home.error}`, home.error || "HOMING_FAILED",
        {detail: this.t(home.target === "leader" ? "homeLeader" : "homeRobot")});
  }
}
