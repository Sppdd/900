"""Install third-party Agent Skills (SKILL.md folders) into ~/.sandcoder/skills, pinned and reviewed.

Sources:
  github:OWNER/REPO[/PATH][@REF]         e.g. github:mattpocock/skills/skills/engineering/tdd@c55ee46
  https://github.com/OWNER/REPO[/tree/REF/PATH]
  ./local/path  or  ~/.claude/skills/tdd
A path that contains several skills (e.g. a repo's skills/ folder) installs each of them.

Every install is pinned: the resolved git commit and a content hash are written to
``<skill>/.source.json``. ``inspect`` produces a review (files, scripts, risky patterns)
that a human approves before anything is installed. Skill scripts only ever run inside
sandboxes, never on this machine.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from sandcoder import guard
from sandcoder.skills import BUILTIN_DIR, MAX_SKILL_BYTES, MAX_SKILL_FILES, folder_digest, installed_dir, parse_frontmatter

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
GH_URL_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:/tree/([^/]+)(/.*)?)?/?$")
INJECTION_HINTS = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|disregard (the )?system prompt|"
    r"exfiltrat|send (the |your )?(api[_ ]?key|token|credentials)|\bNEBIUS_API_KEY\b",
    re.I,
)


@dataclass
class Source:
    kind: str  # "github" | "local"
    location: str  # "owner/repo" or a local path
    path: str = ""  # path inside the repo
    ref: str = ""  # branch / tag / commit (github)

    def label(self) -> str:
        if self.kind == "local":
            return self.location
        return f"github:{self.location}{'/' + self.path if self.path else ''}{'@' + self.ref if self.ref else ''}"


def parse_source(spec: str) -> Source:
    spec = spec.strip()
    if spec.startswith("github:"):
        rest, _, ref = spec[len("github:") :].partition("@")
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 2:
            raise ValueError("github source must look like github:OWNER/REPO[/PATH][@REF]")
        return Source("github", "/".join(parts[:2]), "/".join(parts[2:]), ref)
    m = GH_URL_RE.match(spec)
    if m:
        owner, repo, ref, path = m.groups()
        return Source("github", f"{owner}/{repo}", (path or "").strip("/"), ref or "")
    p = Path(spec).expanduser()
    if p.exists():
        return Source("local", str(p.resolve()))
    raise ValueError(f"not a github source or existing path: {spec}")


@dataclass
class Candidate:
    name: str
    folder: Path
    description: str
    files: list[str]
    total_bytes: int
    scripts: list[str]
    warnings: list[str] = field(default_factory=list)
    digest: str = ""


@dataclass
class Fetched:
    source: Source
    root: Path  # folder that was searched for skills
    commit: str
    candidates: list[Candidate]
    tmp: tempfile.TemporaryDirectory | None = None

    def cleanup(self) -> None:
        if self.tmp:
            self.tmp.cleanup()


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=180).stdout.strip()


def fetch(spec: str) -> Fetched:
    """Download (if remote) and inspect every skill under the source. Nothing is installed yet."""
    src = parse_source(spec)
    tmp = None
    commit = ""
    if src.kind == "github":
        tmp = tempfile.TemporaryDirectory(prefix="sandcoder-skill-")
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        try:
            _git("init", "-q", cwd=repo)
            _git("remote", "add", "origin", f"https://github.com/{src.location}.git", cwd=repo)
            _git("fetch", "-q", "--depth", "1", "origin", src.ref or "HEAD", cwd=repo)
            _git("checkout", "-q", "FETCH_HEAD", cwd=repo)
            commit = _git("rev-parse", "HEAD", cwd=repo)
        except subprocess.CalledProcessError as e:
            tmp.cleanup()
            raise ValueError(f"could not fetch {src.label()}: {e.stderr.strip()[:300]}") from None
        root = (repo / src.path) if src.path else repo
    else:
        root = Path(src.location)
    if not root.is_dir():
        if tmp:
            tmp.cleanup()
        raise ValueError(f"path not found in source: {src.path or src.location}")
    folders = [root] if (root / "SKILL.md").is_file() else sorted(p.parent for p in root.rglob("SKILL.md"))
    candidates = [inspect(f) for f in folders if ".git" not in f.parts]
    return Fetched(src, root, commit, candidates, tmp)


def inspect(folder: Path) -> Candidate:
    """Build a human-reviewable summary of a skill folder, with warnings for anything risky."""
    folder = folder.resolve()
    meta, body = parse_frontmatter((folder / "SKILL.md").read_text(errors="replace"))
    name = (meta.get("name") or folder.name).strip().lower()
    files, total, scripts, warnings = [], 0, [], []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        files.append(rel)
        total += p.stat().st_size
        if rel.startswith("scripts/") or p.suffix in (".sh", ".py", ".js", ".ts", ".rb") or p.stat().st_mode & 0o111:
            scripts.append(rel)
        if p.stat().st_size < 512_000:
            text = p.read_text(errors="replace")
            for rule, pattern, reason in guard.DENY_RULES:
                if re.search(pattern, text, re.I | re.M):
                    warnings.append(f"{rel}: contains a pattern that {reason} ({rule})")
            if INJECTION_HINTS.search(text):
                warnings.append(f"{rel}: contains prompt-injection-like text")
    if not meta.get("name"):
        warnings.append("SKILL.md has no `name` frontmatter; using the folder name")
    if not meta.get("description"):
        warnings.append("SKILL.md has no `description`; agents won't know when to use it")
    if len(files) > MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
        warnings.append(f"large skill ({len(files)} files, {total} bytes); only the first {MAX_SKILL_FILES} files are mounted")
    if not NAME_RE.match(name):
        warnings.append(f"invalid skill name {name!r}")
    if re.search(r"\b(ask the user|wait for (the user|approval)|AskUserQuestion)\b", body, re.I):
        warnings.append("interactive skill: in a sandbox it will assume answers instead of asking")
    return Candidate(name, folder, meta.get("description", ""), files, total, scripts, warnings, folder_digest(folder))


def install(fetched: Fetched, names: list[str] | None = None, *, force: bool = False, dest: Path | None = None) -> list[str]:
    """Copy approved candidates into ``dest`` with a pinned .source.json. Returns installed names."""
    dest = dest or installed_dir()
    builtin = {p.name for p in BUILTIN_DIR.iterdir() if p.is_dir()}
    installed = []
    dest.mkdir(parents=True, exist_ok=True)
    for c in fetched.candidates:
        if names is not None and c.name not in names:
            continue
        if not NAME_RE.match(c.name):
            raise ValueError(f"refusing invalid skill name {c.name!r}")
        if c.name in builtin:
            raise ValueError(f"{c.name!r} would shadow a built-in specialist; rename it")
        target = dest / c.name
        if target.exists() and not force:
            raise ValueError(f"{c.name} is already installed (use --force to replace)")
        staging = dest / f".{c.name}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(c.folder, staging, symlinks=False, ignore=shutil.ignore_patterns(".git", ".*"))
        rel = c.folder.relative_to(fetched.root.resolve()).as_posix() if fetched.source.kind == "github" else ""
        (staging / ".source.json").write_text(json.dumps({
            "source": fetched.source.label(),
            "kind": fetched.source.kind,
            "repo": fetched.source.location if fetched.source.kind == "github" else None,
            "path": "/".join(p for p in (fetched.source.path, rel if rel != "." else "") if p),
            "commit": fetched.commit or None,
            "digest": folder_digest(staging),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2))
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        installed.append(c.name)
    return installed


def remove(name: str, dest: Path | None = None) -> bool:
    dest = dest or installed_dir()
    target = dest / name
    if not NAME_RE.match(name) or not (target / "SKILL.md").is_file():
        return False
    shutil.rmtree(target)
    return True


def installed(dest: Path | None = None) -> list[dict]:
    """Installed skills with pin info; ``tampered`` is True if files changed since install."""
    dest = dest or installed_dir()
    out = []
    if not dest.is_dir():
        return out
    for folder in sorted(p for p in dest.iterdir() if p.is_dir() and not p.name.startswith(".")):
        meta_file = folder / ".source.json"
        meta = json.loads(meta_file.read_text()) if meta_file.is_file() else {}
        out.append({
            "name": folder.name,
            "source": meta.get("source", "unknown"),
            "commit": (meta.get("commit") or "")[:12],
            "full_commit": meta.get("commit") or "",
            "repo": meta.get("repo"),
            "path": meta.get("path") or "",
            "kind": meta.get("kind", "local"),
            "digest": meta.get("digest", ""),
            "tampered": bool(meta) and meta.get("digest") != folder_digest(folder),
        })
    return out


def review_text(fetched: Fetched) -> str:
    lines = [f"Source: {fetched.source.label()}" + (f"  (commit {fetched.commit[:12]})" if fetched.commit else "")]
    for c in fetched.candidates:
        lines.append(f"\n• {c.name}  [{len(c.files)} files, {c.total_bytes} bytes, hash {c.digest}]")
        if c.description:
            lines.append(f"  {c.description[:200]}")
        if c.scripts:
            lines.append(f"  scripts (run only inside sandboxes): {', '.join(c.scripts[:8])}")
        for w in c.warnings:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines)
