"""Synchronize shared control configuration while retaining role-local settings."""

import asyncio
from copy import deepcopy

from .config import AppConfig


ROLES = ("robot", "cloud", "client")
SHARED_GROUPS = ("general", "safety", "robot", "leader", "arm_profiles", "cameras", "policy", "recording")


class ConfigSynchronizer:
    def __init__(self, read, save, apply=None):
        self.read, self.save, self.apply = read, save, apply

    async def update(self, target, data, revision):
        desired = AppConfig.model_validate(data).model_dump()
        snapshots = dict(zip(ROLES, await asyncio.gather(*(self.read(role) for role in ROLES))))
        if snapshots[target]["revision"] != revision:
            raise ValueError("CONFIG_CONFLICT")
        candidates = {}
        for role, snapshot in snapshots.items():
            values = deepcopy(desired if role == target else snapshot["config"])
            for group in SHARED_GROUPS:
                values[group] = deepcopy(desired[group])
            for field in ("telemetry_hz", "observation_hz"):
                values["network"][field] = desired["network"][field]
            candidates[role] = AppConfig.model_validate(values)
        if len({cfg.control_contract for cfg in candidates.values()}) != 1:
            raise ValueError("CONFIG_SYNC_CONTRACT")
        expected_live = {}
        if self.apply:
            for role in ROLES:
                active = deepcopy(snapshots[role]["active_config"])
                active["safety"] = candidates[role].safety.model_dump()
                expected_live[role] = AppConfig.model_validate(active)
            if len({cfg.control_contract for cfg in expected_live.values()}) != 1:
                raise ValueError("CONFIG_SYNC_CONTRACT")

        results, attempted = dict(snapshots), []
        try:
            for role in ROLES:
                if candidates[role] == AppConfig.model_validate(snapshots[role]["config"]):
                    continue
                attempted.append(role)
                results[role] = await self.save(
                    role, candidates[role].model_dump(), snapshots[role]["revision"]
                )
        except Exception as error:
            # A timed-out RPC may already have written its file. Inspect every
            # attempted target before rolling back, using its current revision.
            # Never overwrite a concurrent edit that differs from our proposal.
            incomplete = []
            for role in reversed(attempted):
                try:
                    current = await self.read(role)
                    actual = AppConfig.model_validate(current["config"])
                    original = AppConfig.model_validate(snapshots[role]["config"])
                    if actual == original:
                        continue
                    if actual != candidates[role]:
                        incomplete.append(role)
                        continue
                    await self.save(role, original.model_dump(), current["revision"])
                except Exception:
                    incomplete.append(role)
            raise ValueError("CONFIG_SYNC_PARTIAL" if incomplete else "CONFIG_SYNC_FAILED") from error
        if self.apply:
            try:
                # Files are consistent before any live snapshot changes. Retry
                # also applies unchanged files, repairing an interrupted update.
                for role in ROLES:
                    await self.apply(role, results[role]["revision"])
                results = dict(zip(ROLES, await asyncio.gather(*(self.read(role) for role in ROLES))))
                for role in ROLES:
                    if (
                        results[role]["config"] != candidates[role].model_dump()
                        or results[role]["active_contract"] != expected_live[role].control_contract
                        or results[role]["active_config"]["safety"] != candidates[role].safety.model_dump()
                    ):
                        raise ValueError("CONFIG_LIVE_VERIFY_FAILED")
            except Exception as error:
                # Do not claim success or undo only some live snapshots. The
                # gateway blocks motion until all full live contracts agree.
                raise ValueError("CONFIG_APPLY_PENDING") from error
        return {
            **results[target],
            "synchronized_targets": list(ROLES),
            "restart_targets": [role for role in ROLES if results[role]["restart_required"]],
            "hot_applied": bool(self.apply),
        }
