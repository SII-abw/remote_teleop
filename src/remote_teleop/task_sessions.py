"""Robot-owned task prompts and durable, monotonic episode allocation."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import re
import threading

from .config import atomic_json


class TaskSessions:
    def __init__(self, root, prompt):
        self.root = Path(root).resolve()
        self.path = self.root / ".teleop-tasks.json"
        self.lock = threading.RLock()
        self.data = {"version": 1, "active": "task_001", "tasks": [
            {"id": "task_001", "prompt": prompt, "next_episode": 1}
        ]}
        self._refresh()

    def _refresh(self):
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        tasks = data.get("tasks", [])
        if (data.get("version") != 1 or not tasks or len(tasks) > 1000
                or len({t.get("id") for t in tasks}) != len(tasks)
                or data.get("active") not in {t.get("id") for t in tasks}):
            raise ValueError("TASK_CATALOG_INVALID")
        for task in tasks:
            if (not re.fullmatch(r"task_\d{3,}", task.get("id", ""))
                    or not isinstance(task.get("prompt"), str) or not task["prompt"].strip()
                    or len(task["prompt"]) > 1000
                    or type(task.get("next_episode")) is not int or task["next_episode"] < 1):
                raise ValueError("TASK_CATALOG_INVALID")
        self.data = data

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".teleop-tasks.lock").open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                self._refresh()
                original = deepcopy(self.data)
                try:
                    yield
                    atomic_json(self.path, self.data)
                except BaseException:
                    self.data = original
                    raise

    def _active(self):
        return next(t for t in self.data["tasks"] if t["id"] == self.data["active"])

    def _next_number(self, task):
        number = task["next_episode"]
        if self.root.exists():
            pattern = re.compile(re.escape(task["id"]) + r"_episode_(\d{6,})$")
            for path in self.root.iterdir():
                match = pattern.fullmatch(path.name)
                if match:
                    number = max(number, int(match[1]) + 1)
        return number

    @staticmethod
    def _name(task, number):
        return f"{task['id']}_episode_{number:06d}"

    def snapshot(self, catalog=False):
        with self.lock:
            task = self._active()
            result = {"automatic_naming": True, "active": {"id": task["id"], "prompt": task["prompt"]},
                      "next_dataset_name": self._name(task, task["next_episode"])}
            if catalog:
                result["tasks"] = [{"id": t["id"], "prompt": t["prompt"]} for t in self.data["tasks"]]
            return deepcopy(result)

    def preview(self):
        # Preflight never creates a directory, reserves a number, or overwrites data.
        with self.lock:
            self._refresh()
            task = self._active()
            task["next_episode"] = self._next_number(task)
            return self.snapshot()

    def reserve_episode(self):
        with self._transaction():
            task = self._active()
            number = self._next_number(task)
            task["next_episode"] = number + 1
            result = {"name": self._name(task, number), "task": {"id": task["id"], "prompt": task["prompt"]}}
        return result

    def create(self, prompt):
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1000:
            raise ValueError("INVALID_TASK_PROMPT")
        with self._transaction():
            if len(self.data["tasks"]) >= 1000:
                raise ValueError("TASK_LIMIT")
            number = max(int(t["id"].split("_")[1]) for t in self.data["tasks"]) + 1
            task = {"id": f"task_{number:03d}", "prompt": prompt.strip(), "next_episode": 1}
            self.data["tasks"].append(task)
            self.data["active"] = task["id"]
        return self.snapshot(catalog=True)

    def select(self, task_id):
        with self._transaction():
            if task_id not in {t["id"] for t in self.data["tasks"]}:
                raise ValueError("TASK_NOT_FOUND")
            self.data["active"] = task_id
            task = self._active()
            task["next_episode"] = self._next_number(task)
        return self.snapshot(catalog=True)
