"""Sandbox backends.

A sandbox here is *immutable and branchable*: every command runs against a
``Snapshot`` and (optionally) produces a new ``Snapshot``. Old snapshots stay
valid, so an agent can checkpoint, roll back, or fork N attempts from the same
state. This maps 1:1 onto Token Factory Sandboxes (ConTree), where each run
yields a new image version.

``ContreeSandbox`` is the real, VM-isolated backend. ``LocalSandbox`` mimics
the same semantics with directory copies on the host; it gives NO isolation
and exists for offline tests and demos only.
"""

from __future__ import annotations

import asyncio
import fnmatch
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

WORKDIR = "/workspace"
DEFAULT_TIMEOUT = 300.0
MAX_OUTPUT_BYTES = 256 * 1024
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}


@dataclass(frozen=True)
class Snapshot:
    """An immutable filesystem state inside a sandbox."""

    id: str
    handle: Any = None


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxError(RuntimeError):
    pass


def safe_relpath(path: str) -> str:
    """Normalize a workspace-relative path, rejecting escapes out of the workspace."""
    p = PurePosixPath(path)
    if p.is_absolute():
        try:
            p = p.relative_to(WORKDIR)
        except ValueError:
            raise SandboxError(f"path must be inside {WORKDIR}: {path}") from None
    parts = [part for part in p.parts if part not in ("", ".")]
    if ".." in parts:
        raise SandboxError(f"path may not contain '..': {path}")
    if not parts:
        raise SandboxError("empty path")
    return "/".join(parts)


SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".npmrc", ".pypirc", ".netrc", "*.tfstate", "*.tfstate.*", "credentials*.json", "*secret*",
)
SECRET_ALLOW = (".env.example", ".env.sample", ".env.template")
MAX_UPLOAD_FILE_BYTES = 5 * 1024 * 1024


def is_secret_path(rel: str) -> bool:
    """True for files that must never leave the user's machine (keys, .env, tfstate...)."""
    name = PurePosixPath(rel).name
    if name in SECRET_ALLOW:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)


def _git_listed_files(root: Path) -> list[str] | None:
    """Tracked + untracked-but-not-ignored files, or None if ``root`` isn't a git work tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in out.decode().split("\0") if p]


def collect_local_files(root: Path | str) -> dict[str, tuple[bytes, int]]:
    """Read a project into {relpath: (content, mode)} for upload.

    Respects .gitignore when ``root`` is in a git repo, skips caches/VCS dirs and
    oversized files, and ALWAYS drops secret-looking files (see ``SECRET_PATTERNS``).
    """
    root = Path(root)
    listed = _git_listed_files(root)
    if listed is None:
        listed = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            listed += [(Path(dirpath) / n).relative_to(root).as_posix() for n in filenames]
    out: dict[str, tuple[bytes, int]] = {}
    for rel in listed:
        if is_secret_path(rel) or any(part in SKIP_DIRS for part in PurePosixPath(rel).parts):
            continue
        full = root / rel
        if full.is_symlink() or not full.is_file() or full.stat().st_size > MAX_UPLOAD_FILE_BYTES:
            continue
        out[rel] = (full.read_bytes(), full.stat().st_mode & 0o777)
    return out


class Sandbox(ABC):
    """Branchable execution environment. All paths are relative to the workspace."""

    workdir: str = WORKDIR

    @abstractmethod
    async def start(self, image: str, files: dict[str, tuple[bytes, int]] | None = None) -> Snapshot:
        """Create the initial snapshot from ``image`` with ``files`` placed in the workspace."""

    @abstractmethod
    async def exec(
        self, snap: Snapshot, command: str, *, timeout: float = DEFAULT_TIMEOUT, persist: bool = True
    ) -> tuple[ExecResult, Snapshot]:
        """Run a shell command in the workspace.

        With ``persist=False`` the filesystem changes are discarded and the input
        snapshot is returned unchanged (use for read-only probes and test runs).
        """

    @abstractmethod
    async def write(self, snap: Snapshot, files: dict[str, bytes]) -> Snapshot:
        """Return a new snapshot with ``files`` written into the workspace."""

    @abstractmethod
    async def add_files(self, snap: Snapshot, files: dict[str, tuple[bytes, int]]) -> Snapshot:
        """Like ``write`` but with explicit file modes (used for project uploads)."""

    async def toolchain(
        self, image: str, setup: list[str], *, tag: str | None = None, timeout: float = 900
    ) -> Snapshot:
        """An image with ``setup`` commands applied. Backends may cache it under ``tag``."""
        snap = await self.start(image)
        for cmd in setup:
            res, snap = await self.exec(snap, cmd, timeout=timeout)
            if not res.ok:
                raise SandboxError(f"toolchain setup failed: {cmd}\n{res.stdout}\n{res.stderr}")
        return snap

    @abstractmethod
    async def read(self, snap: Snapshot, path: str) -> bytes:
        """Read a workspace file from a snapshot."""

    @abstractmethod
    async def export(self, snap: Snapshot, dest: Path) -> Path:
        """Copy the snapshot's workspace to a local directory."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass

    async def __aenter__(self) -> Sandbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# --------------------------------------------------------------------------- #
