"""Sandbox profiles: a ``sandbox.toml`` that describes a reusable sandbox filled with skills.

    name = "py-backend"
    image = "python:3.12-slim"
    toolchain = ["pip install -q ruff"]            # baked into a cached, tagged image
    setup = ["pip install -r requirements.txt"]    # per run, after project files are added
    test = "python -m pytest -q"
    specialists = ["security-auditor", "test-writer"]

    [skills]                                        # library skills mounted in every sandbox
    tdd = "github:mattpocock/skills/skills/engineering/tdd@c55ee46"

Put it at the project root (picked up automatically) or pass it explicitly. Installing the
skills a profile references is always a human step (``sandcoder profile install``), so an
agent can't pull new third-party instructions into your sandboxes on its own.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sandcoder import skillhub

PROFILE_FILE = "sandbox.toml"


@dataclass
class Profile:
    name: str = "default"
    image: str = "python:3.12-slim"
    toolchain: list[str] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    test: str | None = None
    specialists: list[str] = field(default_factory=list)
    skills: dict[str, str] = field(default_factory=dict)  # name -> source
    path: str = ""


def load_profile(path: Path | str) -> Profile:
    p = Path(path)
    if p.is_dir():
        p = p / PROFILE_FILE
    data = tomllib.loads(p.read_text())
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("[skills] must be a table of name = \"source\"")
    return Profile(
        name=str(data.get("name", p.parent.name)),
        image=str(data.get("image", "python:3.12-slim")),
        toolchain=[str(c) for c in data.get("toolchain", [])],
        setup=[str(c) for c in data.get("setup", [])],
        test=data.get("test"),
        specialists=[str(s) for s in data.get("specialists", [])],
        skills={str(k): _resolve_local(str(v), p.parent) for k, v in skills.items()},
        path=str(p),
    )


def _resolve_local(source: str, base: Path) -> str:
    """Relative local skill paths are relative to the profile file, not the current directory."""
    if source.startswith(("github:", "http://", "https://", "/", "~")):
        return source
    return str((base / source).resolve())


def find_profile(project: Path | str) -> Profile | None:
    p = Path(project) / PROFILE_FILE
    return load_profile(p) if p.is_file() else None


def skill_status(profile: Profile) -> dict[str, str]:
    """Per referenced skill: "ok", "missing", "other-source" (installed from a different source), or "tampered"."""
    have = {s["name"]: s for s in skillhub.installed()}
    out = {}
    for name, spec in profile.skills.items():
        inst = have.get(name)
        if not inst:
            out[name] = "missing"
        elif inst["tampered"]:
            out[name] = "tampered"
        elif _same_source(inst, spec):
            out[name] = "ok"
        else:
            out[name] = "other-source"
    return out


def _same_source(inst: dict, spec: str) -> bool:
    """Does an installed skill come from the repo/path/commit a profile asks for?"""
    try:
        want = skillhub.parse_source(spec)
    except ValueError:
        return False
    if want.kind == "local":
        return inst["kind"] == "local" and inst["source"] == want.location
    if inst["kind"] != "github" or inst["repo"] != want.location or inst["path"].strip("/") != want.path.strip("/"):
        return False
    is_sha = len(want.ref) >= 7 and all(c in "0123456789abcdef" for c in want.ref.lower())
    return not is_sha or inst["full_commit"].startswith(want.ref.lower())


def toml_for(profile: Profile) -> str:
    """Render a profile back to sandbox.toml (used by the CLI `profile init`)."""
    def arr(xs: list[str]) -> str:
        return "[" + ", ".join(_q(x) for x in xs) + "]"

    lines = [f"name = {_q(profile.name)}", f"image = {_q(profile.image)}", f"toolchain = {arr(profile.toolchain)}",
             f"setup = {arr(profile.setup)}"]
    if profile.test:
        lines.append(f"test = {_q(profile.test)}")
    lines.append(f"specialists = {arr(profile.specialists)}")
    lines.append("\n[skills]")
    lines += [f"{_q(k)} = {_q(v)}" for k, v in profile.skills.items()]
    return "\n".join(lines) + "\n"


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
