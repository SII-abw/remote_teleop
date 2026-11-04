"""Observe the deployed real stack; no control or mode command is ever sent."""

import http.cookiejar
import json
import ssl
import time
import urllib.request
from pathlib import Path

from remote_teleop.config import AppConfig, atomic_json


def main():
    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig.model_validate_json((root / "configs/siinuc/master.json").read_text())
    assert not cfg.general.simulation
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = f"http://127.0.0.1:{cfg.ui.port}"

    def api(path, data=None):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "X-Teleop-Request": "1"},
        )
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    entries = json.loads(Path(cfg.security.credentials_file).read_text())["credentials"]
    api("/api/login", {"token": next(e["token"] for e in entries if e["role"] == "ui")})
    try:
        samples = []
        for _ in range(40):
            samples.append(api("/api/status"))
            time.sleep(0.25)
        for sample in samples[4:]:
            assert sample["simulation"] is False, sample
            assert isinstance(sample["motion_permitted"], bool), sample
            assert sample["mode"] == "DISARMED", sample
            assert len(sample["q"]) == 14, sample
            assert sample["connected"] and sample["healthy"], sample
            assert sample["leader_error"] is None, sample
            assert len(sample["video"]) == 3, sample
            assert max(c["age_ms"] for c in sample["video"].values()) < 500, sample
        configurations = {role: api(f"/api/config/{role}") for role in ("client", "robot", "cloud")}
        assert all(not c["config"]["general"]["simulation"] for c in configurations.values())
        configs = [AppConfig.model_validate(c["config"]) for c in configurations.values()]
        assert len({c.control_contract for c in configs}) == 1, "Control configurations differ"
        assert samples[-1]["attended_limits"] == cfg.safety.attended_test.model_dump()
        context = ssl.create_default_context(cafile=cfg.network.ca_file)
        with urllib.request.urlopen("https://127.0.0.1:8800/health", context=context, timeout=5) as response:
            assert json.load(response)["ok"]
        report = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "samples": len(samples),
            "verified_tls": True,
            "configurations": {
                role: {"revision": c["revision"], "simulation": False} for role, c in configurations.items()
            },
            "last_status": samples[-1],
            "max_state_age_ms": max(s["state_age_ms"] for s in samples[4:]),
            "max_video_age_ms": {
                name: max(s["video"][name]["age_ms"] for s in samples[4:])
                for name in ("main", "left_hand", "right_hand")
            },
            "datasets_found": len(api("/api/datasets")),
            "motion_tested": False,
            "safety_checks": cfg.safety.checks.model_dump(),
            "attended_limits": cfg.safety.attended_test.model_dump(),
        }
        atomic_json(root / "state/siinuc/deployment-check.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        api("/api/logout", {})


if __name__ == "__main__":
    main()