# Token Factory Sandboxes (ConTree)
# --------------------------------------------------------------------------- #


class ContreeSandbox(Sandbox):
    """Token Factory Sandboxes backend, built on ``contree-sdk``.

    Every persisted command produces a new ConTree image; the ``Snapshot``
    wraps that image object, so branching is just "run again from an older one".
    """

    def __init__(self, sdk: Any, api_client: Any = None) -> None:
        """Wrap an existing ``contree_sdk.Contree``; prefer :meth:`from_env`."""
        self._sdk = sdk
        self._api_client = api_client

    @classmethod
    def from_env(cls, *, truncate_output_at: int = MAX_OUTPUT_BYTES) -> ContreeSandbox:
        """Build from env vars, falling back to the ``contree auth`` profile (~/.config/contree/auth.ini).

        Env: ``NEBIUS_API_KEY`` (or ``CONTREE_TOKEN``); ``CONTREE_PROJECT`` (or
        ``NEBIUS_PROJECT_ID`` / ``NEBIUS_AI_PROJECT``); optional ``CONTREE_URL``.
        """
        from contree_sdk import Contree

        token = os.environ.get("CONTREE_TOKEN") or os.environ.get("NEBIUS_API_KEY")
        project = next(
            (os.environ[k] for k in ("CONTREE_PROJECT", "NEBIUS_PROJECT_ID", "NEBIUS_AI_PROJECT") if os.environ.get(k)),
            None,
        )
        url = os.environ.get("CONTREE_URL")

        try:  # contree-sdk <= 0.3.x: the SDK owns auth via ContreeConfig
            from contree_sdk.auth import IAMAuth
            from contree_sdk.config import ContreeConfig
        except ImportError:  # newer SDKs take a pre-built contree_client transport
            from contree_client.httpx import ContreeAsyncClient

            if not token:
                client = ContreeAsyncClient.from_profile()
            else:
                kw: dict[str, Any] = {k: v for k, v in (("base_url", url), ("project", project)) if v}
                client = ContreeAsyncClient(token, **kw)
            return cls(Contree(client, default_truncate_output_at=truncate_output_at), client)

        auth = IAMAuth()  # defaults read NEBIUS_API_KEY / NEBIUS_PROJECT_ID / auth.ini
        overrides = {k: v for k, v in (("token", token), ("project_id", project), ("base_url", url)) if v}
        config = ContreeConfig(auth=replace(auth, **overrides), default_truncate_output_at=truncate_output_at)
        return cls(Contree(config))

    async def close(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()

    async def list_images(self, limit: int = 50) -> list[tuple[str, str | None]]:
        images = await self._sdk.images(number=limit)
        return [(str(i.uuid), i.tag) for i in images]

    async def start(self, image: str, files: dict[str, tuple[bytes, int]] | None = None) -> Snapshot:
        # oci() returns an already-imported image by tag, importing from the registry if needed.
        base = await self._sdk.images.oci(image)
        img = await base.run(shell=f"mkdir -p {self.workdir}", disposable=False)
        snap = self._snap(img)
        return await self.add_files(snap, files) if files else snap

    async def add_files(self, snap: Snapshot, files: dict[str, tuple[bytes, int]]) -> Snapshot:
        from contree_sdk.utils.models.file import UploadFileSpec

        specs = {
            f"{self.workdir}/{safe_relpath(rel)}": UploadFileSpec(source=data, mode=mode)
            for rel, (data, mode) in files.items()
        }
        return self._snap(await snap.handle.apply_files(files=specs))

    async def toolchain(
        self, image: str, setup: list[str], *, tag: str | None = None, timeout: float = 900
    ) -> Snapshot:
        # Snapshots are immutable, so a tagged toolchain image can be reused by every later run.
        if tag:
            try:
                return self._snap(await self._sdk.images.use(tag, strict=True))
            except Exception:  # not built yet
                pass
        snap = await super().toolchain(image, setup, timeout=timeout)
        if tag:
            await snap.handle.tag_as(tag)
        return snap

    async def exec(
        self, snap: Snapshot, command: str, *, timeout: float = DEFAULT_TIMEOUT, persist: bool = True
    ) -> tuple[ExecResult, Snapshot]:
        t0 = time.monotonic()
        try:
            img = await snap.handle.run(
                shell=command, cwd=self.workdir, timeout=timeout, disposable=not persist
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # timeouts and operation failures surface as a failed command
            return ExecResult("", f"{type(e).__name__}: {e}", 124, time.monotonic() - t0), snap
        result = ExecResult(
            stdout=_text(img.stdout),
            stderr=_text(img.stderr),
            exit_code=img.exit_code,
            duration=time.monotonic() - t0,
        )
        return result, (self._snap(img) if persist else snap)

    async def write(self, snap: Snapshot, files: dict[str, bytes]) -> Snapshot:
        mapping = {f"{self.workdir}/{safe_relpath(k)}": v for k, v in files.items()}
        return self._snap(await snap.handle.apply_files(files=mapping))

    async def read(self, snap: Snapshot, path: str) -> bytes:
        return await snap.handle.read(f"{self.workdir}/{safe_relpath(path)}")

    async def export(self, snap: Snapshot, dest: Path) -> Path:
        archive = f"/tmp/sandcoder-{uuid.uuid4().hex}.tar"
        res, tar_snap = await self.exec(snap, f"tar -cf {archive} -C {self.workdir} .")
        if not res.ok:
            raise SandboxError(f"export failed: {res.stderr}")
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "ws.tar"
            await tar_snap.handle.download(archive, local)
            with tarfile.open(local) as tf:
                tf.extractall(dest, filter="data")
        return dest

    @staticmethod
    def _snap(img: Any) -> Snapshot:
        return Snapshot(id=str(img.uuid), handle=img)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


# --------------------------------------------------------------------------- #
# Local backend (no isolation: tests/demos only)
# --------------------------------------------------------------------------- #


class LocalSandbox(Sandbox):
    """Host-directory emulation of branchable snapshots. NOT ISOLATED.

    Each snapshot is a directory; persisted commands run in a fresh copy. The
    ``image`` argument is ignored — commands run with the host's toolchain.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._tmp = None if root else tempfile.TemporaryDirectory(prefix="sandcoder-")
        self.root = Path(root or self._tmp.name)  # type: ignore[union-attr]
        self.root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        if self._tmp:
            self._tmp.cleanup()

    def _new_dir(self, parent: Snapshot | None) -> Path:
        d = self.root / uuid.uuid4().hex
        if parent is None:
            d.mkdir()
        else:
            shutil.copytree(parent.handle, d, symlinks=True)
        return d

    async def start(self, image: str, files: dict[str, tuple[bytes, int]] | None = None) -> Snapshot:
        d = self._new_dir(None)
        snap = Snapshot(id=d.name, handle=d)
        return await self.add_files(snap, files) if files else snap

    async def add_files(self, snap: Snapshot, files: dict[str, tuple[bytes, int]]) -> Snapshot:
        d = self._new_dir(snap)
        for rel, (data, mode) in files.items():
            p = d / safe_relpath(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(mode)
        return Snapshot(id=d.name, handle=d)

    async def exec(
        self, snap: Snapshot, command: str, *, timeout: float = DEFAULT_TIMEOUT, persist: bool = True
    ) -> tuple[ExecResult, Snapshot]:
        d = self._new_dir(snap)
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=d,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            code = proc.returncode or 0
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            err += f"\n[timed out after {timeout}s]".encode()
            code = 124
        result = ExecResult(
            stdout=out[:MAX_OUTPUT_BYTES].decode(errors="replace"),
            stderr=err[:MAX_OUTPUT_BYTES].decode(errors="replace"),
            exit_code=code,
            duration=time.monotonic() - t0,
        )
        if not persist:
            shutil.rmtree(d, ignore_errors=True)
            return result, snap
        return result, Snapshot(id=d.name, handle=d)

    async def write(self, snap: Snapshot, files: dict[str, bytes]) -> Snapshot:
        d = self._new_dir(snap)
        for rel, data in files.items():
            p = d / safe_relpath(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return Snapshot(id=d.name, handle=d)

    async def read(self, snap: Snapshot, path: str) -> bytes:
        p = Path(snap.handle) / safe_relpath(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        return p.read_bytes()

    async def export(self, snap: Snapshot, dest: Path) -> Path:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(snap.handle, arcname=".")
        buf.seek(0)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=buf) as tf:
            tf.extractall(dest, filter="data")
        return dest
