import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID


ROLES = ("overseer", "implementation", "chat")
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_REASONING_EFFORT = "max"


@dataclass
class ProjectConfig:
    data: dict
    runtimes: dict
    root: Path

    def placement(self, role):
        placement = self.data["placement"][role]
        if "runtime" in placement or "host" in placement:
            raise ValueError("Choose one runtime for the project; role-specific hosts/runtimes are not allowed")
        runtime_id = self.data["runtime"]
        runtime = self.runtimes[runtime_id]
        sources = self.data.get("instructions", []) + placement.get("instructions", [])
        return {
            "host_id": runtime["host"],
            "runtime_id": runtime_id,
            "cwd": placement["cwd"],
            "instructions": "\n\n".join((self.root / source).read_text() for source in sources),
            "config": {"model_reasoning_effort": DEFAULT_REASONING_EFFORT, **placement.get("config", {})},
            "model": placement.get("model", DEFAULT_MODEL),
        }

    @property
    def key(self):
        return os.environ[self.data["dispatcher_key_env"]]


@dataclass
class RuntimeConfig:
    api_url: str
    runtimes: dict
    projects: list[ProjectConfig]


def load_config(path):
    path = Path(path)
    data = json.loads(path.read_text())
    runtimes = data["runtimes"]
    for runtime in runtimes.values():
        if not runtime.get("host") or urlparse(runtime.get("url", "")).scheme not in ("ws", "wss"):
            raise ValueError("Each runtime needs a host identity and a ws:// or wss:// URL")
    projects = []
    seen = set()
    for project in data["projects"]:
        identifier = str(UUID(project["project_id"]))
        if identifier in seen:
            raise ValueError("Duplicate project configuration")
        seen.add(identifier)
        project["project_id"] = identifier
        if project.get("runtime") not in runtimes:
            raise ValueError("Each project must select one configured runtime for all its threads")
        UUID(project["worker_id"])
        configured = ProjectConfig(project, runtimes, path.parent)
        for role in ROLES:
            placement = configured.placement(role)
            if not placement["cwd"].startswith("/"):
                raise ValueError("Role working directories must be absolute host paths")
        projects.append(configured)
    return RuntimeConfig(data.get("api_url", "http://api:8000").rstrip("/"), runtimes, projects)
