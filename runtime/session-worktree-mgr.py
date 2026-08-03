#!/usr/bin/env python3
"""
session-worktree-mgr.py

Fixed worktree pool + persistent OpenCode session dispatcher.

Design:
- Keep OpenCode API directory = worktree path.
- Worktrees are long-lived pool resources.
- pool init/repair creates the wt_N worktrees themselves and synchronizes
  the .opencode/node_modules from main. Sessions are NOT created at
  init/repair time — they are created lazily on the first dispatch
  (``ensure_session(..., recreate_existing=True)``), so unused pool
  slots do not hold warm OpenCode sessions.
- prepare grabs an idle worktree and checks out the task branch.
- dispatch uses session id from state; if the id is missing (first
  dispatch on a fresh worktree), it creates the session, persists the
  id, and dispatches — then re-uses the id on subsequent dispatches.
- release resets the worktree and marks it idle; it does not delete
  sessions (cleanup_stale_sessions is the only path that evicts state
  pointers, and it does so only after archiving/unwatching in OpenCode).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re

try:
    import fcntl  # POSIX-only; Windows falls back to best-effort (atomic os.replace only)
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeGuard, cast

DEFAULT_AGENTS = ("Daedalus", "Morpheus", "Themis", "QA")
PROG = "python3 scripts/session-worktree-mgr.py"


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env_extra: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env_extra:
        merged_env.update(env_extra)
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        text=True,
        capture_output=capture,
        check=check,
        timeout=timeout,
    )


def require_cmd(name: str) -> None:
    if shutil.which(name) is None:
        fail(f"command not found: {name}")


def git(cwd: Path, *args: str, capture: bool = False, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(cwd), *args], capture=capture, check=check, timeout=timeout)


def repo_root(cwd: Path | None = None) -> Path:
    cwd = cwd or Path.cwd()
    try:
        out = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture=True)
    except subprocess.CalledProcessError:
        fail("not inside a git repository")
    return Path(out.stdout.strip()).resolve()


def parse_agents(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_AGENTS)
    agents = [x.strip() for x in value.split(",") if x.strip()]
    if not agents:
        fail("empty --agents")
    for agent in agents:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", agent):
            fail(f"invalid agent name: {agent}")
    return agents


def normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


@dataclass(frozen=True)
class Config:
    repo: Path
    pool_dir: Path
    pool_size: int
    max_worktrees: int
    base_ref: str
    op_server: str
    sidecar: str
    op_host: str
    op_port: int
    sidecar_host: str
    sidecar_port: int
    log_dir: Path
    http_timeout: int
    pm_session_id: str = ""  # per-PM-session isolation of main agents

    @classmethod
    def load(cls) -> Config:
        root = repo_root()
        op_port = int(env("OP_PORT", "4097"))
        sidecar_port = int(env("SIDECAR_PORT", "4107"))
        # PM scope precedence: PM_SESSION_ID env > .pm/pm-session-info.json
        # > "" (legacy global main.state).  Env wins so callers and tests can
        # pin scope without rewriting the JSON.
        pm_sid = env("PM_SESSION_ID", "")
        if not pm_sid:
            try:
                info_path = root / ".pm" / "pm-session-info.json"
                if info_path.exists():
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                    pm_sid = str(info.get("current_session_id") or "")
            except Exception:
                pass
        return cls(
            repo=root,
            pool_dir=Path(env("WORKTREE_POOL_DIR", str(Path.home() / ".worktrees" / root.name))).expanduser().resolve(),
            pool_size=int(env("POOL_SIZE", "10")),
            max_worktrees=int(env("MAX_WORKTREES", "10")),
            base_ref=env("WORKTREE_BASE_REF", "origin/main"),
            op_server=env("OP_SERVER", f"http://127.0.0.1:{op_port}").rstrip("/"),
            sidecar=env("SIDECAR", f"http://127.0.0.1:{sidecar_port}").rstrip("/"),
            op_host=env("OP_HOST", "127.0.0.1"),
            op_port=op_port,
            sidecar_host=env("SIDECAR_HOST", "127.0.0.1"),
            sidecar_port=sidecar_port,
            log_dir=root / ".opencode" / "logs",
            http_timeout=int(env("OP_HTTP_TIMEOUT", "10")),
            pm_session_id=pm_sid,
        )


# ---------------- HTTP ----------------


def no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    expected: tuple[int, ...] = (200,),
    timeout: int | None = None,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    effective_timeout = timeout if timeout is not None else int(os.environ.get("OP_HTTP_TIMEOUT", "10"))
    try:
        with no_proxy_opener().open(req, timeout=effective_timeout) as res:
            payload = res.read()
            status = res.status
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        if exc.code not in expected:
            fail(f"{method} {url} failed: HTTP {exc.code}\n{payload}")
        return payload
    except TimeoutError:
        fail(f"{method} {url} timed out after {effective_timeout}s.\nFor directory=worktree, run pool repair/init to prewarm the worktree.")
    except urllib.error.URLError as exc:
        fail(f"{method} {url} failed: {exc}")
    except Exception as exc:
        fail(f"{method} {url} failed: {exc.__class__.__name__}: {exc}")
    if status not in expected:
        text = payload.decode("utf-8", errors="replace")
        fail(f"{method} {url} failed: HTTP {status}\n{text}")
    if not payload:
        return None
    text = payload.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def http_code(url: str, timeout: int = 2) -> int | None:
    try:
        with no_proxy_opener().open(url, timeout=timeout) as res:
            return int(res.status)
    except Exception:
        return None


# ---------------- state / git ----------------


# Stale-lock thresholds for pool_lock(). Without these, a SIGKILL during a
# pool op (or an OS-level crash) leaves ``.grab.lock`` behind forever and
# every subsequent ``pool {init, repair, prepare, release}`` fails with
# "another pool operation is running". The two-tier check — PID dead OR
# mtime > 1h — is a conservative auto-recovery: the PID test is the
# strongest signal, mtime is a fallback for hosts where ``os.kill`` is
# permission-restricted.
_POOL_LOCK_STALE_SECONDS = 3600


def _pool_lock_break_stale(lock_dir: Path) -> bool:
    """Return True if a stale ``.grab.lock`` was broken, False otherwise.

    Stale = owner PID is dead AND no live mtime signal, OR mtime is older
    than ``_POOL_LOCK_STALE_SECONDS``. Best-effort: returns False rather
    than raising so the caller's normal ``fail("another pool op running")``
    can fire on a live lock.
    """
    pid_path = lock_dir / "pid"
    try:
        mtime = lock_dir.stat().st_mtime
    except OSError:
        return False
    stale = False
    pid_text = ""
    if pid_path.exists():
        try:
            pid_text = pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            pid_text = ""
    if pid_text.isdigit():
        pid = int(pid_text)
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            stale = True
        except PermissionError:
            pass
    if (time.time() - mtime) > _POOL_LOCK_STALE_SECONDS:
        stale = True
    if not stale:
        return False
    eprint(f"warning: breaking stale pool lock (mtime > {_POOL_LOCK_STALE_SECONDS}s, pid={pid_text or '?'}); another pool op may have crashed")
    # Remove the lock dir and its pid file together. rmdir() needs the
    # target to be empty, so unlink the pid first, then rmdir. If rmdir
    # fails here we don't suppress — the caller will see the original
    # FileExistsError and can investigate.
    with contextlib.suppress(FileNotFoundError):
        pid_path.unlink()
    try:
        lock_dir.rmdir()
    except OSError as exc:
        eprint(f"warning: failed to rmdir stale lock {lock_dir}: {exc}")
        return False
    return True


@contextlib.contextmanager
def pool_lock(config: Config) -> Iterator[None]:
    config.pool_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = config.pool_dir / ".grab.lock"
    try:
        lock_dir.mkdir()
    except FileExistsError:
        # Try to break a stale lock first (crashed previous op). If the
        # lock is genuinely live, retry mkdir and fail loudly — never
        # silently skip a live holder.
        if _pool_lock_break_stale(lock_dir):
            try:
                lock_dir.mkdir()
            except FileExistsError:
                fail(f"another pool operation is running: {lock_dir}")
        else:
            fail(f"another pool operation is running: {lock_dir}")
    # Record the holder PID inside the lock dir. On a clean release we
    # unlink the pid then rmdir the dir; on a SIGKILL the pid file plus
    # lock dir are both left behind, which ``_pool_lock_break_stale``
    # will detect via dead-PID OR mtime on the next acquire.
    try:
        (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        eprint(f"warning: failed to record lock holder pid: {exc}")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            (lock_dir / "pid").unlink()
        try:
            lock_dir.rmdir()
        except OSError as exc:
            # Don't swallow: a future acquire needs to know the prior
            # holder exited uncleanly so it can break the stale lock.
            eprint(f"warning: failed to release pool lock {lock_dir}: {exc}")


def state_dir(config: Config) -> Path:
    return config.pool_dir / ".state"


def state_file(config: Config, wt_id: str) -> Path:
    return state_dir(config) / f"{wt_id}.state"


def read_state(config: Config, wt_id: str) -> dict[str, str]:
    return _read_state_file(state_file(config, wt_id))


def _read_state_file(path: Path) -> dict[str, str]:
    """Read a state file in the standard ``key=value`` format.

    Used by ``read_state`` (which routes by wt_id) and by callers that need
    to read a state file at a specific Path (e.g. a per-PM main.state that is
    NOT the current PM's main.state).
    """
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def _write_state_file(path: Path, values: dict[str, str]) -> None:
    """Write a standard ``key=value`` state file at an explicit path.

    Atomic via temp-file + ``os.replace()`` + ``fcntl.flock`` advisory lock
    (per Codex PR #32 P2 fix). The lock serializes the read-modify-write
    critical section against concurrent ``update_state()`` calls from a
    parallel dispatch, preventing both:

      - file-level race: two writers contending for the same ``.tmp`` path can
        get ``FileNotFoundError`` if one unlinks mid-write
      - logical lost-update: two callers both read the same initial state,
        merge their own patches, and the second ``os.replace`` clobbers the
        first writer's patch

    ``os.replace`` is atomic on POSIX and Windows when the source and
    destination are on the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{k}={v}\n" for k, v in values.items())
    tmp_path = path.with_name(f".{path.name}.tmp")
    lock_path = path.with_name(f".{path.name}.lock")

    # Acquire exclusive advisory lock on a sibling lock file. flock() is
    # automatically released when the fd is closed (incl. on SIGKILL of the
    # holding process), so no orphan-cleanup is required.
    if fcntl is not None:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _do_atomic_state_write(tmp_path, path, payload)
        finally:
            os.close(lock_fd)
    else:
        # Windows / non-POSIX fallback: atomic write only (no cross-process
        # serialization). Acceptable because the dispatcher is POSIX-only.
        _do_atomic_state_write(tmp_path, path, payload)


def _do_atomic_state_write(tmp_path: Path, path: Path, payload: str) -> None:
    """Inner writer split out so the lock path can be reused by the fallback."""
    tmp_path.write_text(payload, encoding="utf-8")
    try:
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort cleanup of the orphan tmp file before re-raising.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _update_state_file(path: Path, patch: dict[str, str]) -> dict[str, str]:
    """Read-merge-write under flock, preventing lost-update race on state files.

    The earlier `_write_state_file` only serialized the write half of the
    read-modify-write, so two concurrent callers could both read the same
    initial state, both merge their own patches, and the second ``os.replace``
    would clobber the first writer's patch.  This helper takes flock over the
    full read → merge → write critical section.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    tmp_path = path.with_name(f".{path.name}.tmp")

    def _do() -> dict[str, str]:
        data = _read_state_file(path)
        data.update(patch)
        data["updated_at"] = now_utc()
        payload = "".join(f"{k}={v}\n" for k, v in data.items())
        tmp_path.write_text(payload, encoding="utf-8")
        try:
            os.replace(tmp_path, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        return data

    if fcntl is not None:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return _do()
        finally:
            os.close(lock_fd)
    return _do()


def write_state(config: Config, wt_id: str, values: dict[str, str]) -> None:
    _write_state_file(state_file(config, wt_id), values)


def update_state(config: Config, wt_id: str, patch: dict[str, str]) -> dict[str, str]:
    return _update_state_file(state_file(config, wt_id), patch)


def _tombstoned_sids(state: dict[str, str]) -> set[str]:
    """Parse ``state['deleted_session_ids']`` (comma-separated) into a set.

    Soft-deleted session IDs are recorded here by ``cmd_sessions_delete`` (when
    ``--hard`` is NOT passed) so that ``rewatch_all_sessions`` and
    ``ensure_session`` know to skip them. The on-disk state still keeps the
    ``*_session_id`` pointer intact (dispatch keeps working), but the
    tombstoned session id is excluded from sidecar re-watch and title-search
    re-attach.
    """
    raw = state.get("deleted_session_ids", "")
    return {x for x in raw.split(",") if x}


def _add_tombstone(config: Config, wt_id: str, sid: str) -> None:
    """Append ``sid`` to the state file's ``deleted_session_ids`` field.

    Works for both ``wt_N.state`` and ``xidi-minimal`` (which routes to the
    per-PM-scoped ``sessions/<pm_sid>/main.state`` via ``update_main_state``).
    Existing ``*_session_id`` fields are NOT touched (per spec: do not pop
    other fields — only mark the sid as soft-deleted).
    """
    if wt_id == "xidi-minimal":
        state = read_main_state(config)
        existing = _tombstoned_sids(state)
        existing.add(sid)
        update_main_state(
            config,
            {"deleted_session_ids": ",".join(sorted(existing))},
        )
        return
    state = read_state(config, wt_id)
    existing = _tombstoned_sids(state)
    existing.add(sid)
    update_state(
        config,
        wt_id,
        {"deleted_session_ids": ",".join(sorted(existing))},
    )


def _prune_tombstones(config: Config, wt_id: str, alive: set[str]) -> None:
    """Drop tombstoned sids that are no longer present in OpenCode.

    Called by ``cleanup_stale_sessions`` so the tombstone set does not grow
    unbounded as ``sessions delete`` accumulates over time. ``alive`` is the
    set of session ids that ``GET /session/{id}`` returned a payload for; any
    tombstoned sid not in ``alive`` is removed from the field.
    """
    if wt_id == "xidi-minimal":
        state = read_main_state(config)
        tombstoned = _tombstoned_sids(state)
        keep = tombstoned & alive
        if keep != tombstoned:
            update_main_state(
                config,
                {"deleted_session_ids": ",".join(sorted(keep)) if keep else ""},
            )
        return
    state = read_state(config, wt_id)
    tombstoned = _tombstoned_sids(state)
    keep = tombstoned & alive
    if keep != tombstoned:
        update_state(
            config,
            wt_id,
            {"deleted_session_ids": ",".join(sorted(keep)) if keep else ""},
        )


def wt_id_for_index(index: int) -> str:
    return f"wt_{index}"


def validate_wt_id(wt_id: str) -> None:
    if not re.fullmatch(r"wt_[0-9]+", wt_id):
        fail(f"invalid wt_id: {wt_id}, expected wt_N")


def path_for_wt(config: Config, wt_id: str) -> Path:
    validate_wt_id(wt_id)
    return config.pool_dir / wt_id


def resolve_wt_id_or_path(config: Config, value: str) -> tuple[str, Path]:
    if re.fullmatch(r"wt_[0-9]+", value):
        wt_id = value
        state = read_state(config, wt_id)
        path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).expanduser().resolve()
        return wt_id, path
    path = Path(value).expanduser().resolve()
    # Main worktree (repo root) accepted for sessions list/delete.
    if path == config.repo:
        return "xidi-minimal", path
    wt_id = path.name
    validate_wt_id(wt_id)
    return wt_id, path


def resolve_pool_wt_target(config: Config, value: str) -> tuple[str, Path]:
    """Resolve a pool op target, rejecting the main repo.

    ``resolve_wt_id_or_path`` accepts ``config.repo`` and returns
    ``("xidi-minimal", repo)`` so that ``sessions list/delete`` can target
    the main worktree.  Pool ops (prepare / release / dispatch) must NEVER
    touch the main repo: doing so would reset / dirty-check / branch-checkout
    the user's working tree.  This wrapper enforces that boundary.
    """
    wt_id, wt_path = resolve_wt_id_or_path(config, value)
    if wt_id == "xidi-minimal" or wt_path == config.repo:
        fail(f"refusing to run pool op on main repo ({wt_path}). Pool ops target wt_N worktrees only — use `sessions list --main` or `sessions delete --main` for main-repo session management.")
    return wt_id, wt_path


def is_git_worktree(path: Path) -> bool:
    return path.is_dir() and git(path, "rev-parse", "--is-inside-work-tree", capture=True, check=False).returncode == 0


def worktree_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel", capture=True).stdout.strip()).resolve()


def assert_worktree_root(path: Path) -> None:
    if not is_git_worktree(path):
        fail(f"not a git worktree: {path}")
    root = worktree_root(path)
    if root != path:
        fail(f"worktree path must be root: got={path} root={root}")


def is_clean_worktree(path: Path) -> bool:
    return git(path, "status", "--porcelain", capture=True).stdout.strip() == ""


def branch_exists(repo: Path, branch: str) -> bool:
    return (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            capture=True,
            check=False,
        ).returncode
        == 0
    )


def _branch_tip(repo: Path, ref: str) -> str:
    """Return the commit SHA at ``ref`` (empty string on lookup failure)."""
    res = git(repo, "rev-parse", "--verify", ref, capture=True, check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def branch_tip_equals_base(repo: Path, branch: str, base_ref: str) -> bool:
    """True when ``refs/heads/<branch>`` and ``<base_ref>`` resolve to the
    same commit SHA. Used by ``checkout_task_branch`` to decide whether
    ``git reset --hard <base_ref>`` is a safe no-op (same commit) or a
    destructive operation that would discard commits the user made on the
    branch.
    """
    return bool(_branch_tip(repo, f"refs/heads/{branch}") and _branch_tip(repo, f"refs/heads/{branch}") == _branch_tip(repo, base_ref))


def remote_branch_exists(repo: Path, remote_branch: str) -> bool:
    return (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/{remote_branch}",
            capture=True,
            check=False,
        ).returncode
        == 0
    )


def validate_new_branch(repo: Path, branch: str, *, allow_existing: bool = False) -> None:
    if git(repo, "check-ref-format", "--branch", branch, capture=True, check=False).returncode != 0:
        fail(f"invalid branch name: {branch}")
    if not allow_existing and branch_exists(repo, branch):
        fail(f"local branch already exists: {branch}")
    if not allow_existing and remote_branch_exists(repo, f"origin/{branch}"):
        fail(f"remote branch already exists: origin/{branch}")


def ensure_base_ref(repo: Path, base_ref: str) -> None:
    if "/" in base_ref:
        remote, branch = base_ref.split("/", 1)
        eprint(f"fetch base ref: {remote} {branch}")
        git(repo, "fetch", remote, branch)
    if git(repo, "rev-parse", "--verify", base_ref, capture=True, check=False).returncode != 0:
        fail(f"base ref not found: {base_ref}")


def reset_to_base(path: Path, base_ref: str) -> None:
    git(path, "checkout", "--detach", base_ref)
    git(path, "reset", "--hard", base_ref)
    git(path, "clean", "-fd")


def checkout_task_branch(path: Path, repo: Path, branch: str, base_ref: str, *, allow_existing: bool) -> None:
    validate_new_branch(repo, branch, allow_existing=allow_existing)
    reset_to_base(path, base_ref)
    if allow_existing and branch_exists(repo, branch):
        if not branch_tip_equals_base(repo, branch, base_ref):
            # Refuse to ``git reset --hard`` when the branch has commits
            # the user made — that would silently destroy work. Force the
            # user to either rebase/merge into base, delete the branch, or
            # pick a new branch name.
            tip = _branch_tip(repo, f"refs/heads/{branch}")[:12]
            fail(
                f"branch {branch} already has commits not in {base_ref} "
                f"(tip {tip}). Refusing to reset --hard to avoid losing work. "
                f"Either rebase/merge {branch} onto {base_ref}, delete the "
                f"branch with `git branch -D {branch}`, or pick a new name."
            )
        git(path, "checkout", branch)
        # Branch tip == base_ref: reset --hard is a no-op, kept for clarity
        # so the worktree index matches the branch tip exactly.
        git(path, "reset", "--hard", base_ref)
    else:
        git(path, "checkout", "-b", branch, "--no-track", base_ref)


# ---------------- OpenCode helpers ----------------


def opencode_config(config: Config) -> dict[str, Any]:
    path = config.repo / ".opencode" / "opencode.json"
    if not path.exists():
        fail(f"{path} not found")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def parse_model_override(override: str) -> tuple[str, str, str | None]:
    """Parse the manager's ``providerID/modelID[:variant]`` notation."""
    # The variant is only recognized when the lone ":" sits to the right of
    # the "/" (i.e. it qualifies the model id, not the provider id).
    model_str = override
    variant: str | None = None
    if ":" in model_str:
        head, _, tail = model_str.partition(":")
        if "/" in head and ":" not in tail:
            model_str, variant = head, tail
    if "/" not in model_str:
        fail(f"--model override must be 'providerID/modelID[:variant]', got: {override!r}")
    provider_id, model_id = model_str.split("/", 1)
    if not provider_id or not model_id:
        fail(f"--model override must be 'providerID/modelID[:variant]', got: {override!r}")
    return provider_id, model_id, variant


def model_label(provider_id: str, model_id: str, variant: str | None = None) -> str:
    return f"{provider_id}/{model_id}" + (f":{variant}" if variant else "")


def is_session_pointer_key(key: str) -> bool:
    """Return whether a state key owns a session, excluding model metadata."""
    return key.endswith("_session_id") and not key.endswith("_model_session_id")


def opencode_model(config: Config, agent: str, *, override: str | None = None) -> tuple[str, str, str | None]:
    if override:
        # CLI override wins over agent config.
        return parse_model_override(override)
    data = opencode_config(config)
    agent_config = data.get("agent", {}).get(agent, {})
    variant = None
    if isinstance(agent_config, str):
        model_str = agent_config
    elif isinstance(agent_config, dict):
        model_str = agent_config.get("model") or data.get("small_model") or "opencode/deepseek-v4-flash-free"
        variant = agent_config.get("variant")
    else:
        model_str = data.get("small_model") or "opencode/deepseek-v4-flash-free"
    if "/" in model_str:
        provider_id, model_id = model_str.split("/", 1)
    else:
        provider_id, model_id = "opencode", model_str
    return provider_id, model_id, variant


def session_create_model(provider_id: str, model_id: str, variant: str | None) -> dict[str, str]:
    # POST /session uses the persisted Session model schema: ``id`` is the
    # model id.  prompt_async uses a different request schema and therefore
    # needs ``modelID`` (see prompt_model below).  Do not share these dicts.
    result = {"id": model_id, "providerID": provider_id}
    if variant:
        result["variant"] = variant
    return result


def prompt_model(provider_id: str, model_id: str) -> dict[str, str]:
    return {"providerID": provider_id, "modelID": model_id}


def session_bound_model(session: dict[str, Any] | None) -> tuple[str, str, str | None] | None:
    """Read a model from an OpenCode session response.

    OpenCode session responses store the model id as ``model.id`` while some
    API-compatible servers expose ``model.modelID``.  Accept both response
    shapes, but always return the manager's canonical tuple so dispatch can
    build the prompt request correctly.
    """
    if not isinstance(session, dict):
        return None
    raw = session.get("model")
    if not isinstance(raw, dict):
        return None
    provider_id = raw.get("providerID")
    model_id = raw.get("id") or raw.get("modelID")
    if not isinstance(provider_id, str) or not isinstance(model_id, str) or not provider_id or not model_id:
        return None
    variant = raw.get("variant")
    if not isinstance(variant, str) or not variant or variant == "default":
        variant = None
    return provider_id, model_id, variant


def stored_model_override(state: dict[str, str] | None, agent: str, session_id: str) -> tuple[str, str, str | None] | None:
    """Return a preselected model only while it belongs to this session.

    Older OpenCode servers accept the extra ``model`` field on create but do
    not persist it.  ``sessions create --model`` therefore pins the requested
    model in manager state until the first prompt binds it in OpenCode.
    """
    if not state or state.get(f"{agent}_model_session_id") != session_id:
        return None
    raw = state.get(f"{agent}_model_override", "")
    if not raw:
        return None
    try:
        return parse_model_override(raw)
    except SystemExit:
        # A manually edited state file must not make an otherwise valid
        # session undispatchable.  The normal agent config remains a fallback.
        eprint(f"warning: ignoring invalid stored model override for {agent} {session_id}: {raw!r}")
        return None


def dispatch_model(
    config: Config,
    agent: str,
    session: dict[str, Any] | None,
    *,
    override: str | None = None,
    state: dict[str, str] | None = None,
) -> tuple[str, str, str | None]:
    """Choose the model for a prompt.

    Explicit ``--model`` wins.  A manager-side preselection made by
    ``sessions create --model`` remains authoritative until the first prompt
    for compatibility with servers that ignore the model on ``POST /session``.
    Otherwise a model already bound to the session wins, and only then do we
    consult the agent definition; this prevents an agent config change from
    silently replacing a session's model.
    """
    if override:
        return opencode_model(config, agent, override=override)
    bound = session_bound_model(session)
    preselected = stored_model_override(state, agent, str((session or {}).get("id") or ""))
    if preselected:
        # A preselection represents an explicit choice made at session create.
        # It wins over a mismatching server response only until the first
        # dispatch clears it; matching responses are equivalent.
        return preselected
    if bound:
        return bound
    return opencode_model(config, agent)


def sessions(config: Config, *, directory: Path | None = None, search: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    query: dict[str, str | int] = {"limit": limit}
    if directory is not None:
        query["directory"] = str(directory)
    if search:
        query["search"] = search
    url = f"{config.op_server}/session?{urllib.parse.urlencode(query)}"
    data = http_json("GET", url)
    if not isinstance(data, list):
        fail("unexpected /session response")
    return cast(list[dict[str, Any]], data)


def get_session_by_id(config: Config, session_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """Fetch a single session by ID (direct GET, not list scan)."""
    try:
        data = http_json("GET", f"{config.op_server}/session/{urllib.parse.quote(session_id)}")
    except SystemExit:
        return None
    if not isinstance(data, dict):
        return None
    if directory is not None:
        sdir = data.get("directory")
        if not sdir or normalize_path(str(sdir)) != normalize_path(directory):
            return None
    return data


def find_session_by_title(config: Config, title: str, directory: Path) -> dict[str, Any] | None:
    found = [
        item
        for item in sessions(config, directory=directory, search=title, limit=50)
        if item.get("title") == title and item.get("directory") and normalize_path(str(item["directory"])) == normalize_path(directory)
    ]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        found.sort(key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)
        latest = found[0]
        eprint(f"multiple sessions for {title}: using latest {latest.get('id')} (total {len(found)})")
        return latest
    return None


def watch_session(config: Config, session_id: str) -> None:
    http_json("POST", f"{config.sidecar}/watch", {"sessionID": session_id})


def unwatch_session(config: Config, session_id: str) -> None:
    try:
        http_json(
            "DELETE",
            f"{config.sidecar}/watch/{urllib.parse.quote(session_id)}",
            expected=(200, 404),
        )
    except SystemExit:
        pass


def _kill_idle_watch(config: Config, session_id: str) -> None:
    """Kill the idle-watch process watching ``session_id``, if any.

    Reads the PID file written by ``_spawn_dispatch_idle_watch`` and sends
    SIGTERM.  Best-effort — failures are logged but not fatal.
    """
    pid_file_path = _idle_pidfile(config, session_id)
    if not pid_file_path.exists():
        return
    try:
        pid = int(pid_file_path.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        pid_file_path.unlink(missing_ok=True)
    except (ValueError, ProcessLookupError, FileNotFoundError):
        pid_file_path.unlink(missing_ok=True)
    except Exception:
        pass


def delete_session(config: Config, session_id: str, *, hard: bool = False) -> None:
    """Unwatch session via sidecar. If ``hard``, also DELETE from OpenCode server.

    Default (hard=False) is safe: session remains in OpenCode for later
    inspection / recovery. Internal cleanup (release / ensure_session) always
    uses hard=False. Only user-facing ``sessions delete --hard --yes`` triggers
    permanent deletion.

    Also kills any idle-watch process watching this session to prevent orphan
    watchers.
    """
    _kill_idle_watch(config, session_id)
    unwatch_session(config, session_id)
    if hard:
        try:
            http_json(
                "DELETE",
                f"{config.op_server}/session/{urllib.parse.quote(session_id)}",
                expected=(200, 204, 404),
            )
        except SystemExit:
            pass


STALE_SESSION_MS_DEFAULT = 24 * 60 * 60 * 1000  # 1 day
STALE_DISPATCH_MS = 10 * 60 * 1000  # 10 min — auto-recover stuck dispatch sessions
MAX_MAIN_SESSION_CONTEXT = 400_000  # auto-compact main agent session when context exceeds this (>1d → rebuild)

# Default recent window for `cmd_overview` and `rewatch_all_sessions`.
# Sessions whose `time.updated` is older than this are filtered out unless
# no recent session exists for that (wt_id, agent) pair, in which case the
# most-recent stale session is retained as a "last-known tombstone".
_OVERVIEW_RECENT_DEFAULT_SECONDS = 3 * 86400  # 3 days
_OVERVIEW_RECENT_DEFAULT_MS = _OVERVIEW_RECENT_DEFAULT_SECONDS * 1000

# Overview/sidecar should not scan an unbounded number of historical PM
# conversations from <pool_dir>/.state/sessions/<pm_session_id>/main.state.
# Keep the current PM session (if present) plus the newest historical PM states.
_PM_STATE_HISTORY_LIMIT_DEFAULT = 1


def is_session_stale(session: dict[str, Any], max_age_ms: int = STALE_SESSION_MS_DEFAULT) -> bool:
    """Return True if session's ``time.updated`` is older than ``max_age_ms`` (default 1 day)."""
    if not session:
        return False
    updated = int((session.get("time") or {}).get("updated") or 0)
    if not updated:
        return False
    return (int(time.time() * 1000) - updated) > max_age_ms


_KNOWN_AGENT_NAMES: dict[str, str] = {
    "pm": "PM",
    "momus": "Momus",
    "clio": "Clio",
    "daedalus": "Daedalus",
    "morpheus": "Morpheus",
    "themis": "Themis",
    "qa": "QA",
    "janitor": "Janitor",
    "websearch": "WebSearch",
    "explore": "Explore",
    "general": "General",
}
"""Canonical agent name mapping (lowercase → proper case)."""


def normalize_agent_label(agent: object) -> str:
    """Return a stable display/grouping label for agent names.

    OpenCode/plugin sources may report agent names in any case.
    All known agents are normalized to canonical proper case;
    unknown names are capitalized on first letter as fallback.
    """
    if not isinstance(agent, str) or not agent:
        return "__unknown__"
    key = agent.lower()
    if key in _KNOWN_AGENT_NAMES:
        return _KNOWN_AGENT_NAMES[key]
    return agent[0].upper() + agent[1:] if len(agent) > 1 else agent.upper()


def _is_pm_agent(agent: object) -> bool:
    return isinstance(agent, str) and agent.lower() == "pm"


def _pm_state_mtime_ms(state_file: Path) -> int:
    try:
        return int(state_file.stat().st_mtime_ns // 1_000_000)
    except OSError:
        return 0


def recent_pm_state_files(
    config: Config,
    *,
    limit: int = _PM_STATE_HISTORY_LIMIT_DEFAULT,
) -> list[tuple[str, Path, bool]]:
    """Return current + recent historical per-PM ``main.state`` files.

    ``limit`` applies only to historical PM sessions.  The active PM session
    (when it has a state file) is pinned and does not consume the historical
    budget.  This keeps overview/sidecar from resurrecting every old PM
    conversation while still showing the current PM plus the newest historical
    PM states from ``.state/sessions/<pm_session_id>/main.state``.
    """
    if limit < 0:
        fail("PM state history limit must be >= 0")
    sessions_root = state_dir(config) / "sessions"
    if not sessions_root.is_dir():
        return []

    current = config.pm_session_id
    rows: list[tuple[str, Path, bool, int]] = []
    for pm_dir in sorted(sessions_root.iterdir()):
        if not pm_dir.is_dir():
            continue
        state_file = pm_dir / "main.state"
        if not state_file.exists():
            continue
        pm_sid = pm_dir.name
        if not pm_sid.startswith("ses"):
            continue
        is_current = bool(pm_sid and pm_sid == current)
        rows.append((pm_sid, state_file, is_current, _pm_state_mtime_ms(state_file)))

    current_rows = [row for row in rows if row[2]]
    historical_rows = [row for row in rows if not row[2]]
    current_rows.sort(key=lambda item: (-item[3], item[0]))
    historical_rows.sort(key=lambda item: (-item[3], item[0]))
    selected = current_rows[:1] + historical_rows[:limit]
    return [(pm_sid, state_file, is_current) for pm_sid, state_file, is_current, _ in selected]


def tag_pm_session_ownership(config: Config, indexed: list[dict[str, Any]]) -> None:
    """Mutate main-worktree session index rows with PM owner metadata.

    Ownership comes from recent ``.state/sessions/<pm_sid>/main.state`` files.
    The PM session itself is also recognized by the state directory name, so
    historical PM conversations show as bounded PM groups instead of orphans.
    """
    pm_map = build_pm_session_map(config)
    owning_pm_sids = {pm_sid for pm_sid, _ in pm_map.values() if pm_sid}
    for it in indexed:
        raw = it.get("_raw")
        sid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
        pm_sid, is_current = pm_map.get(sid, ("", False))
        if not pm_sid and _is_pm_agent(it.get("agent")) and sid == config.pm_session_id:
            pm_sid = config.pm_session_id
            is_current = True
        elif not pm_sid and sid in owning_pm_sids:
            pm_sid = sid
            is_current = sid == config.pm_session_id
        it["pm_session_id"] = pm_sid
        it["pm_current"] = is_current


def cleanup_stale_sessions(
    config: Config,
    wt_id: str,
    max_age_ms: int = STALE_SESSION_MS_DEFAULT,
) -> list[tuple[str, str]]:
    """Archive (evict the state pointer, leave the session in OpenCode) any
    ``*_session_id`` field whose underlying session's ``time.updated`` exceeds
    ``max_age_ms``.

    The OpenCode session itself is NOT deleted — only its
    ``*_session_id``/``*_session_title`` entries in the state file are
    removed, so the next ``pool prepare``/``pool repair`` creates a fresh
    session with cold cache. The old session remains in OpenCode for
    history/inspection and is still visible via ``sessions list``.

    Used by ``release`` to keep the worktree's session pool fresh and
    bounded. ``pool prepare`` no longer recreates sessions — that moved
    to ``cmd_dispatch`` (auto-create on first use, recreate_existing=True),
    so release and dispatch are no longer paired by prepare in the middle.

    Returns the list of ``(agent, session_id)`` tuples whose state pointers
    were archived. Also prunes the ``deleted_session_ids`` tombstone field
    for any soft-deleted sid that no longer exists in OpenCode, so the
    tombstone set does not grow unbounded.
    """
    state = read_state(config, wt_id)
    # Collect every sid we need to look up: state-pinned + tombstoned
    # (the tombstone set may include sids from prior ``sessions delete``
    # invocations that the state file is still tracking).
    pinned_sids: dict[str, str] = {}  # sid -> "<agent>_session_id" key
    for key in list(state.keys()):
        if not is_session_pointer_key(key):
            continue
        agent = key[: -len("_session_id")]
        sid = state[key]
        if not sid:
            state.pop(key, None)
            state.pop(f"{agent}_session_title", None)
            state.pop(f"{agent}_model_override", None)
            state.pop(f"{agent}_model_session_id", None)
            continue
        pinned_sids[sid] = key
    tombstoned = _tombstoned_sids(state)
    all_sids_to_check = set(pinned_sids) | tombstoned

    # Batch lookup: one ``GET /session?limit=N`` covers every sid we care
    # about (state-pinned and tombstoned) instead of N round-trips. Falls
    # back to per-sid ``GET /session/{id}`` for any sid the bulk listing
    # did not return — e.g. sessions older than the listing window.
    sessions_by_sid: dict[str, dict[str, Any]] = {}
    if all_sids_to_check:
        try:
            all_listed = sessions(config, limit=_OVERVIEW_SESSION_LIMIT)
        except SystemExit as exc:
            eprint(f"warning: cleanup_stale_sessions bulk fetch failed: {exc}; falling back to per-sid lookups")
            all_listed = []
        for item in all_listed:
            sid = str(item.get("id") or "")
            if sid and sid in all_sids_to_check:
                sessions_by_sid[sid] = item
        missing = all_sids_to_check - set(sessions_by_sid)
        for sid in missing:
            try:
                item = http_json(
                    "GET",
                    f"{config.op_server}/session/{urllib.parse.quote(sid)}",
                )
            except SystemExit:
                continue
            if isinstance(item, dict):
                sessions_by_sid[sid] = item

    cleaned: list[tuple[str, str]] = []
    for sid, key in pinned_sids.items():
        agent = key[: -len("_session_id")]
        ses = sessions_by_sid.get(sid)
        if not ses:
            # State has a pinned sid but OpenCode doesn't list it (or the
            # bulk fetch failed for it) — leave the state pointer alone so
            # the next ``pool dispatch`` auto-create on a known-missing
            # sid can run the ensure_session path explicitly. Archive
            # is only safe when we have proof the session still exists.
            continue
        if is_session_stale(ses, max_age_ms):
            # Archive/unwatch the stale session in OpenCode + sidecar so
            # the dead pointer does not keep a watch slot warm.
            # hard=False preserves the OpenCode session record for
            # history/inspection. Failures are best-effort: if the
            # sidecar is down we still evict the state pointer to
            # prevent zombie dispatch.
            try:
                delete_session(config, sid, hard=False)
            except SystemExit:
                pass
            state.pop(key, None)
            state.pop(f"{agent}_session_title", None)
            state.pop(f"{agent}_model_override", None)
            state.pop(f"{agent}_model_session_id", None)
            cleaned.append((agent, sid))
    # Prune tombstoned sids that are no longer present in OpenCode, so the
    # tombstone field does not grow unbounded across many ``sessions delete``
    # invocations. Best-effort — failures are silently dropped (the next
    # cleanup cycle retries).
    pruned = False
    if tombstoned:
        alive = {sid for sid in tombstoned if sid in sessions_by_sid}
        keep = tombstoned & alive
        if keep != tombstoned:
            state["deleted_session_ids"] = ",".join(sorted(keep)) if keep else ""
            pruned = True
    if cleaned or pruned:
        write_state(config, wt_id, state)
    return cleaned


def create_session(config: Config, wt_id: str, wt_path: Path, agent: str, *, model_override: str | None = None) -> dict[str, Any]:
    title = f"{wt_id}-{agent}"
    provider_id, model_id, variant = opencode_model(config, agent, override=model_override)
    body = {
        "title": title,
        "agent": agent,
        "model": session_create_model(provider_id, model_id, variant),
        "metadata": {
            "managedBy": "session-worktree-mgr.py",
            "wt_id": wt_id,
            "wt_path": str(wt_path),
            "agent": agent,
        },
    }
    query = urllib.parse.urlencode({"directory": str(wt_path)})
    data = http_json("POST", f"{config.op_server}/session?{query}", body, expected=(200, 201))
    if not isinstance(data, dict) or not data.get("id"):
        fail(f"create session failed: response missing id for {title}")
    if model_override:
        requested = (provider_id, model_id, variant)
        bound = session_bound_model(cast(dict[str, Any], data))
        if bound != requested:
            # Some OpenCode releases silently strip ``model`` from the
            # create payload even though the prompt endpoint supports model
            # selection.  The caller persists the requested model alongside
            # this session so the first dispatch still pins it explicitly.
            actual = model_label(*bound) if bound else "(unset)"
            eprint(f"warning: OpenCode did not bind --model {model_label(*requested)} when creating {data['id']} (reported {actual}); first dispatch will pin it")
    return cast(dict[str, Any], data)


def ensure_session(
    config: Config,
    wt_id: str,
    wt_path: Path,
    agent: str,
    *,
    recreate_missing: bool = True,
    recreate_stale: bool = False,
    recreate_existing: bool = False,
    max_age_ms: int = STALE_SESSION_MS_DEFAULT,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Return a usable session for ``(wt_id, agent)``.

        Lookup order:
          1. state ``*_session_id`` + ``get_session_by_id`` (still in OpenCode).
             The fast path also honors ``state['deleted_session_ids']``: if the
             pinned sid was soft-deleted via ``sessions delete``, the state
             pointer is bypassed and we fall through to step 2 — the user
             explicitly tombstoned it and we must not resurrect it.
          2. fallback: ``find_session_by_title`` (e.g. state was lost), skipping
             any soft-deleted sids in ``state['deleted_session_ids']`` (P1-5 —
             the user explicitly tombstoned these via ``sessions delete``, so
             we must not resurrect them as the "current" session)
          3. create new session via ``create_session``

        With ``recreate_stale=True``, sessions whose ``time.updated`` exceeds
        ``max_age_ms`` are ARCHIVED (unwatched, left in OpenCode for later
        inspection — cache is cold, conversation history is no longer useful)
        and a fresh session is created instead. This is the "grab-time
        refresh" behavior — see ``cmd_pool_repair_stuck`` for the current
        caller.

    With ``recreate_existing=True``, ANY existing session (state-pinned or
        title-found) is archived first (unwatched, left in OpenCode) and a
        fresh session is created. This is the "always-fresh" behavior used by
        ``cmd_dispatch`` auto-create fallback (cold-start context on first
        use) and by ``pool repair_stuck`` to reset a stuck session. Per-dispatch
        loops within the same task still reuse the session — dispatch only
        calls ``ensure_session(recreate_existing=True)`` when the existing
        sid is missing or stale.
    """
    title = f"{wt_id}-{agent}"
    state = read_state(config, wt_id)
    tombstoned = _tombstoned_sids(state)
    state_sid = state.get(f"{agent}_session_id")
    if state_sid and state_sid in tombstoned:
        # Soft-deleted via ``sessions delete`` (no ``--hard``): the state
        # pointer still points to a tombstoned sid. Honor the tombstone
        # before consulting OpenCode so the fast path cannot resurrect a
        # session the user explicitly marked as deleted. Fall through to
        # ``find_session_by_title`` / ``create_session`` for a clean one.
        eprint(f"tombstoned state session skipped: {state_sid}")
    elif state_sid:
        item = get_session_by_id(config, state_sid, directory=wt_path)
        if item:
            if recreate_existing:
                delete_session(config, state_sid, hard=False)
                eprint(f"old state session archived: {state_sid}")
            elif recreate_stale and is_session_stale(item, max_age_ms):
                eprint(f"stale state session archived (> {max_age_ms}ms): {state_sid}")
                delete_session(config, state_sid)
            else:
                return item
        else:
            # State has a pinned sid but OpenCode no longer knows it (e.g.
            # user hard-deleted the session, or the worktree was moved).
            # The title-search fallback exists for "state was lost" (no
            # sid at all); here we DO have a sid, it just doesn't exist
            # in OpenCode anymore — title-search would either find a
            # different (possibly stale) session or waste an HTTP call.
            # Skip the fallback and create a fresh session directly.
            eprint(f"stale state session id archived: {agent} {state_sid}")
            if not recreate_missing:
                fail(f"missing session for {title}; run pool repair {wt_id}")
            return create_session(config, wt_id, wt_path, agent, model_override=model_override)
    item = find_session_by_title(config, title, wt_path)
    if item and str(item.get("id", "")) in tombstoned:
        # Soft-deleted via ``sessions delete`` (no ``--hard``): the title
        # match would resurrect a session the user explicitly tombstoned.
        # Fall through to create a fresh one so the next dispatch gets a
        # clean context window.
        eprint(f"tombstoned title match skipped: {item.get('id')}")
        item = None
    if item:
        if recreate_existing:
            delete_session(config, item["id"], hard=False)
            eprint(f"session archived by title: {item.get('id')}")
        elif recreate_stale and is_session_stale(item, max_age_ms):
            eprint(f"session archived by title (> {max_age_ms}ms): {item.get('id')}")
            delete_session(config, item["id"])
        else:
            return item
    if not recreate_missing:
        fail(f"missing session for {title}; run pool repair {wt_id}")
    return create_session(config, wt_id, wt_path, agent, model_override=model_override)


def persist_session(
    config: Config,
    wt_id: str,
    agent: str,
    session: dict[str, Any],
    *,
    model_override: str | None = None,
) -> None:
    sid = str(session.get("id") or "")
    if not sid:
        fail(f"session missing id for {wt_id}-{agent}")
    patch = {
        f"{agent}_session_id": sid,
        f"{agent}_session_title": str(session.get("title") or f"{wt_id}-{agent}"),
        f"{agent}_model_override": "",
        f"{agent}_model_session_id": "",
    }
    if model_override:
        provider_id, model_id, variant = opencode_model(config, agent, override=model_override)
        patch[f"{agent}_model_override"] = model_label(provider_id, model_id, variant)
        patch[f"{agent}_model_session_id"] = sid
    update_state(
        config,
        wt_id,
        patch,
    )
    watch_session(config, sid)


def clear_stored_model_override(config: Config, wt_id: str, agent: str, session_id: str) -> None:
    """Drop a create-time model pin after its first prompt is accepted."""
    if wt_id == "main":
        state = read_main_state(config)
        if state.get(f"{agent}_model_session_id") != session_id:
            return
        update_main_state(
            config,
            {
                f"{agent}_model_override": "",
                f"{agent}_model_session_id": "",
            },
        )
        return
    state = read_state(config, wt_id)
    if state.get(f"{agent}_model_session_id") != session_id:
        return
    update_state(
        config,
        wt_id,
        {
            f"{agent}_model_override": "",
            f"{agent}_model_session_id": "",
        },
    )


# ---------------- node_modules copy ----------------


def copy_opencode_node_modules(config: Config, wt_path: Path, *, force: bool = False) -> None:
    source = config.repo / ".opencode" / "node_modules"
    target_dir = wt_path / ".opencode"
    target = target_dir / "node_modules"
    if not source.is_dir():
        fail(f"main .opencode/node_modules not found: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if not force:
            fail(f"worktree .opencode/node_modules is symlink; rerun with --force-copy to replace: {target}")
        target.unlink()
    if target.exists():
        if not force:
            eprint(f".opencode/node_modules already exists, keep: {target}")
            return
        shutil.rmtree(target)
    eprint(f"copy .opencode/node_modules: {source} -> {target}")
    shutil.copytree(source, target, symlinks=True)
    eprint(f"copied .opencode/node_modules: {target}")


# ---------------- service ----------------


def op_healthy(config: Config) -> bool:
    return http_code(f"{config.op_server}/global/health") == 200 or http_code(f"{config.op_server}/session") == 200


def sidecar_healthy(config: Config) -> bool:
    try:
        data = http_json("GET", f"{config.sidecar}/health")
        return isinstance(data, dict) and data.get("opencodeServer") == config.op_server
    except SystemExit:
        return False


def check_services(config: Config) -> None:
    if not op_healthy(config):
        fail(f"OpenCode server not healthy: {config.op_server}")
    if not sidecar_healthy(config):
        fail(f"sidecar not healthy: {config.sidecar}")


def pid_file(config: Config, name: str) -> Path:
    return config.log_dir / f"{name}.pid"


def log_file(config: Config, name: str) -> Path:
    return config.log_dir / f"{name}.log"


def _write_pid_file(path: Path, pid: int) -> None:
    """Atomically write a PID to ``path`` via tmp + os.replace.

    Without this, a crashed mid-write (e.g. SIGKILL during ``write_text``)
    can leave a partial numeric string that ``int(...)`` happily parses as
    a different — possibly live — PID, which would then receive
    ``SIGTERM``/``SIGKILL`` from ``cmd_idle_watch_stop``. ``os.replace`` is
    atomic on POSIX for files in the same directory, so callers either see
    the previous valid value or the new valid value, never a partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(str(pid), encoding="utf-8")
    try:
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort cleanup of the orphan tmp file before re-raising so
        # the caller (and the next acquire) doesn't see a stale .tmp.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Reject partial writes (e.g. "12" from a crashed write_text) and any
    # non-pure-digit content. Without this, a truncated PID file would
    # parse as the wrong number and ``pid_alive`` would happily return
    # True for an unrelated live process.
    if not raw or not raw.isdigit():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def pid_alive(pid: int | None) -> TypeGuard[int]:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _get_session_status(config: Config, session_id: str) -> str:
    """Return the sidecar status for ``session_id`` (``idle``/``busy``/``streaming``/``unknown``).

    Single read with no side effects.  ``unknown`` covers both HTTP failure
    and "not in watch table"; callers treat it as "cannot prove busy" and
    fall through to the safe path (re-use existing session).  This is
    deliberately distinct from the idle-watch auto-register-and-refetch
    variant — auto-watching on create would silently pin an untracked
    session into the sidecar's table.
    """
    if not session_id:
        return "unknown"
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        return "unknown"
    if not isinstance(status_map, dict):
        return "unknown"
    raw = status_map.get(session_id, "unknown")
    return str(raw) if isinstance(raw, str) else "unknown"


def _check_no_active_sessions_on_wt(config: Config, wt_id: str) -> None:
    """Refuse reset if any session on this worktree is busy/streaming.

    Prevents accidentally destroying uncommitted agent work via
    ``pool repair --reset`` while a session is actively running.
    Query strings ``{agent}_session_id`` from the worktree state file
    and check sidecar /status for each.
    """
    state = read_state(config, wt_id)
    active: list[str] = []
    for key, sid in state.items():
        if not is_session_pointer_key(key) or not sid:
            continue
        status = _get_session_status(config, sid)
        if status in ("busy", "streaming"):
            agent = key.replace("_session_id", "")
            active.append(f"{agent}({status})")
    if active:
        fail(f"refusing --reset on {wt_id}: {len(active)} active session(s) ({', '.join(active)})\n  Use 'overview --wt {wt_id}' to inspect, then retry when sessions are idle.")


def wait_until(fn: Callable[[], bool], timeout: int = 20) -> bool:
    for _ in range(timeout):
        if fn():
            return True
        time.sleep(1)
    return False


def rewatch_all_sessions(config: Config) -> None:
    """Re-register persisted session IDs with the sidecar after a sidecar restart.

    Scans every ``wt_*.state`` file in the pool directory, extracts each
    ``*_session_id`` field, fetches the session metadata from OpenCode to learn
    its ``agent`` + ``updated_ms``, and applies the same recent-window filter
    used by ``cmd_overview`` (see ``_apply_recent_filter``).  Sessions that
    no longer exist in OpenCode, or whose ``GET /session/{id}`` call fails,
    are skipped silently — they will be reconciled by the next ``pool prepare``
    or ``pool repair`` cycle.

    Sharing the filter with ``collect_overview`` keeps the sidecar's watch
    table aligned with what overview displays: a session the user explicitly
    removed via ``sessions delete`` (and therefore intentionally aged out of
    the recent window) stays unwatched even after a restart.
    """
    sd = state_dir(config)
    if not sd.is_dir():
        return
    candidates: list[dict[str, Any]] = []

    def _ingest_sid(sid: str, wt_id: str, pm_session_id: str = "") -> None:
        """Fetch session metadata and append to candidates.

        ``pm_session_id`` tags the candidate with its owning PM session so
        per-PM isolation flows through ``_apply_recent_filter``. Defaults to
        the empty string (back-compat for callers that don't track PM).
        """
        try:
            ses = http_json(
                "GET",
                f"{config.op_server}/session/{urllib.parse.quote(sid)}",
            )
        except SystemExit:
            # Network blip / sidecar down / 5xx — skip this session; we
            # don't want a transient failure to abort the whole restart
            # recovery loop.
            return
        if not isinstance(ses, dict):
            return
        meta = ses.get("metadata") or {}
        agent = meta.get("agent") or ses.get("agent") or "__unknown__"
        updated_ms = int((ses.get("time") or {}).get("updated") or 0)
        candidates.append(
            {
                "_sid": sid,
                "wt_id": wt_id,
                "pm_session_id": pm_session_id,
                "agent": normalize_agent_label(agent),
                "updated_ms": updated_ms,
            }
        )

    # Scan worktree pool state files (per-PM tagging does not apply; PM
    # sessions are scoped to main worktree via sessions/*/main.state below).
    for sf in sorted(sd.glob("wt_*.state")):
        wt_id = sf.stem
        if not re.fullmatch(r"wt_[0-9]+", wt_id):
            continue
        state = read_state(config, wt_id)
        tombstoned = _tombstoned_sids(state)
        for key, sid in state.items():
            if not is_session_pointer_key(key) or not isinstance(sid, str) or not sid:
                continue
            if sid in tombstoned:
                # Soft-deleted via ``sessions delete`` (no ``--hard``): skip
                # the sidecar re-watch so the user stays unwatched until a
                # future ``pool repair`` creates a fresh session.
                continue
            _ingest_sid(sid, wt_id)

    # Scan recent per-PM-session main.state files for main agents.  This is
    # intentionally capped (current PM + newest historical PM state by default)
    # so sidecar restart recovery does not re-watch every old PM conversation.
    # The PM session itself is also ingested from the state directory name so
    # sidecar can report current/old PM sessions instead of only their agents.
    for pm_sid, state_file, _is_current in recent_pm_state_files(config):
        main_state = _read_state_file(state_file)
        main_tombstoned = _tombstoned_sids(main_state)
        if pm_sid and pm_sid not in main_tombstoned:
            _ingest_sid(pm_sid, "xidi-minimal", pm_session_id=pm_sid)
        for key, sid in main_state.items():
            if is_session_pointer_key(key) and sid and sid not in main_tombstoned:
                _ingest_sid(sid, "xidi-minimal", pm_session_id=pm_sid)
    filtered = _apply_recent_filter(
        candidates,
        now_ms=int(time.time() * 1000),
        recent_seconds=_OVERVIEW_RECENT_DEFAULT_SECONDS,
        pm_session_id="pm_session_id",
    )
    rewired = 0
    for fs in filtered:
        try:
            watch_session(config, fs["_sid"])
            rewired += 1
        except SystemExit:
            pass
    if rewired:
        eprint(f"rewired {rewired} session watch(es) after sidecar (re)start")


def cmd_opencode_serve_service(args: argparse.Namespace, config: Config) -> None:
    """管理 OpenCode Server 进程（opencode serve）。

    独立管理，不依赖 Sidecar。start/stop/status/restart 仅作用于 OpenCode Server。
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    op_pid_file = pid_file(config, "opencode-server")
    if args.action == "status":
        print(
            json.dumps(
                {
                    "service": "opencode-server",
                    "healthy": op_healthy(config),
                    "pid": read_pid(op_pid_file),
                    "pidFile": str(op_pid_file),
                    "logFile": str(log_file(config, "opencode-server")),
                    "url": config.op_server,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.action in ("stop", "restart"):
        pid = read_pid(op_pid_file)
        if pid_alive(pid):
            eprint(f"opencode-server: stop pid={pid}")
            os.kill(pid, signal.SIGTERM)
            for _ in range(5):
                if not pid_alive(pid):
                    break
                time.sleep(1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        op_pid_file.unlink(missing_ok=True)
        if args.action == "stop":
            return
    if args.action in ("start", "restart"):
        require_cmd("opencode")
        if not op_healthy(config):
            op_log = open(log_file(config, "opencode-server"), "ab")
            proc = subprocess.Popen(
                ["opencode", "serve", "--hostname", config.op_host, "--port", str(config.op_port)],
                cwd=str(config.repo),
                stdout=op_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _write_pid_file(op_pid_file, proc.pid)
            if not wait_until(lambda: op_healthy(config)):
                fail(f"OpenCode Server did not become healthy; log={log_file(config, 'opencode-server')}")
            eprint(f"OpenCode Server: started pid={proc.pid}")
        else:
            eprint("OpenCode Server: already healthy")


def cmd_sidecar_service(args: argparse.Namespace, config: Config) -> None:
    """管理 Session Status Sidecar 进程（node scripts/session-status-server.mjs）。

    独立管理，不依赖 OpenCode Server。start/restart 后自动恢复所有持久化 session watch。
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    sidecar_pid_file = pid_file(config, "session-status-server")
    if args.action == "status":
        print(
            json.dumps(
                {
                    "service": "session-status-server",
                    "healthy": sidecar_healthy(config),
                    "pid": read_pid(sidecar_pid_file),
                    "pidFile": str(sidecar_pid_file),
                    "logFile": str(log_file(config, "session-status-server")),
                    "url": config.sidecar,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.action in ("stop", "restart"):
        pid = read_pid(sidecar_pid_file)
        if pid_alive(pid):
            eprint(f"session-status-server: stop pid={pid}")
            os.kill(pid, signal.SIGTERM)
            for _ in range(5):
                if not pid_alive(pid):
                    break
                time.sleep(1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        sidecar_pid_file.unlink(missing_ok=True)
        if args.action == "stop":
            return
    if args.action in ("start", "restart"):
        require_cmd("node")
        sidecar_script = config.repo / "scripts" / "session-status-server.mjs"
        if not sidecar_script.exists():
            fail(f"sidecar script not found: {sidecar_script}")
        if not sidecar_healthy(config):
            sidecar_log = open(log_file(config, "session-status-server"), "ab")
            proc = subprocess.Popen(
                ["node", str(sidecar_script)],
                cwd=str(config.repo),
                env={
                    **os.environ,
                    "OPENCODE_SERVER": config.op_server,
                    "SESSION_STATUS_HOST": config.sidecar_host,
                    "SESSION_STATUS_PORT": str(config.sidecar_port),
                },
                stdout=sidecar_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _write_pid_file(sidecar_pid_file, proc.pid)
            if not wait_until(lambda: sidecar_healthy(config)):
                fail(f"Status Sidecar did not become healthy; log={log_file(config, 'session-status-server')}")
            eprint(f"Status Sidecar: started pid={proc.pid}")
        else:
            eprint("Status Sidecar: already healthy")
        # Sidecar 重启后 watch 表为空，恢复所有持久化的 session watch
        rewatch_all_sessions(config)


# ---------------- pool ----------------


def create_or_reuse_worktree(config: Config, wt_id: str) -> Path:
    wt_path = path_for_wt(config, wt_id)
    config.pool_dir.mkdir(parents=True, exist_ok=True)
    ensure_base_ref(config.repo, config.base_ref)
    if not wt_path.exists():
        eprint(f"create worktree: {wt_id} {wt_path}")
        git(config.repo, "worktree", "add", "--detach", str(wt_path), config.base_ref)
    else:
        eprint(f"reuse worktree: {wt_id} {wt_path}")
    assert_worktree_root(wt_path)
    return wt_path


def repair_one(
    config: Config,
    wt_id: str,
    agents: list[str],
    *,
    reset: bool,
    force_copy: bool,
) -> dict[str, Any]:
    """Create or repair a single worktree (physical + state).

    Both current call sites (``pool init`` and ``pool repair``, after
    commit ``16abebf``) only produce the wt directory, the
    ``.opencode/node_modules`` copy, and the state file
    (``initialized=1``). Sessions are created lazily by ``cmd_dispatch``
    auto-create on first use — the ``agents`` argument is retained for
    ``cmd_pool_init``'s default-agents check and the result-row
    compatibility, but is no longer iterated for session creation.

    Skipping session creation at init/repair time avoids leaving unused
    preallocated sessions on every freshly initialized wt (they showed up
    as ``unknown`` in sidecar ``/status`` and triggered the overview
    placeholder row for never-dispatched wts). Pool size grew but actual
    dispatch utilization was uneven across agents per wt, so a typical wt
    ended up with 2-3 sessions that never saw a single prompt.
    """
    del agents  # intentionally unused: sessions are created lazily on dispatch
    validate_wt_id(wt_id)
    wt_path = create_or_reuse_worktree(config, wt_id)
    if reset:
        _check_no_active_sessions_on_wt(config, wt_id)
        reset_to_base(wt_path, config.base_ref)
    copy_opencode_node_modules(config, wt_path, force=force_copy)
    state = read_state(config, wt_id)
    state.update(
        {
            "status": state.get("status") or "idle",
            "wt_id": wt_id,
            "wt_path": str(wt_path),
            "base_ref": config.base_ref,
            "initialized": "1",
            "node_modules_mode": "copy",
            "updated_at": now_utc(),
        }
    )
    write_state(config, wt_id, state)
    return {"wt_id": wt_id, "wt_path": str(wt_path), "sessions": []}


def cmd_pool_init(args: argparse.Namespace, config: Config) -> None:
    check_services(config)
    agents = parse_agents(args.agents)
    if args.size < 1:
        fail("--size must be >= 1")
    if args.size > config.max_worktrees:
        fail(f"--size exceeds MAX_WORKTREES={config.max_worktrees}")
    results = []
    with pool_lock(config):
        for i in range(1, args.size + 1):
            wt_id = wt_id_for_index(i)
            eprint(f"== pool init {wt_id} ==")
            # pool init no longer pre-creates agent sessions — they show up
            # as absent from sidecar /status until first dispatch, and a
            # freshly initialized wt no longer accumulates agents that never
            # see a prompt. Sessions are created lazily on first
            # ``dispatch`` (see cmd_dispatch auto-create fallback).
            results.append(
                repair_one(
                    config,
                    wt_id,
                    agents,
                    reset=args.reset,
                    force_copy=args.force_copy,
                )
            )
    print(json.dumps({"pool_dir": str(config.pool_dir), "results": results}, ensure_ascii=False, indent=2))


def cmd_pool_repair(args: argparse.Namespace, config: Config) -> None:
    """Repair a single wt — physical + state only, no session creation.

    Scope: ``validate_wt_id`` → ``create_or_reuse_worktree`` → optional
    ``reset_to_base`` → ``copy_opencode_node_modules`` → mark
    ``initialized=1``. Sessions are NOT created here — that responsibility
    moved entirely to ``cmd_dispatch`` auto-create (commit ``16abebf`` and
    later). ``pool repair`` is now a thin wt-state fixup; for any session
    work, the first dispatch on a fresh wt triggers creation on demand.

    Sessions have never been "repaired" by this command in any meaningful
    sense — before commit ``16abebf`` it pre-created sessions, but with the
    dispatch task_marker decision tree the only legitimate reason to want
    pre-creation was avoiding dispatch auto-create on first use. That
    benefit is small (one ensure_session call per agent, ~ms-scale on
    OpenCode) and not worth maintaining a separate code path that has to
    stay in sync with dispatch's marker semantics.
    """
    check_services(config)
    agents = parse_agents(args.agents)
    with pool_lock(config):
        result = repair_one(
            config,
            args.wt_id,
            agents,
            reset=args.reset,
            force_copy=args.force_copy,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def pool_row(config: Config, wt_id: str, agents: list[str], *, verify: bool) -> dict[str, Any]:
    state = read_state(config, wt_id)
    wt_path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
    row: dict[str, Any] = {
        "wt_id": wt_id,
        "status": state.get("status", "missing"),
        "wt_path": str(wt_path),
        "branch": state.get("branch", ""),
        "initialized": state.get("initialized", "0"),
        "node_modules_mode": state.get("node_modules_mode", ""),
        "sessions": {},
    }
    for agent in agents:
        sid = state.get(f"{agent}_session_id", "")
        exists = None
        if verify and sid:
            exists = get_session_by_id(config, sid, directory=wt_path) is not None
        row["sessions"][agent] = {"sessionID": sid, "exists": exists}
    return row


def cmd_pool_status(args: argparse.Namespace, config: Config) -> None:
    agents = parse_agents(args.agents)
    rows = [pool_row(config, wt_id_for_index(i), agents, verify=args.verify) for i in range(1, args.size + 1)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


# ---------------- prepare / dispatch / release ----------------

# Round-robin state for find_idle_wt(). Persisted to
# ``<pool_dir>/.state/.pool_rr_index`` so it survives across CLI invocations
# (each ``python3 ...`` is a new process). Missing or corrupt file resets
# to wt_1 (per PM convention 2026-06-08).
_RR_STATE_FILENAME = ".pool_rr_index"


def _read_rr_index(config: Config) -> int:
    path = state_dir(config) / _RR_STATE_FILENAME
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 1


def _write_rr_index(config: Config, idx: int) -> None:
    """Atomically persist the round-robin pointer; warn (not swallow) on failure.

    A silent failure here would cause every subsequent ``find_idle_wt`` to
    reset to ``wt_1`` even when other slots are idle, undoing the pool
    distribution without any user-visible signal. Use the same tmp +
    os.replace pattern as the state-file writer so concurrent readers
    never see a partial index.
    """
    path = state_dir(config) / _RR_STATE_FILENAME
    state_dir(config).mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(str(idx), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        # Best-effort cleanup of the orphan tmp file before warning.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        eprint(f"warning: failed to persist RR index ({exc}); next call resets to wt_1")


def find_idle_wt(config: Config) -> tuple[str, Path]:
    """Pick the next idle worktree via round-robin (persists across CLI calls).

    Scans wt_1..wt_<pool_size> starting from the persisted ``_RR_STATE_FILENAME``
    index. On a hit, advances the persisted pointer to the slot after the
    picked one (wraps to wt_1 after wt_<pool_size>). Falls through to the
    original "no idle initialized worktree" failure if every slot is busy.
    """
    pool_size = config.pool_size
    start = _read_rr_index(config)
    if start < 1 or start > pool_size:
        start = 1
    for offset in range(pool_size):
        i = ((start - 1 + offset) % pool_size) + 1
        wt_id = wt_id_for_index(i)
        state = read_state(config, wt_id)
        if state.get("initialized") == "1" and state.get("status", "idle") == "idle":
            _write_rr_index(config, 1 if i >= pool_size else i + 1)
            return wt_id, Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
    fail("no idle initialized worktree; run pool status or pool init")
    raise AssertionError("unreachable")


def cmd_prepare(args: argparse.Namespace, config: Config) -> None:
    """Reserve an idle wt for the task branch — does NOT touch sessions.

    Sessions are created lazily by ``cmd_dispatch`` (auto-create on first
    use, recreate_existing=True for cold-start context) or explicitly via
    ``pool repair``. ``pool prepare`` is now scoped to wt state only:
    pick idle wt, checkout branch, mark busy. Session pre-allocation was
    dropped so pool init / prepare no longer leaves unused sessions in
    the sidecar watch table.

    Each prepare generates a fresh ``task_marker`` (uuid4 hex) into the wt
    state. ``cmd_dispatch`` compares this against the agent's stored
    ``{agent}_task_marker``: a mismatch means a new task boundary was
    crossed (e.g. release + re-prepare), so the agent's session must be
    archived and a fresh one created. The uuid ensures even same-branch
    re-prepares trigger the new-task recreate.
    """
    check_services(config)
    agents = parse_agents(args.agents)
    with pool_lock(config):
        wt_id, wt_path = find_idle_wt(config)
        if not is_clean_worktree(wt_path):
            fail(f"selected worktree is dirty: {wt_path}; run release --force or pool repair")
        ensure_base_ref(config.repo, config.base_ref)
        checkout_task_branch(wt_path, config.repo, args.branch, config.base_ref, allow_existing=args.force_branch)
        update_state(
            config,
            wt_id,
            {
                "status": "busy",
                "branch": args.branch,
                "wt_path": str(wt_path),
                "task_marker": uuid.uuid4().hex,
            },
        )
    print(f"wt_id={wt_id}")
    print(f"wt_path={wt_path}")
    print(f"branch={args.branch}")
    print()
    print("下一步：先预览 prompt，不会发送")
    print(f'{PROG} pool dispatch {wt_id} {agents[0]} --task "..."')
    print()
    print("用户确认后再发送")
    print(f'{PROG} pool dispatch {wt_id} {agents[0]} --task "..." --yes')
    print()
    print("（可选：加 --notify-session <PM_CURRENT_SESSION_ID> 自动 idle-watch，或设 $PM_CURRENT_SESSION_ID）")
    print()
    print(f"已分配: {wt_id}")


def _hard_constraints() -> str:
    return """---
⚠️ HARD CONSTRAINTS:

- 严格按 workflow 完成任务 - 不跳、不省略、不提前结束
- 禁止发起 subagent 任务
- 禁止访问（读或写）工作目录外的文件
- 任何阻塞点或需要进一步决策的问题都应暂停任务并报告状态
- 最后一条消息按报告格式（如有）输出，没有规定报告格式则做总结性陈述
"""


def render_prompt(wt_dir: Path, task: str, *, config: Config) -> str:
    label = "main" if wt_dir == config.repo else wt_dir.name
    header = f"<!-- {label}: {wt_dir} -->"
    return f"""{header}

{task}

{_hard_constraints()}
"""


def cmd_dispatch(args: argparse.Namespace, config: Config) -> None:
    check_services(config)
    if args.session:
        if args.wt_id:
            fail(
                "dispatch accepts either '--session ses_xxx' OR 'wt_N Agent', not both.\n"
                "Examples:\n"
                f'  {PROG} dispatch wt_1 Daedalus --task "..."\n'
                f'  {PROG} dispatch --session ses_xxx --task "..." --yes\n'
                f'  {PROG} session dispatch ses_xxx --task "..." --yes'
            )
    else:
        if not args.wt_id or not args.agent:
            fail(
                "dispatch requires either '--session ses_xxx' or positional 'wt_N Agent'.\n"
                "Examples:\n"
                f'  {PROG} dispatch wt_1 Daedalus --task "..."\n'
                f'  {PROG} dispatch --session ses_xxx --task "..." --yes\n'
                f'  {PROG} session dispatch ses_xxx --task "..." --yes'
            )
    # Validate --model override early so a bad value exits before any
    # destructive action (delete_session + create_session pair below).
    if getattr(args, "model", None):
        opencode_model(config, args.agent or "_validate", override=args.model)
    if args.session:
        # Direct session dispatch — bypass wt_id/state lookup.
        # Used for main-repo agents (Janitor/General) that have persistent
        # sessions created by `sessions create`.
        sid = args.session
        # PM session ownership check: the sid must be present in the current
        # PM's ``main.state`` under some ``{agent}_session_id`` field.
        # Without this, a copy-pasted sid from a different PM conversation
        # would silently overwrite another PM's main.state pointer.
        main_state = read_main_state(config)
        owned_key = None
        for key, val in main_state.items():
            if is_session_pointer_key(key) and val == sid:
                owned_key = key[: -len("_session_id")]
                break
        if not owned_key:
            fail(f"session {sid} does not belong to current PM session {config.pm_session_id or '(unset)'}; use `{PROG} sessions create --agent <agent>` first.")
        ses = get_session_by_id(config, sid)
        if not ses:
            fail(f"session not found: {sid}")
        wt_path = Path(ses.get("directory") or str(config.repo)).resolve()
        wt_id = ses.get("metadata", {}).get("wt_id", "main")
        # Agent precedence: CLI override > main.state ownership > session
        # metadata.  main.state ownership is the most authoritative since
        # ``sessions create --agent <X>`` pins the agent name explicitly.
        agent = args.agent or owned_key or str(ses.get("metadata", {}).get("agent") or ses.get("agent") or "")
        if agent:
            agent = normalize_agent_label(agent)
            if args.agent:
                args.agent = agent
        if not agent:
            fail("--agent required when --session metadata has no agent")
        if not wt_path.is_dir():
            fail(f"session directory not found: {wt_path}")
        # Stale-session guard: ``time.created`` (constant for session
        # lifetime, unlike ``time.updated`` which races with long tasks)
        # older than 1 day means the context has aged out.  Auto-rebuild
        # before dispatching — same effect as ``sessions create --force``.
        # Skip in preview mode (--yes=False): dry-run must not be destructive.
        created_ms = int((ses.get("time") or {}).get("created") or 0)
        if created_ms > 0 and (int(time.time() * 1000) - created_ms) > STALE_SESSION_MS_DEFAULT:
            if not args.yes:
                eprint(f"session {sid} created >1d ago; pass --yes to rebuild ({agent})")
            else:
                eprint(f"session {sid} created >1d ago; auto-rebuilding ({agent})...")
                delete_session(config, sid, hard=True)
                new_ses = create_session(config, "main", wt_path, agent, model_override=getattr(args, "model", None))
                sid = new_ses["id"]
                ses = new_ses
                persist_main_session(config, agent, new_ses, model_override=getattr(args, "model", None))
                # Refresh local main_state so the agent-missing guard below
                # sees the freshly-persisted pointer; otherwise a dispatch
                # where args.agent=X and the sid was owned by agent Y would
                # create the session twice (stale rebuild for X, then
                # agent-missing guard for X).
                main_state = read_main_state(config)
        # Agent-missing guard: --agent was set but main.state has no entry
        # for it (the owned sid belongs to a different agent).  Auto-create
        # the requested agent's session in the main repo so the dispatch
        # lands on the right session.  Skip in preview mode: a destructive
        # create-then-persist in dry-run is unsafe.
        if args.agent and main_state.get(f"{args.agent}_session_id") is None:
            if not args.yes:
                print(
                    json.dumps(
                        {
                            "send": False,
                            "error": f"agent {args.agent} has no session in current PM's main.state",
                            "fix": f"run `{PROG} sessions create --agent {args.agent}` first, then re-dispatch with --yes",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            eprint(f"agent {args.agent} has no entry in current PM's main.state; auto-creating...")
            new_ses = create_session(config, "main", config.repo, args.agent, model_override=getattr(args, "model", None))
            sid = new_ses["id"]
            ses = new_ses
            persist_main_session(config, args.agent, new_ses, model_override=getattr(args, "model", None))
            main_state = read_main_state(config)
        if args.agent:
            agent = normalize_agent_label(args.agent)
        wt_path = Path(config.repo).resolve()
        wt_id = "main"
    else:
        wt_id = args.wt_id
        validate_wt_id(wt_id)
        state = read_state(config, wt_id)
        wt_path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
        agent = normalize_agent_label(args.agent)
        sid = state.get(f"{agent}_session_id") or ""
        ses = get_session_by_id(config, sid, directory=wt_path) if sid else None
        agent_marker = state.get(f"{agent}_task_marker", "")
        wt_marker = state.get("task_marker", "")
        # Decision tree for archive + recreate:
        #   - no sid / OpenCode can't find it → "missing"
        #   - task_marker changed since the agent's last create → "new-task"
        #     (wt was re-prepared; even same-branch re-prepare gets a new uuid)
        #   - session time.updated older than 1 day → "stale (>1d)"
        # else: reuse the existing sid (same task, same agent, fresh cache).
        needs_recreate = False
        reason: str | None = None
        if not ses:
            needs_recreate = True
            reason = "missing"
        elif agent_marker != wt_marker:
            needs_recreate = True
            reason = "new-task"
        elif is_session_stale(ses, max_age_ms=STALE_SESSION_MS_DEFAULT):
            needs_recreate = True
            reason = "stale (>1d)"
        if needs_recreate and args.yes:
            eprint(f"{wt_id} {agent} session {reason}; auto-creating fresh session")
            new_ses = ensure_session(config, wt_id, wt_path, agent, recreate_existing=True, model_override=getattr(args, "model", None))
            sid = new_ses["id"]
            ses = new_ses
            persist_session(config, wt_id, agent, new_ses, model_override=getattr(args, "model", None))
            # Sync the agent's task_marker so the next dispatch of the same
            # agent on the same wt_marker skips the new-task recreate.
            update_state(config, wt_id, {f"{agent}_task_marker": wt_marker})
            state = read_state(config, wt_id)
        elif needs_recreate:
            # Preview path: no side effects (no ensure_session, no
            # persist_session, no watch_session with a fake sid). Tell the
            # user what will happen on --yes and return.
            provider_id, model_id, variant = opencode_model(config, agent, override=getattr(args, "model", None))
            model_label = f"{provider_id}/{model_id}" + (f":{variant}" if variant else "")
            preview = {
                "send": False,
                "wt_id": wt_id,
                "agent": agent,
                "sessionID": f"(auto-create-on-execute: {reason})",
                "auto_create": True,
                "reason": reason,
                "model": model_label,
                "directory": str(wt_path),
                "prompt": render_prompt(wt_path, args.task.strip(), config=config),
            }
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            print()
            print("确认后执行 (--yes 会自动创建 session):")
            notify_flag = f" --notify-session {args.notify_session}" if args.notify_session else ""
            model_flag = f" --model {args.model}" if getattr(args, "model", None) else ""
            print(f"python3 scripts/session-worktree-mgr.py dispatch {args.wt_id} {agent} --task {json.dumps(args.task, ensure_ascii=False)} --yes{notify_flag}{model_flag}")
            return
    watch_session(config, sid)
    status_map = http_json("GET", f"{config.sidecar}/status")
    session_status = "unknown"
    if isinstance(status_map, dict):
        session_status = str(status_map.get(sid, "unknown"))
    if args.require_no_busy and session_status in ("busy", "streaming"):
        fail(f"session {sid} not dispatchable: {session_status} (--require-no-busy rejects busy/streaming)")
    if session_status in ("busy", "streaming"):
        force_recover = getattr(args, "force", False)
        if not force_recover:
            # Auto-recover if session has been stuck > 10 min
            ses = get_session_by_id(config, sid, directory=wt_path)
            if ses and is_session_stale(ses, max_age_ms=STALE_DISPATCH_MS):
                force_recover = True
                eprint(f"auto-recovering stuck session {sid} (busy > {STALE_DISPATCH_MS // 60000}min)")
        if force_recover:
            if getattr(args, "force", False):
                eprint(f"force: soft-archiving {session_status} session {sid} and creating a fresh session")
            delete_session(config, sid, hard=False)
            if args.session:
                # Main session: unwatch old + create new + dispatch
                new_ses = create_session(config, "main", wt_path, agent, model_override=getattr(args, "model", None))
                sid = new_ses["id"]
                ses = new_ses  # update reference for downstream persist_main_session
                persist_main_session(config, agent, new_ses, model_override=getattr(args, "model", None))
                main_state = read_main_state(config)
            else:
                update_state(config, wt_id, {f"{agent}_session_id": ""})
                new_ses = ensure_session(config, wt_id, wt_path, agent, recreate_existing=True, model_override=getattr(args, "model", None))
                sid = new_ses["id"]
                ses = new_ses
                persist_session(config, wt_id, agent, new_ses, model_override=getattr(args, "model", None))
                state = read_state(config, wt_id)
            watch_session(config, sid)
            status_map = http_json("GET", f"{config.sidecar}/status")
            session_status = "unknown"
            if isinstance(status_map, dict):
                session_status = str(status_map.get(sid, "unknown"))
        else:
            fail(f"session {sid} is not dispatchable: {session_status} (use --force to unwatch stuck session and create a fresh one)")
    elif getattr(args, "force", False):
        eprint(f"note: --force ignored: session {sid} is {session_status}; only busy/streaming sessions are archived and recreated")
    state_for_model = main_state if args.session else state
    provider_id, model_id, variant = dispatch_model(
        config,
        agent,
        ses,
        override=getattr(args, "model", None),
        state=state_for_model,
    )
    prompt = render_prompt(wt_path, args.task.strip(), config=config)
    body: dict[str, Any] = {
        "agent": agent,
        "model": prompt_model(provider_id, model_id),
        "parts": [{"type": "text", "text": prompt}],
    }
    if variant:
        body["variant"] = variant
    model_label = f"{provider_id}/{model_id}" + (f":{variant}" if variant else "")
    preview = {
        "send": bool(args.yes),
        "wt_id": wt_id,
        "agent": agent,
        "sessionID": sid,
        "status": session_status,
        "model": model_label,
        "directory": str(wt_path),
        "prompt": prompt,
    }
    if not args.yes:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        print()
        print("确认后执行：")
        notify_flag = f" --notify-session {args.notify_session}" if args.notify_session else ""
        model_flag = f" --model {args.model}" if getattr(args, "model", None) else ""
        if args.session:
            # Direct session dispatch — no wt_id/agent positional args
            print(f"python3 scripts/session-worktree-mgr.py dispatch --session {sid} --task {json.dumps(args.task, ensure_ascii=False)} --yes{notify_flag}{model_flag}")
        else:
            print(f"python3 scripts/session-worktree-mgr.py dispatch {args.wt_id} {agent} --task {json.dumps(args.task, ensure_ascii=False)} --yes{notify_flag}{model_flag}")
        return
    query = urllib.parse.urlencode({"directory": str(wt_path)})
    url = f"{config.op_server}/session/{sid}/prompt_async?{query}"
    # Capture the dispatch timestamp before prompt_async so the auto idle-watch
    # can detect very short tasks that finish before the watcher observes
    # busy/streaming.  The fallback only fires when an assistant reply appears
    # after this timestamp, so it does not reintroduce initial-idle false
    # positives.
    dispatch_started_at_ms = int(time.time() * 1000)
    http_json("POST", url, body, expected=(204,))
    if args.session:
        persist_main_session(config, agent, ses)  # type: ignore
    else:
        clear_stored_model_override(config, wt_id, agent, sid)
    disp_label = f"{wt_id}-{agent}" if not args.session else f"main-{agent}"
    print(f"dispatched -> {disp_label} ({sid}) status=accepted model={model_label} directory={wt_path}")
    notify_sid = args.notify_session or config.pm_session_id
    if notify_sid:
        _idle_validate_ses("--notify-session", notify_sid)
        _spawn_dispatch_idle_watch(
            config,
            sid,
            notify_sid,
            wt_path,
            wt_id=wt_id if not args.session else "main",
            agent=agent,
            max_poll_seconds=args.max_poll_seconds,
            started_at_ms=dispatch_started_at_ms,
        )
        print("agent 完成时会异步通知（idle-watch 已挂载），无需轮询。")


def cmd_release(args: argparse.Namespace, config: Config) -> None:
    wt_id, wt_path = resolve_pool_wt_target(config, args.target)
    assert_worktree_root(wt_path)
    with pool_lock(config):
        if not is_clean_worktree(wt_path):
            if not args.force:
                status = git(wt_path, "status", "--porcelain", capture=True).stdout
                eprint(status)
                fail("worktree is dirty. Commit/stash changes or use --force")
            git(wt_path, "reset", "--hard")
            git(wt_path, "clean", "-fd")
        ensure_base_ref(wt_path, config.base_ref)
        reset_to_base(wt_path, config.base_ref)
        # Evict sessions older than STALE_SESSION_MS_DEFAULT (1 day) so the
        # next prepare creates a fresh session pool instead of reusing cold cache.
        cleaned = cleanup_stale_sessions(config, wt_id)
        update_state(
            config,
            wt_id,
            {"status": "idle", "branch": "", "wt_path": str(wt_path), "base_ref": config.base_ref},
        )
    print(f"wt_id={wt_id}")
    print(f"wt_path={wt_path}")
    print("status=idle")
    if cleaned:
        for agent, sid in cleaned:
            # The OpenCode session is NOT deleted — only the state pointer
            # is evicted. The session remains in OpenCode for history.
            print(f"evicted from state (session left in OpenCode): {agent} {sid}")


# ---------------- sessions management ----------------


def session_items_for_wt(config: Config, wt_id: str, wt_path: Path, agents: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Main worktree sessions use "main" as wt_id in titles, not the repo dir name.
    title_prefix = "main" if wt_id == "xidi-minimal" else wt_id
    for agent in agents:
        title = f"{title_prefix}-{agent}"
        for session in sessions(config, directory=wt_path, search=title, limit=100):
            if session.get("title") == title:
                item = dict(session)
                item["_agent"] = agent
                items.append(item)
    return items


def session_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("updatedAt") or item.get("time", {}).get("updated") or item.get("id") or "")


def cmd_session_create(args: argparse.Namespace, config: Config) -> None:
    """Create a persistent session for a main-repo agent (Janitor/General/Momus/Clio).

    These agents work in the main repository directory (not in a worktree),
    and their sessions outlive the prepare→dispatch→release cycle.  Session
    ID is persisted per PM session in ``<pool_dir>/.state/sessions/<pm_sid>/main.state``
    so that each PM conversation gets its own set of main agents — no
    cross-conversation context leak.

    By default idempotent: returns existing non-stale session if one exists.
    Sessions are rebuilt when age exceeds 1 day.  When context exceeds
    ``MAX_MAIN_SESSION_CONTEXT`` (500K tokens) but the session is younger
    than 1 day, auto-compact is attempted first (summarize → reuse).
    ``--force`` hard-deletes the existing session and creates a fresh one
    unconditionally.

    If the existing session is reported by the sidecar as ``busy`` or
    ``streaming`` (another dispatch in flight), creation is refused — a task
    is in flight and an automatic rebuild would silently destroy it.  An
    explicit ``--force`` is required to hard-delete and recreate.  ``unknown``
    status (sidecar unreachable / unwatched) is treated as "cannot prove busy
    → safe to reuse".
    """
    check_services(config)
    agent = args.agent
    directory = Path(args.directory).expanduser().resolve() if args.directory else config.repo
    if not directory.is_dir():
        fail(f"directory not found: {directory}")
    # Validate --model override early so a bad value exits before any
    # destructive action (delete_session + create_session pair below).
    if getattr(args, "model", None):
        opencode_model(config, agent, override=args.model)
    title = f"main-{agent}"
    # If a session for this agent already exists in this PM session's state,
    # reuse it (create_session is idempotent in the sense that we only call it
    # when we don't already have a live one).  If the persisted session is stale
    # (>1d), recreate.
    main_state = read_main_state(config)
    existing_sid = main_state.get(f"{agent}_session_id")
    # tombstoned sessions must not be resurrected — clear the state pointer
    # so the create path below produces a fresh session.
    if existing_sid:
        tombstoned = _tombstoned_sids(main_state)
        if existing_sid in tombstoned:
            eprint(f"tombstoned session skipped: {existing_sid}")
            main_state[f"{agent}_session_id"] = ""
            write_main_state(config, main_state)
            existing_sid = None
    if existing_sid and not args.force:
        ses = get_session_by_id(config, existing_sid, directory=directory)
        if ses and not is_session_stale(ses):
            session_status = _get_session_status(config, existing_sid)
            if session_status in ("busy", "streaming"):
                # BL-TOOLCHAIN-CREATE-BUSY-DELETE: never auto-rebuild a busy
                # session — that destroys an in-flight task without the user
                # asking for it.  Refuse and require an explicit --force.
                fail(
                    f"existing session {existing_sid} ({agent}) is {session_status}; "
                    f"refusing to auto-rebuild — a task is in flight. "
                    f"Pass --force to hard-delete and recreate it (destroys the running task), "
                    f"or wait until the session goes idle."
                )
            else:
                ctx = fetch_session_context(config, existing_sid)
                if ctx > MAX_MAIN_SESSION_CONTEXT:
                    eprint(f"context exceeded ({ctx // 1000}K > {MAX_MAIN_SESSION_CONTEXT // 1000}K): auto-compacting {agent} {existing_sid}")
                    result = auto_compact_session(config, existing_sid, str(directory), threshold=_AUTO_COMPACT_THRESHOLD, timeout=_AUTO_COMPACT_HTTP_TIMEOUT)
                    if result.compacted:
                        after = fetch_session_context(config, existing_sid)
                        eprint(f"auto-compact ok: context {ctx // 1000}K → {after // 1000}K; reusing {agent} {existing_sid}")
                        print(json.dumps({"sessionID": existing_sid, "agent": agent, "title": title, "directory": str(directory), "status": "existing"}, ensure_ascii=False))
                        return
                    # compact failed — user-initiated rebuild, hard-delete the
                    # bloated session so the new create_session() starts from a
                    # clean OpenCode record. A soft delete here would leave a
                    # tombstoned sid in the main.state and re-prompt the same
                    # auto-compact path on the next sessions create.
                    eprint(f"auto-compact failed ({result.error}); rebuilding {agent} {existing_sid} (hard-delete)")
                    delete_session(config, existing_sid, hard=True)
                else:
                    if getattr(args, "model", None):
                        # --model override means the existing session (bound to
                        # the agent's default model) is stale for the user's
                        # intent. Force-rebuild so the override actually applies.
                        eprint(f"--model override set: rebuilding existing session {existing_sid} (was using agent default model)")
                        delete_session(config, existing_sid, hard=True)
                    else:
                        print(json.dumps({"sessionID": existing_sid, "agent": agent, "title": title, "directory": str(directory), "status": "existing"}, ensure_ascii=False))
                        return
        elif ses:
            eprint(f"stale session archived: {agent} {existing_sid}")
            delete_session(config, existing_sid)
    elif existing_sid and args.force:
        eprint(f"force: deleting existing session: {agent} {existing_sid}")
        delete_session(config, existing_sid, hard=True)
    elif args.force and not existing_sid:
        # ``--force`` is a no-op when there's nothing to delete. Surface
        # the no-op rather than silently creating a fresh session — the
        # user explicitly asked for "force-replace" semantics and we
        # should be honest about the empty prior state.
        eprint("note: --force ignored: no existing session in state")
    session = create_session(config, "main", directory, agent, model_override=getattr(args, "model", None))
    persist_main_session(config, agent, session, model_override=getattr(args, "model", None))
    watch_session(config, session["id"])
    print(json.dumps({"sessionID": session.get("id"), "agent": agent, "title": title, "directory": str(directory), "status": "created"}, ensure_ascii=False))


def _main_state_file(config: Config) -> Path:
    """Return the path to the main-agent session state file.

    When a PM session is active (``config.pm_session_id`` non-empty), the file
    is scoped to that PM session so that each PM conversation gets its own set
    of main agents (Momus, Clio, Janitor, General).  Old global state
    (``.state/main.state``) is migrated to the per-session path on first access.
    """
    if config.pm_session_id:
        session_dir = state_dir(config) / "sessions" / config.pm_session_id
        new_path = session_dir / "main.state"
        old_path = state_dir(config) / "main.state"
        session_dir.mkdir(parents=True, exist_ok=True)
        if old_path.exists() and not new_path.exists():
            shutil.copy2(str(old_path), str(new_path))
        return new_path
    return state_dir(config) / "main.state"


def read_main_state(config: Config) -> dict[str, str]:
    # Reuse the canonical key=value parser so wt_N.state and per-PM
    # main.state share one implementation. Kept as a thin shim because
    # _read_state_file is also called directly for non-PM-scoped paths.
    return _read_state_file(_main_state_file(config))


def build_pm_session_map(config: Config) -> dict[str, tuple[str, bool]]:
    """Return session_id -> (pm_session_id, is_current) for recent PM state.

    Only the bounded recent PM state set is scanned (current PM + newest
    historical PM state by default).  The PM session itself is mapped from the
    ``sessions/<pm_sid>`` directory name in addition to child main-agent session
    ids stored inside ``main.state``.
    """
    result: dict[str, tuple[str, bool]] = {}
    for pm_sid, state_file, is_current in recent_pm_state_files(config):
        if pm_sid:
            result[pm_sid] = (pm_sid, is_current)
        state = _read_state_file(state_file)
        tombstoned = _tombstoned_sids(state)
        for key, val in state.items():
            if is_session_pointer_key(key) and val and val not in tombstoned:
                result[val] = (pm_sid, is_current)
    return result


def write_main_state(config: Config, values: dict[str, str]) -> None:
    # Route through _write_state_file (tmp + os.replace) so concurrent
    # update_main_state() / _add_tombstone() cannot lose patches via a
    # non-atomic read-modify-write on the per-PM main.state file.
    path = _main_state_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_state_file(path, values)


def update_main_state(config: Config, patch: dict[str, str]) -> dict[str, str]:
    return _update_state_file(_main_state_file(config), patch)


def persist_main_session(
    config: Config,
    agent: str,
    session: dict[str, Any],
    *,
    model_override: str | None = None,
) -> None:
    sid = str(session.get("id") or "")
    if not sid:
        fail(f"session missing id for main-{agent}")
    patch = {
        f"{agent}_session_id": sid,
        f"{agent}_session_title": str(session.get("title") or f"main-{agent}"),
        f"{agent}_model_override": "",
        f"{agent}_model_session_id": "",
    }
    if model_override:
        provider_id, model_id, variant = opencode_model(config, agent, override=model_override)
        patch[f"{agent}_model_override"] = model_label(provider_id, model_id, variant)
        patch[f"{agent}_model_session_id"] = sid
    update_main_state(
        config,
        patch,
    )
    watch_session(config, sid)


def _warn_deprecated(message: str) -> None:
    eprint(f"DEPRECATED: {message}")


def _filter_for_pm_ownership(config: Config, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only sessions whose id appears in the current PM's ``main.state``.

    Per-PM isolation: when ``config.pm_session_id`` is set, ``main.state`` is
    the source of truth for which main-agent session IDs belong to the
    current PM conversation.  Without this filter, ``sessions list --main``
    would scan the full directory and surface other PMs' session IDs.

    - ``config.pm_session_id`` empty → pass through (legacy / no-PM envs).
    - ``config.pm_session_id`` set + empty main.state → empty list (the
      current PM owns nothing; show honest empty instead of every PM's
      sessions).
    - ``config.pm_session_id`` set + populated main.state → keep only
      items whose ``id`` matches a ``{agent}_session_id`` field.

    Display-only filter, no side effects.
    """
    if not config.pm_session_id:
        return items
    main_state = read_main_state(config)
    owned_sids: set[str] = set()
    for key, val in main_state.items():
        if is_session_pointer_key(key) and val:
            owned_sids.add(str(val))
    if not owned_sids:
        return []
    return [it for it in items if str(it.get("id") or "") in owned_sids]


def _resolve_sessions_filter(args: argparse.Namespace, config: Config) -> tuple[str, Path]:
    """Resolve sessions list/delete target from explicit filters.

    Preferred interface:
      - --wt wt_N
      - --main
      - --path /abs/worktree

    Back-compat:
      - positional target (hidden in help) still works, but emits a deprecation warning.
    """
    selected = [bool(getattr(args, "wt", None)), bool(getattr(args, "main", False)), bool(getattr(args, "path", None))]
    if sum(selected) > 1:
        fail(
            f"sessions accepts exactly one target filter: --wt, --main, or --path.\nExamples:\n  {PROG} sessions list --wt wt_1\n  {PROG} sessions list --main\n  {PROG} sessions list --path /abs/path"
        )
    if getattr(args, "target", None):
        if any(selected):
            fail("do not combine legacy positional target with --wt/--main/--path")
        _warn_deprecated(f'use "sessions list --wt wt_N" or "sessions list --main" instead of positional target {args.target!r}.')
        return resolve_wt_id_or_path(config, args.target)
    if getattr(args, "main", False):
        return "xidi-minimal", config.repo
    if getattr(args, "wt", None):
        wt = args.wt
        if wt.isdigit():
            wt = f"wt_{wt}"
        return resolve_wt_id_or_path(config, wt)
    if getattr(args, "path", None):
        return resolve_wt_id_or_path(config, args.path)
    fail(
        "sessions list/delete requires one target filter: --wt wt_N, --main, or --path /abs/path.\n"
        "For one session by ID, use:\n"
        f"  {PROG} session show ses_xxx\n"
        f"  {PROG} session status ses_xxx\n"
        f"  {PROG} session last ses_xxx"
    )


def _state_files_for_all_sessions(config: Config) -> list[tuple[str, Path]]:
    """Return (scope_id, state_file_path) pairs for wt_N and per-PM main states."""
    sd = state_dir(config)
    out: list[tuple[str, Path]] = []
    if not sd.is_dir():
        return out
    for sf in sorted(sd.glob("wt_*.state")):
        wt_id = sf.stem
        if re.fullmatch(r"wt_[0-9]+", wt_id):
            out.append((wt_id, sf))
    sessions_root = sd / "sessions"
    if sessions_root.is_dir():
        for pm_dir in sorted(sessions_root.iterdir()):
            if not pm_dir.is_dir():
                continue
            sf = pm_dir / "main.state"
            if sf.exists():
                out.append(("xidi-minimal", sf))
    old_main = sd / "main.state"
    if old_main.exists():
        out.append(("xidi-minimal", old_main))
    return out


def _add_tombstone_by_session_id(config: Config, sid: str) -> list[str]:
    """Soft-delete by session id wherever it appears in persisted state."""
    touched: list[str] = []
    for scope, sf in _state_files_for_all_sessions(config):
        state = _read_state_file(sf)
        if not any(is_session_pointer_key(k) and v == sid for k, v in state.items()):
            continue
        tombstoned = _tombstoned_sids(state)
        tombstoned.add(sid)
        state["deleted_session_ids"] = ",".join(sorted(tombstoned))
        _write_state_file(sf, state)
        touched.append(f"{scope}:{sf}")
    return touched


def _remove_session_pointer_by_session_id(config: Config, sid: str) -> list[str]:
    """Hard-delete: remove every ``*_session_id`` / ``*_session_title`` / tombstone
    entry that references ``sid`` from all persisted state files.

    Returns the list of ``scope:path`` strings that were touched.
    """
    touched: list[str] = []
    for scope, sf in _state_files_for_all_sessions(config):
        state = _read_state_file(sf)
        changed = False
        for key, val in list(state.items()):
            if is_session_pointer_key(key) and val == sid:
                agent = key[: -len("_session_id")]
                state.pop(key, None)
                state.pop(f"{agent}_session_title", None)
                state.pop(f"{agent}_model_override", None)
                state.pop(f"{agent}_model_session_id", None)
                changed = True
        tombstoned = _tombstoned_sids(state)
        if sid in tombstoned:
            tombstoned.discard(sid)
            state["deleted_session_ids"] = ",".join(sorted(tombstoned)) if tombstoned else ""
            changed = True
        if changed:
            _write_state_file(sf, state)
            touched.append(f"{scope}:{sf}")
    return touched


def _session_agent_label(session: dict[str, Any]) -> str:
    meta = session.get("metadata") or {}
    agent = meta.get("agent") or session.get("agent") or "__unknown__"
    return normalize_agent_label(agent)


def _sessions_for_filter(config: Config, wt_id: str, wt_path: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Return sessions for sessions list/delete.

    If --agent/--agents is supplied, use exact title-based lookup for those
    agents. If omitted, list every session belonging to the target worktree/main
    directory so AI does not need to infer the agent set.
    """
    if getattr(args, "agent", None) or getattr(args, "agents", None):
        agents = [args.agent] if args.agent else parse_agents(args.agents)
        return session_items_for_wt(config, wt_id, wt_path, agents)
    items: list[dict[str, Any]] = []
    for s in collect_wt_sessions(config, wt_id, str(wt_path)):
        item = dict(s)
        item["_agent"] = _session_agent_label(item)
        items.append(item)
    return items


def cmd_sessions_list(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        fail(
            '"sessions list" lists multiple sessions by filters and does not accept --session.\n'
            "For one session, use:\n"
            f"  {PROG} session show {args.session}\n"
            f"  {PROG} session status {args.session}\n"
            f"  {PROG} session last {args.session}"
        )
    wt_id, wt_path = _resolve_sessions_filter(args, config)
    items = _sessions_for_filter(config, wt_id, wt_path, args)
    # ``--main`` is the only target with a PM dimension; ``--wt``/``--path``
    # are worktree-scoped and pass through unchanged.  When ``pm_session_id``
    # is empty, the filter is a no-op (legacy / no-PM environments).
    if getattr(args, "main", False) and config.pm_session_id:
        items = _filter_for_pm_ownership(config, items)
    items.sort(key=session_sort_key, reverse=True)
    if args.format == "json":
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for item in items:
        updated = item.get("updatedAt") or (item.get("time") or {}).get("updated", "")
        print(f"{item.get('id')}\t{item.get('title')}\t{item.get('directory')}\t{updated}")


def cmd_sessions_delete(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        fail(
            '"sessions delete" deletes multiple sessions by filters and does not accept --session.\n'
            "For one session, use:\n"
            f"  {PROG} session delete {args.session} --yes\n"
            f"  {PROG} session delete {args.session} --hard --yes"
        )
    wt_id, wt_path = _resolve_sessions_filter(args, config)
    items = _sessions_for_filter(config, wt_id, wt_path, args)
    items.sort(key=session_sort_key, reverse=True)
    if args.keep_latest:
        kept: set[str] = set()
        delete_list: list[dict[str, Any]] = []
        for item in items:
            agent = str(item.get("_agent"))
            if agent not in kept:
                kept.add(agent)
                continue
            delete_list.append(item)
    else:
        delete_list = items
    print(
        json.dumps(
            {
                "dryRun": not args.yes,
                "target": wt_id,
                "directory": str(wt_path),
                "mode": "hard-delete" if args.hard else "soft-delete/tombstone",
                "delete": [{"id": x.get("id"), "title": x.get("title"), "agent": x.get("_agent")} for x in delete_list],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not args.yes:
        print()
        print("确认删除后加 --yes")
        return
    for item in delete_list:
        sid = str(item.get("id") or "")
        if not sid:
            continue
        if not args.hard:
            _add_tombstone(config, wt_id, sid)
        else:
            _remove_session_pointer_by_session_id(config, sid)
        delete_session(config, sid, hard=args.hard)
        eprint(f"{'tombstoned' if not args.hard else 'deleted'} session: {sid}{' (hard)' if args.hard else ''}")


# ---------------- status / last ----------------


def cmd_status(args: argparse.Namespace, config: Config) -> None:
    if args.session:
        data = http_json("GET", f"{config.sidecar}/sessions/{urllib.parse.quote(args.session)}")
    elif args.detail:
        data = http_json("GET", f"{config.sidecar}/sessions")
    else:
        data = http_json("GET", f"{config.sidecar}/status")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_last(args: argparse.Namespace, config: Config) -> None:
    data = http_json(
        "GET",
        f"{config.op_server}/session/{urllib.parse.quote(args.session)}/message?limit={args.limit}",
    )
    if not isinstance(data, list):
        fail("unexpected message response")
    assistant_msgs = [m for m in data if m.get("info", {}).get("role") == "assistant"]
    if not assistant_msgs:
        print("(no assistant messages)")
        return

    def msg_time(msg: dict[str, Any]) -> int | float:
        t = msg.get("info", {}).get("time", {})
        return t.get("completed") or t.get("created") or 0

    last = sorted(assistant_msgs, key=msg_time)[-1]
    texts = [p.get("text", "") for p in last.get("parts", []) if p.get("type") == "text"]
    print("".join(texts) if texts else "(no text parts)")


# ---------------- overview ----------------

# Overview fetches ALL sessions once (not per-wt) to minimise HTTP calls to
# the OpenCode server.  A single ``GET /session?limit=2000`` replaces ~10
# per-worktree calls.  The client-side filter mirrors ``collect_wt_sessions``.
_OVERVIEW_SESSION_LIMIT = 2000


def _filter_sessions_for_wt(
    all_sessions: list[dict[str, Any]],
    wt_id: str,
    wt_path: str,
) -> list[dict[str, Any]]:
    """Return sessions belonging to wt_id from a pre-fetched master list."""
    if wt_id == "xidi-minimal":
        out: list[dict[str, Any]] = []
        n_wt_path = normalize_path(wt_path)
        for s in all_sessions:
            meta = s.get("metadata", {}) or {}
            sdir = s.get("directory", "")
            sid_wt = meta.get("wt_id")
            if sid_wt and sid_wt != "main":
                continue
            if not sdir or normalize_path(str(sdir)) != n_wt_path:
                continue
            out.append(s)
        return out
    # wt_N: match by metadata.wt_id or title prefix
    out = []
    for s in all_sessions:
        meta = s.get("metadata", {}) or {}
        title = s.get("title", "")
        if meta.get("wt_id") == wt_id or title.startswith(f"{wt_id}-"):
            out.append(s)
    return out


def collect_worktree_list(repo: Path) -> list[dict[str, str]]:
    """List all worktrees via ``git worktree list --porcelain``.

    Includes detached worktrees (HEAD not on a local branch) — previously
    silently dropped because the parser only handled ``branch`` lines.
    Detached entries get ``branch=""`` to match ``wt.state`` file convention.
    """
    raw = git(repo, "worktree", "list", "--porcelain", capture=True).stdout
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("worktree "):
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("branch ") and current is not None:
            branch = line[len("branch ") :].removeprefix("refs/heads/")
            current["branch"] = branch
            current["id"] = Path(current["path"]).name
            entries.append(current)
            current = None
        elif line == "detached" and current is not None:
            # Detached HEAD: append with empty branch (matches wt.state file convention)
            current["branch"] = ""
            current["id"] = Path(current["path"]).name
            entries.append(current)
            current = None
    return entries


def enrich_worktree_status(wt: dict[str, str]) -> None:
    """Mutate ``wt`` with commit / dirty / ahead_main (best-effort).

    All git calls have a 5 s timeout so a broken worktree (stale index.lock,
    NFS hang, etc.) does not block overview indefinitely.  KeyboardInterrupt
    (user Ctrl+C before timeout fires) is caught and treated as a timeout so
    the caller can continue rendering other worktrees.
    """
    wt_path = wt["path"]
    try:
        wt["commit"] = git(Path(wt_path), "rev-parse", "--short", "HEAD", capture=True, timeout=5).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyboardInterrupt):
        wt["commit"] = "?"
    try:
        status = git(Path(wt_path), "status", "--porcelain", capture=True, timeout=5).stdout.strip()
        wt["dirty"] = "dirty" if status else "clean"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyboardInterrupt):
        wt["dirty"] = "?"
    try:
        # ``origin/main..HEAD`` is the count of commits reachable from HEAD
        # but not from origin/main — i.e. how far ahead this worktree is
        # over the fetched ``main``. The previous ``HEAD...origin/main``
        # triple-dot is the symmetric difference (ahead + behind), which
        # grows forever as main advances and is not what ``ahead_main``
        # claims to show.
        ahead = git(Path(wt_path), "rev-list", "--count", "origin/main..HEAD", capture=True, timeout=5).stdout.strip()
        wt["ahead_main"] = ahead
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyboardInterrupt):
        wt["ahead_main"] = "?"


def collect_wt_sessions(config: Config, wt_id: str, wt_path: str) -> list[dict[str, Any]]:
    """Return sessions for a worktree identified by ``wt_id``/title prefix or directory match.

    Filters:
      - ``wt_N``: ``meta.wt_id == wt_id`` OR ``title.startswith(f"{wt_id}-")``
      - main worktree (``xidi-minimal``): ``directory`` exact match (no ``meta.wt_id``)

    No ``managedBy`` filter — pool-managed, legacy ``worktree_session.py``, and
    user-created sessions (e.g. ``P0-T3-*`` explorations) all surface so overview
    reflects the true session landscape per worktree. Earlier hardcoded
    ``managedBy == "session-worktree-mgr.py"`` filter dropped ~3 sessions per
    wt that were created under the pre-PR-#3 ``worktree_session.py`` name.
    """
    if wt_id == "xidi-minimal":
        # main worktree: sessions with wt_id="main" (sessions create produced)
        # or legacy sessions without any wt_id metadata
        candidates = sessions(config, limit=500)
        out = []
        seen_ids: set[str] = set()
        for s in candidates:
            meta = s.get("metadata", {}) or {}
            sdir = s.get("directory", "")
            sid_wt = meta.get("wt_id")
            if sid_wt and sid_wt != "main":
                continue
            if not sdir or normalize_path(sdir) != normalize_path(wt_path):
                continue
            sid = str(s.get("id") or "")
            if sid:
                seen_ids.add(sid)
            out.append(s)

        # Also materialize PM sessions referenced only by bounded state files.
        # They may be older than the generic /session?limit=500 result window,
        # but the state directory is the source of truth for PM ownership.
        for pm_sid, _state_file, _is_current in recent_pm_state_files(config):
            if not pm_sid or pm_sid in seen_ids:
                continue
            try:
                state_pm_session = http_json(
                    "GET",
                    f"{config.op_server}/session/{urllib.parse.quote(pm_sid)}",
                )
            except SystemExit:
                continue
            if not isinstance(state_pm_session, dict):
                continue
            sdir = state_pm_session.get("directory", "")
            if not sdir or normalize_path(str(sdir)) != normalize_path(wt_path):
                continue
            seen_ids.add(pm_sid)
            out.append(state_pm_session)

    else:
        # wt_N: match by metadata.wt_id or title prefix
        candidates = sessions(config, directory=Path(wt_path), limit=500)
        out = []
        for s in candidates:
            meta = s.get("metadata", {}) or {}
            title = s.get("title", "")
            if meta.get("wt_id") == wt_id or title.startswith(f"{wt_id}-"):
                out.append(s)
    return out


def session_summary(s: dict[str, Any]) -> dict[str, Any]:
    """Reduce a session to id / title / agent / cost / token breakdown for overview output.

    Token breakdown (replaces a single summed ``Tokens`` field — that 20x-distorted
    number hid the 95% cache-hit ratio that's actually driving usage):
      - ``input``:        cumulative input tokens (user prompts)
      - ``out_reason``:   output + reasoning tokens (model-generated, charged at full rate)
      - ``cache_read``:   cumulative cache hits (often the dominant field; cheap rate)
    """
    meta = s.get("metadata", {}) or {}
    tk = s.get("tokens", {}) or {}
    cache = tk.get("cache", {}) or {}
    time_obj = s.get("time") or {}
    return {
        "id": s.get("id", "-"),
        "title": s.get("title", "-"),
        "agent": normalize_agent_label(meta.get("agent") or s.get("agent") or "-"),
        "cost": s.get("cost", 0),
        "input": tk.get("input", 0),
        "out_reason": tk.get("output", 0) + tk.get("reasoning", 0),
        "cache_read": cache.get("read", 0),
        "updated_ms": int(time_obj.get("updated") or 0),
    }


def fmt_tokens(n: int) -> str:
    """Format token count as 12K / 1300 / 800."""
    if n >= 1000:
        return f"{n / 1000:.0f}K"
    return str(n)


def fmt_updated(ms: int) -> str:
    """Format a millisecond timestamp as relative age: now / Xm / Xh / Xd / MM-DD.

    Used in the overview ``Updated`` column to surface stale sessions at a glance
    (cache stays warm ~hours; > 1d old = cache effectively cold for new prompts).
    """
    if not ms:
        return "-"
    delta_s = (int(time.time() * 1000) - ms) / 1000
    if delta_s < 0:
        return "now"
    if delta_s < 60:
        return "now"
    if delta_s < 3600:
        return f"{int(delta_s / 60)}m"
    if delta_s < 86400:
        return f"{int(delta_s / 3600)}h"
    if delta_s < 86400 * 30:
        return f"{int(delta_s / 86400)}d"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%m-%d")


def _parse_duration(value: str) -> int:
    """Parse a duration string into seconds.

    Accepted forms: ``"0"``, ``"<N>d"``, ``"<N>h"``, ``"<N>m"``, ``"<N>s"``.
    Case-insensitive on the unit suffix. Empty string, negative numbers, or
    unknown units call ``fail()`` (which raises SystemExit).
    """
    s = value.strip()
    if not s:
        fail("duration must not be empty")
    if s == "0":
        return 0
    # Must end with a single unit char; rest must be a non-negative integer.
    if len(s) < 2:
        fail(f"invalid duration: {value!r}")
    unit = s[-1].lower()
    body = s[:-1]
    if not body.isdigit():
        fail(f"invalid duration: {value!r}")
    n = int(body)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    fail(f"invalid duration unit: {value!r} (use s/m/h/d)")


def _apply_recent_filter(
    sessions: list[dict[str, Any]],
    now_ms: int,
    recent_seconds: int,
    pm_session_id: str = "",
) -> list[dict[str, Any]]:
    """Group sessions by ``(wt_id, [pm_session_id,] agent)`` and keep only the recent window.

    Each input dict must carry:

    - ``wt_id``        : str  (worktree id, e.g. ``"wt_1"``); if missing, item is dropped.
    - ``agent``        : str  (e.g. ``"Daedalus"``); if missing, falls back to ``"__unknown__"``.
    - ``updated_ms``   : int  (epoch ms); ``0`` is treated as "unknown age".
    - ``pm_session_id`` (optional): str; only consulted when the ``pm_session_id``
      argument is non-empty (see below).

    Per-group semantics (group key is ``(wt_id, pm, agent)``):

    - If any item has ``updated_ms > now_ms - recent_seconds * 1000`` (strict ``>``),
      keep ONLY the in-window items in the group (strict semantics: ``--recent
      1d`` means "show what updated in the last 1 day, nothing older").
    - Else keep only the single item with the largest ``updated_ms`` (the
      "last-known tombstone") to preserve user-visible history.
    - Empty group → empty output.

    ``pm_session_id`` argument:
      - Empty (default, back-compat): every item's PM bucket is the empty
        string, so the group key collapses to ``(wt_id, agent)`` — identical
        behavior to the original implementation.
      - Non-empty (e.g. ``"pm_session_id"``): treated as the dict key whose
        value should become the per-PM bucket. Two PM sessions that both own
        ``Janitor`` sessions no longer share a single bucket and don't steal
        each other's "last-known tombstone".

    The original order is preserved for retained items (stable within the group);
    groups themselves are emitted in first-seen order.

    Pass ``recent_seconds = 0`` to disable the window: every group collapses
    to its single newest item.
    """
    if recent_seconds < 0:
        fail("recent_seconds must be >= 0")
    threshold_ms = now_ms - recent_seconds * 1000

    # 1. Bucket by (wt_id, pm, agent), preserving first-seen order.
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, str]] = []
    for item in sessions:
        wt_id = item.get("wt_id")
        if not wt_id or not isinstance(wt_id, str):
            # Defensive: callers should always pass wt_id. Drop silently
            # rather than fabricating a bucket key.
            continue
        agent_raw = item.get("agent")
        agent = normalize_agent_label(agent_raw)
        pm = ""
        if pm_session_id:
            pm_raw = item.get(pm_session_id, "")
            pm = pm_raw if isinstance(pm_raw, str) and pm_raw else ""
        key = (wt_id, pm, agent)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)

    # 2. For each bucket, decide keep-in-window-only vs keep-newest.
    out: list[dict[str, Any]] = []
    for key in order:
        items = buckets[key]
        has_recent = any(int(i.get("updated_ms") or 0) > threshold_ms for i in items)
        if has_recent:
            # Strict semantics: --recent N means "only the last N". Older items
            # in the same group are dropped, not "kept for thread continuity"
            # (that interpretation was a spec deviation; see devlog for
            # ``feat_overview_recent_filter_strict``).
            out.extend(i for i in items if int(i.get("updated_ms") or 0) > threshold_ms)
        else:
            # last-known tombstone: max updated_ms; tie-break by first seen.
            newest = max(
                items,
                key=lambda i: (int(i.get("updated_ms") or 0), -items.index(i)),
            )
            out.append(newest)
    return out


def _limit_per_agent(
    sessions: list[dict[str, Any]],
    limits: dict[str, int],
    pm_session_id: str = "",
    default_limit: int = 1,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Keep at most ``limits[agent]`` sessions per agent, newest first by ``updated_ms``.

    When ``pm_session_id`` is set, the limit is applied independently inside
    each PM bucket (read from each item via ``item.get(pm_session_id)``), so
    two PM sessions owning sessions of the same agent do not steal each
    other's slots. When ``pm_session_id`` is empty (default), PM-bucketing is
    skipped — every agent's limit is global (back-compat with the original
    behavior).

    Agents not listed in ``limits`` are NOT dropped silently: they fall back
    to ``default_limit`` (default 1) and a warning is emitted to stderr. This
    preserves the "last-known tombstone" for any new agent the pool learns
    about (e.g. a fresh ``Clio2`` next to the whitelisted ``Clio``) instead
    of vanishing from the overview. Pass ``default_limit=0`` to restore the
    original "drop unlisted agents" behavior.

    ``verbose`` (default False) gates the stderr warning. The fallback
    behavior itself (keep ``default_limit`` sessions for unlisted agents) is
    unchanged — only the warning is silenced unless the caller opts in via
    ``--verbose`` (overview subcommand).
    """
    out: list[dict[str, Any]] = []

    def _select(items: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
        if pm_session_id:
            buckets: dict[str, list[dict[str, Any]]] = {}
            order: list[str] = []
            for s in items:
                pm_raw = s.get(pm_session_id, "")
                pm = pm_raw if isinstance(pm_raw, str) and pm_raw else ""
                if pm not in buckets:
                    buckets[pm] = []
                    order.append(pm)
                buckets[pm].append(s)
            selected: list[dict[str, Any]] = []
            for pm in order:
                bucket = buckets[pm]
                bucket.sort(key=lambda x: int(x.get("updated_ms", 0)), reverse=True)
                selected.extend(bucket[:cap])
            return selected
        items.sort(key=lambda x: int(x.get("updated_ms", 0)), reverse=True)
        return items[:cap]

    # 1. Listed agents get their declared limit.
    listed_agents = set(limits.keys())
    for agent, limit in limits.items():
        items = [s for s in sessions if s.get("agent") == agent]
        out.extend(_select(items, limit))

    # 2. Unlisted agents get ``default_limit`` (default 1) + a warning
    # (gated by ``verbose``). Sorted by agent name for stable order across runs.
    unlisted = sorted({s.get("agent") for s in sessions} - listed_agents)  # type: ignore
    for agent in unlisted:
        items = [s for s in sessions if s.get("agent") == agent]
        if verbose:
            eprint(f"warning: agent {agent!r} not in limits; keeping {default_limit} (default_limit)")
        out.extend(_select(items, default_limit))
    return out


def collect_overview(
    config: Config,
    *,
    recent_seconds: int | None = _OVERVIEW_RECENT_DEFAULT_SECONDS,
    show_all: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Build overview payload. Read-mostly: the only side effect is a
    ``watch_session`` call for the current PM's main agents (so the State
    column shows real idle/busy instead of "unknown"). All other
    session-watch restoration is owned by ``sidecar-service`` start/restart
    via ``rewatch_all_sessions``.

    ``recent_seconds`` filters sessions per ``(wt_id, pm_session_id, agent)``
    group (P0-1: per-PM isolation is part of the group key):
      - ``None`` or ``<0`` → no filter applied (legacy show-all behavior).
      - ``0`` → each group collapses to its newest item only.
      - ``>0`` → keep group if any item is within the window, else keep the
        single newest item as a "last-known tombstone".

    ``show_all=True`` overrides ``recent_seconds`` AND the per-agent session
    count limit (P1-1) — parity with the ``--all`` CLI flag.

    ``verbose`` (default False) is forwarded to ``_limit_per_agent`` to gate
    the unlisted-agent fallback warning. Wired from ``--verbose`` (overview
    subcommand).
    """
    # Fetch ALL sessions once (replaces ~10 per-wt HTTP calls with 1).
    all_sessions = sessions(config, limit=_OVERVIEW_SESSION_LIMIT)
    wts = collect_worktree_list(config.repo)
    rows: list[dict[str, Any]] = []
    for wt in wts:
        enrich_worktree_status(wt)
        sess_list = _filter_sessions_for_wt(all_sessions, wt["id"], wt["path"])
        # Main worktree: also pull PM sessions referenced by state files that
        # fall outside the global list window.  (With limit=2000 this should
        # be rare, but the state directory is the source of truth.)
        if wt["id"] == "xidi-minimal":
            seen_ids = {str(s.get("id") or "") for s in sess_list if s.get("id")}
            for pm_sid, _state_file, _is_current in recent_pm_state_files(config):
                if not pm_sid or pm_sid in seen_ids:
                    continue
                try:
                    pm_ses = http_json(
                        "GET",
                        f"{config.op_server}/session/{urllib.parse.quote(pm_sid)}",
                    )
                except SystemExit:
                    continue
                if not isinstance(pm_ses, dict):
                    continue
                sdir = pm_ses.get("directory", "")
                if not sdir or normalize_path(str(sdir)) != normalize_path(wt["path"]):
                    continue
                sess_list.append(pm_ses)
        # Normalize to (wt_id, agent, updated_ms) tuples for the filter, but
        # also keep the raw session dict so we can render after filtering.
        indexed: list[dict[str, Any]] = []
        for s in sess_list:
            meta = s.get("metadata") or {}
            wt_id_meta = meta.get("wt_id")
            wt_id = wt_id_meta if isinstance(wt_id_meta, str) and wt_id_meta else wt["id"]
            agent_raw = meta.get("agent") or s.get("agent")
            updated_ms = int((s.get("time") or {}).get("updated") or 0)
            indexed.append(
                {
                    "_raw": s,
                    "wt_id": wt_id,
                    "agent": normalize_agent_label(agent_raw),
                    "updated_ms": updated_ms,
                    "pm_session_id": "",  # main worktree fills below; others stay ""
                    "pm_current": False,
                }
            )
        # Main worktree: tag every item with its owning PM session BEFORE the
        # recent-window filter and per-agent limit run, so per-PM isolation is
        # honored at the (wt_id, pm_session_id, agent) grouping level. Filter
        # and limit then see the same PM buckets the renderer uses for display.
        if wt["id"] == "xidi-minimal":
            tag_pm_session_ownership(config, indexed)
        if show_all or recent_seconds is None or recent_seconds < 0:
            kept_indexed = indexed
        else:
            kept_indexed = _apply_recent_filter(
                indexed,
                now_ms=int(time.time() * 1000),
                recent_seconds=recent_seconds,
                pm_session_id="pm_session_id",  # per-PM 隔离贯穿 filter
            )
        # Main worktree: per-agent session count limit, scoped per-PM so two
        # PM sessions owning sessions of the same agent do not steal each
        # other's slots. --all overrides this so the user can see every
        # session regardless of count (parity with --all bypassing the
        # recent-window filter above).
        if wt["id"] == "xidi-minimal" and not show_all:
            kept_indexed = _limit_per_agent(
                kept_indexed,
                limits={"PM": 2, "General": 2, "Janitor": 2, "Momus": 2, "Clio": 2},
                pm_session_id="pm_session_id",  # per-PM 隔离贯穿 limit
                verbose=verbose,  # --verbose 控 unlisted-agent warning
            )
            # Cap PM groups to current + the newest historical PM states.
            # Tagged historical PM (pm_session_id != "") outranks orphan (pm_sid="")
            # so a tagged PM with older ``updated_ms`` is not displaced by a newer
            # orphan that ``_PM_STATE_HISTORY_LIMIT_DEFAULT`` could not tag.
            pm_items = [it for it in kept_indexed if _is_pm_agent(it.get("agent"))]
            pm_groups: dict[str, list[dict[str, Any]]] = {}
            for it in pm_items:
                gid = it.get("pm_session_id", "")
                pm_groups.setdefault(gid, []).append(it)
            pm_group_limit = _PM_STATE_HISTORY_LIMIT_DEFAULT + (1 if any(any(i.get("pm_current") for i in group) for group in pm_groups.values()) else 0)
            if len(pm_groups) > pm_group_limit:
                sorted_groups = sorted(
                    pm_groups.items(),
                    key=lambda kv: (
                        0 if any(i.get("pm_current") for i in kv[1]) else 1,
                        0 if kv[0] else 1,  # tagged PM outranks orphan (pm_sid != "")
                        -max(int(i.get("updated_ms", 0)) for i in kv[1]),
                    ),
                )
                drop_sids = {sid for sid, _ in sorted_groups[pm_group_limit:]}
                kept_indexed = [it for it in kept_indexed if it.get("pm_session_id", "") not in drop_sids]
            # Ensure all PM-owned main agents are watched by sidecar so State
            # column shows real idle/busy instead of "unwatch"
            for it in kept_indexed:
                if it.get("pm_session_id"):
                    try:
                        watch_session(config, it["_raw"].get("id", ""))
                    except SystemExit:
                        pass
        rows.append(
            {
                **wt,
                "sessions": [
                    {
                        **session_summary(it["_raw"]),
                        "context": fetch_session_context(config, it["_raw"].get("id", "-")),
                        "pm_session_id": it.get("pm_session_id", ""),
                        "pm_current": it.get("pm_current", False),
                    }
                    for it in kept_indexed
                ],
            }
        )
    # Fetch sidecar /status once for the State column; unwatched sessions
    # are reported as "unknown" (sidecar only tracks registered sessions).
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        status_map = {}
    if not isinstance(status_map, dict):
        status_map = {}
    return {
        "time": now_utc(),
        "health": {
            "opencode": op_healthy(config),
            "sidecar": sidecar_healthy(config),
        },
        "worktrees": rows,
        "sidecar_status_map": status_map,
    }


def fetch_last_reply(config: Config, session_id: str, limit: int = 50) -> str | None:
    """Fetch last assistant message text for a session; ``None`` on failure."""
    try:
        data = http_json(
            "GET",
            f"{config.op_server}/session/{urllib.parse.quote(session_id)}/message?limit={limit}",
        )
    except SystemExit:
        return None
    if not isinstance(data, list):
        return None
    msgs = [m for m in data if m.get("info", {}).get("role") == "assistant"]
    if not msgs:
        return None

    def msg_time(m: dict[str, Any]) -> int | float:
        t = m.get("info", {}).get("time", {})
        return t.get("completed") or t.get("created") or 0

    # Backtrack: find the most recent assistant message that has text content.
    # Tool-call-only messages (type="tool_call" parts but no type="text" parts)
    # are skipped so PM receives the agent's actual last textual output.
    for msg in sorted(msgs, key=msg_time, reverse=True):
        texts = [p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"]
        if texts:
            return "".join(texts)
    return None


def fetch_session_context(config: Config, session_id: str) -> int:
    """Return the context window tokens used in the session's latest LLM call.

    Reads the last messages (``GET /session/{id}/message?limit=10``) and
    extracts ``input + cache.read`` from the ``step-finish`` part of the
    most recent assistant message that actually finished a step. This is the
    actual token count that was sent to the model in the most recent
    completed LLM call (cached prefix hits included). Distinct from
    ``session.tokens`` which is cumulative across the whole session
    lifetime.

    The list is sorted by ``time.completed`` (falling back to
    ``time.created``) descending so the result is robust to whichever
    chronological order the op-server returns — same pattern as
    :func:`fetch_last_reply`.

    ``limit=10`` is intentional: a session currently in the middle of a tool
    call has no ``step-finish`` on its most recent message, but the previous
    completed LLM call (typically just seconds earlier) does. Returning 0 in
    that case would cause auto-compact to skip a session that genuinely
    exceeded the threshold. We walk back at most 10 messages to find the
    latest ``step-finish``; if none is found, we return 0 (keep the previous
    behavior of "no step-finish ⇒ no context signal").

    Returns ``0`` on fetch failure, no step-finish in the last 10 messages, or
    empty ``session_id``.
    """
    if not session_id or session_id == "-":
        return 0
    try:
        data = http_json(
            "GET",
            f"{config.op_server}/session/{urllib.parse.quote(session_id)}/message?limit=10",
        )
    except SystemExit:
        return 0
    if not isinstance(data, list) or not data:
        return 0

    def msg_time(m: dict[str, Any]) -> int | float:
        t = m.get("info", {}).get("time", {}) or {}
        return t.get("completed") or t.get("created") or 0

    for msg in sorted(data, key=msg_time, reverse=True):
        for part in msg.get("parts") or []:
            if part.get("type") == "step-finish":
                tk = part.get("tokens", {}) or {}
                cache = tk.get("cache", {}) or {}
                return int(tk.get("input", 0)) + int(cache.get("read", 0))
    return 0


@dataclass
class CompactResult:
    """Outcome of an auto-compact attempt.

    Attributes:
        compacted: True iff a /summarize call was issued AND the call returned
            a 2xx status. False covers both "context below threshold, no
            compact needed" and "compact attempted but failed".
        context_before: Context tokens at decision time (input + cache.read
            from the latest step-finish). 0 if the pre-check fetch failed.
        context_after: Always 0.  Callers fetch post-compact context themselves
            after :func:`auto_compact_session` returns — /summarize is synchronous
            so the value is immediately accurate.  This field is retained for
            dataclass shape compatibility only.
        error: Human-readable error string when ``compacted`` is False because
            the HTTP call failed or timed out. None otherwise.
    """

    compacted: bool
    context_before: int = 0
    context_after: int = 0
    error: str | None = None


_AUTO_COMPACT_PROVIDER_ID = "opencode"
_AUTO_COMPACT_MODEL_ID = "deepseek-v4-flash-free"


def auto_compact_session(
    config: Config,
    session_id: str,
    directory: str | None,
    *,
    threshold: int = 300_000,
    timeout: int = 60,
) -> CompactResult:
    """Compact a session's context if it exceeds ``threshold`` tokens.

    Workflow:

    1. Fetch current context via :func:`fetch_session_context`.
    2. If the value is 0 (fetch failed or no step-finish in the last 10
       messages) **or** below ``threshold``, return without compacting as a
       no-op — caller should fall through to its normal exit path. The 0
       case is a separate, explicit no-op (not a failure): we have no token
       count to evaluate, so the only safe action is to skip. See the
       inline comment above the ``context_before == 0`` check for why this
       is a primary guard rather than falling through to the
       ``<= threshold`` branch.
    3. Otherwise ``POST /session/{id}/summarize?directory=...`` with a fixed
       body ``{"providerID": "opencode", "modelID": "deepseek-v4-flash-free"}``
       and a ``timeout``-second HTTP budget. On 2xx, return
       ``CompactResult(compacted=True, context_before=<pre-context>, ...)``.
       On SystemExit (HTTP failure / timeout), capture the message and return
       it as ``error``.

    The provider/model is hardcoded intentionally: per the Phase-1 task spec,
    auto-compact is a fail-closed safety net for runaway context, not a
    user-tunable knob. Any new model choice must be reviewed and rolled out
    with the rest of the workflow configuration.

    ``directory`` is appended as a query string parameter (matching
    ``_idle_prompt_async`` and other op-server calls) so the summarize call
    targets the correct worktree the session was bound to.

    **Post-compact context is NOT fetched here.** ``/summarize`` is synchronous
    in the current op-server implementation — the 2xx return indicates the
    summary has been applied.  Callers can immediately ``fetch_session_context``
    to read the post-compact value.  The ``CompactResult.context_after`` field
    is left at ``0`` here because fetching it would duplicate the caller's
    concern.  All callers in this codebase independently fetch post-compact
    context after this function returns.
    """
    if not session_id or session_id == "-":
        return CompactResult(compacted=False, error="invalid session_id")
    context_before = fetch_session_context(config, session_id)
    # Explicit no-op for context=0 (fetch failure or no step-finish in the
    # last 10 messages). Both are observation failures: we have no token
    # count to compare against ``threshold``, so the only safe call is to
    # skip the compact. Treating this as a failure (with ``error=`` set)
    # would force the caller to send a [idle-notify:compact-failed] for a
    # transient sidecar / network blip, drowning the PM in noise; the
    # safety net here is a *missed* compact, not a *loud* one. This check
    # is the primary guard — the ``context_before <= threshold`` below
    # would also match for the value 0, but relying on that constant
    # coincidence is fragile (e.g. someone could later introduce a
    # non-positive sentinel other than 0). Keep the two checks separate
    # and explicit.
    if context_before == 0:
        return CompactResult(compacted=False, context_before=0)
    if context_before <= threshold:
        return CompactResult(compacted=False, context_before=context_before)
    body = {"providerID": _AUTO_COMPACT_PROVIDER_ID, "modelID": _AUTO_COMPACT_MODEL_ID}
    query_items: list[str] = []
    if directory:
        query_items.append("directory=" + urllib.parse.quote(directory, safe=""))
    query = "?" + "&".join(query_items) if query_items else ""
    url = f"{config.op_server}/session/{urllib.parse.quote(session_id)}/summarize{query}"
    try:
        http_json("POST", url, body, expected=(200, 201, 204), timeout=timeout)
    except SystemExit as exc:
        return CompactResult(
            compacted=False,
            context_before=context_before,
            error=f"{exc}",
        )
    return CompactResult(
        compacted=True,
        context_before=context_before,
        context_after=0,
    )


def _print_session_rows(
    wt: dict[str, Any],
    sessions: list[dict[str, Any]],
    status_map: dict[str, str],
    detail: bool,
    config: Config,
    *,
    show_unwatch: bool = False,
) -> None:
    """Print one worktree's session rows.

    ``wt['id'] == 'xidi-minimal'`` suppresses the WT column (PM session label
    already printed by the caller for grouped main-worktree sessions).

    ``show_unwatch`` (default False) hides sessions whose sidecar state is
    ``unwatch``. Pass ``--show-unwatch`` to include them.

    When ``show_unwatch=False`` filters every session out as ``unwatch`` /
    ``unknown``, emit a single placeholder row carrying the wt header
    (commit/dirty/Δmain) and a ``Session ID`` hint naming how many sessions
    are hidden behind ``--show-unwatch``. Without this fallback an
    all-unknown wt (e.g. freshly preallocated sessions that have never been
    dispatched, so ``tokens.input == 0`` and sidecar never sees an idle
    event) vanishes from the text overview even though ``--format json``
    confirms it owns N sessions. Main worktree skip: its header is rendered
    by the caller's PM group label, and per-group empty fallback is noisy.
    """
    is_main = wt["id"] == "xidi-minimal"
    raw_count = len(sessions)
    if not show_unwatch:
        sessions = [s for s in sessions if str(status_map.get(s.get("id", "-"), "unwatch")) not in ("unwatch", "unknown")]
    hidden = raw_count - len(sessions)
    if not sessions:
        if hidden and not is_main:
            prefix = f"  {wt['id']:<14} {wt['branch']:<40} {wt['commit']:<9} {wt['dirty']:<7} {wt['ahead_main']:<6}"
            note = f"({hidden} unwatch session{'s' if hidden != 1 else ''} hidden; pass --show-unwatch)"
            row = f"{prefix} {'(无)':<10} {'':<7} {'':<8} {'':<8} {'':<6} {'':<8} {'':<7} {'':<8} {note}"
            print(row)
        return
    first = True
    for sess in sessions:
        sid = sess.get("id", "-")
        sagent = sess.get("agent", "-")
        sin = fmt_tokens(sess.get("input", 0))
        sout = fmt_tokens(sess.get("out_reason", 0))
        scr = fmt_tokens(sess.get("cache_read", 0))
        inp = int(sess.get("input", 0))
        cr = int(sess.get("cache_read", 0))
        total_in = inp + cr
        hit_pct = f"{cr * 100 // total_in}%" if total_in > 0 else "—"
        sctx = fmt_tokens(sess.get("context", 0))
        supd = fmt_updated(sess.get("updated_ms", 0))
        sstate = str(status_map.get(sid, "unwatch"))
        if sstate == "unknown":
            sstate = "unwatch"
        # Flag stuck sessions (busy/streaming + no update > STALE_DISPATCH_MS)
        if sstate in ("busy", "streaming"):
            updated_ms = sess.get("updated_ms", 0)
            if updated_ms and (int(time.time() * 1000) - updated_ms) > STALE_DISPATCH_MS:
                sstate += " [STUCK]"
        # ANSI bold for busy/streaming rows so PM can spot active sessions
        # at a glance in the terminal table. Gated on isatty() so JSON
        # output (--format json) and piped consumers do not see raw escape
        # sequences corrupting their input. sstate.startswith handles the
        # [STUCK] suffix where the value is e.g. 'busy [STUCK]'.
        if sys.stdout.isatty() and (sstate.startswith("busy") or sstate.startswith("streaming")):
            raw = sstate
            sstate = f"\033[1m{raw}\033[0m"
            # ANSI escape codes inflate len() but don't occupy visible columns;
            # pad so the table column stays at 8 visible chars regardless of bold.
            sstate += " " * max(0, 8 - len(raw))
        if is_main:
            prefix = f"  {'':<14} {'':<40} {'':<9} {'':<7} {'':<6}"
        else:
            prefix = f"  {wt['id']:<14} {wt['branch']:<40} {wt['commit']:<9} {wt['dirty']:<7} {wt['ahead_main']:<6}" if first else f"  {'':<14} {'':<40} {'':<9} {'':<7} {'':<6}"
        first = False
        row = f"{prefix} {sagent:<10} {sin:<7} {sout:<8} {scr:<8} {hit_pct:<6} {sctx:<8} {supd:<7} {sstate:<8} {sid}"
        print(row)
        if detail and sid != "-":
            reply = fetch_last_reply(config, sid)
            if reply:
                print("    ── 最后回复 ──")
                for line in reply.splitlines():
                    print(f"    {line}")
                print()


def print_overview_text(payload: dict[str, Any], detail: bool, config: Config, *, show_orphan: bool = False, show_unwatch: bool = False) -> None:
    """Render overview payload as a fixed-width text table.

    ``show_orphan`` (default False) controls whether main-worktree sessions
    that do not belong to any PM session (no matching ``main.state`` entry,
    no current-PM self-group) are surfaced. Hidden by default to keep
    overview compact; pass ``--show-orphan`` to include the orphan group.
    """
    oh = payload["health"]["opencode"]
    sh = payload["health"]["sidecar"]
    print()
    print(f"══ Session Status — {payload['time']} ══")
    print()
    print("── 服务健康 ──")
    print(f"  OpenCode : {'healthy' if oh else 'down'} ({config.op_server})")
    print(f"  Sidecar  : {'healthy' if sh else 'down'}")
    print()
    print("── Worktree ──")
    status_map = payload.get("sidecar_status_map", {})
    header = (
        f"  {'WT':<14} {'Branch':<40} {'Commit':<9} {'Dirty':<7} {'Δmain':<6} "
        f"{'Agent':<10} {'Input':<7} {'Out+Rea':<8} {'Cache.R':<8} {'Hit%':<6} "
        f"{'Context':<8} {'Updated':<7} {'State':<8} {'Session ID'}"
    )
    sep = f"  {'-' * 14:<14} {'-' * 40:<40} {'-' * 9:<9} {'-' * 7:<7} {'-' * 6:<6} {'-' * 10:<10} {'-' * 7:<7} {'-' * 8:<8} {'-' * 8:<8} {'-' * 6:<6} {'-' * 8:<8} {'-' * 7:<7} {'-' * 8:<8} {'-' * 30}"
    print(header)
    print(sep)
    for wt in payload["worktrees"]:
        sessions = wt.get("sessions", [])
        if not sessions:
            print(f"  {wt['id']:<14} {wt['branch']:<40} {wt['commit']:<9} {wt['dirty']:<7} {wt['ahead_main']:<6} (无)")
            continue
        is_main = wt["id"] == "xidi-minimal"
        # Group main-worktree sessions by PM session for clean grouping
        if is_main:
            groups: dict[str, list[dict[str, Any]]] = {}
            group_order: list[str] = []
            for sess in sessions:
                pm_sid = sess.get("pm_session_id", "") or "orphan"
                if pm_sid not in groups:
                    groups[pm_sid] = []
                    group_order.append(pm_sid)
                groups[pm_sid].append(sess)
            # Sort each group: PM first, then agents alphabetically
            for g in group_order:
                groups[g].sort(key=lambda s: (0 if _is_pm_agent(s.get("agent")) else 1, s.get("agent", "")))
            # Print current PM session first, then others
            current_first: list[str] = []
            others: list[str] = []
            for g in group_order:
                if any(sess.get("pm_current") for sess in groups[g]):
                    current_first.append(g)
                else:
                    others.append(g)
            group_order = current_first + others
            first_wt = True
            for grp in group_order:
                if not show_orphan and grp == "orphan":
                    continue
                grp_sessions = groups[grp]
                # Group header
                if grp == "orphan":
                    label = "── 未归属（orphan）──"
                elif any(sess.get("pm_current") for sess in grp_sessions):
                    label = f"── PM {grp}（当前）──"
                else:
                    label = f"── PM {grp} ──"
                if not first_wt:
                    print()
                print(f"  {label}")
                first_wt = False
                _print_session_rows(wt, grp_sessions, status_map, detail, config, show_unwatch=show_unwatch)
        else:
            _print_session_rows(wt, sessions, status_map, detail, config, show_unwatch=show_unwatch)
    print()


def cmd_overview(args: argparse.Namespace, config: Config) -> None:
    """Show project-wide / single-wt / single-session overview.

    Replaces the legacy worktree_session_status.py.
    """
    if args.session and (args.wt or getattr(args, "main", False)):
        fail("--session is mutually exclusive with --wt/--main")
    if args.wt and getattr(args, "main", False):
        fail("--wt and --main are mutually exclusive")
    if args.session:
        cmd_overview_session(args, config)
        return
    if getattr(args, "main", False):
        args.wt = config.repo.name
    if args.wt:
        # Accept --wt 10 as shorthand for --wt wt_10
        if args.wt.isdigit():
            args.wt = f"wt_{args.wt}"
        cmd_overview_wt(args, config)
        return
    watch_mode = bool(getattr(args, "watch", False))
    interval = float(getattr(args, "interval", 5.0))
    if watch_mode and interval <= 0:
        fail("--interval must be > 0")

    # Reconciling --all and --recent: --all disables recent-* seconds filtering
    show_all = bool(getattr(args, "all", False))
    if show_all:
        recent_seconds: int | None = None
        args.show_unwatch = True  # --all implies --show-unwatch
    else:
        recent_arg = getattr(args, "recent", None)
        if recent_arg is None:
            recent_seconds = _OVERVIEW_RECENT_DEFAULT_SECONDS
        else:
            recent_seconds = _parse_duration(recent_arg)

    while True:
        try:
            payload = collect_overview(
                config,
                recent_seconds=recent_seconds,
                show_all=show_all,
                verbose=bool(getattr(args, "verbose", False)),
            )
        except KeyboardInterrupt:
            print()
            return
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if not watch_mode:
                return
            time.sleep(interval)
            continue
        if watch_mode and sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
        show_orphan = bool(getattr(args, "show_orphan", False))
        show_unwatch = bool(getattr(args, "show_unwatch", False))
        print_overview_text(payload, args.detail, config, show_orphan=show_orphan, show_unwatch=show_unwatch)
        if not watch_mode:
            return
        sys.stdout.flush()
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print()
            return


def cmd_overview_session(args: argparse.Namespace, config: Config) -> None:
    """Show a single session's full state — any session, not limited to worktree.

    Useful for: main worktree's PM session, orphaned sessions, subagent sessions.
    """
    ses_id = args.session
    data = http_json(
        "GET",
        f"{config.op_server}/session/{urllib.parse.quote(ses_id)}",
    )
    if not isinstance(data, dict):
        fail(f"unexpected /session response: {type(data).__name__}")
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    print(f"══ Session {ses_id} ══")
    print()
    print(f"  Title      : {data.get('title', '-')}")
    print(f"  Agent      : {data.get('agent', '-')}")
    model = data.get("model") or {}
    print(f"  Model      : {model.get('id', '-')} ({model.get('providerID', '-')}{':' + model.get('variant', '-') if model.get('variant') else ''})")
    print(f"  Directory  : {data.get('directory', '-')}")
    parent = data.get("parentID")
    print(f"  Parent     : {parent or '-'}")

    # Sidecar state (idle / busy / unknown)
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
        if isinstance(status_map, dict):
            state = str(status_map.get(ses_id, "unknown"))
        else:
            state = "unknown"
    except SystemExit:
        state = "unknown"
    print(f"  State      : {state}")
    time_obj = data.get("time") or {}
    if time_obj.get("created"):
        from datetime import UTC, datetime

        created = datetime.fromtimestamp(time_obj["created"] / 1000, tz=UTC).isoformat()
        print(f"  Created    : {created}")
    if time_obj.get("updated"):
        from datetime import UTC, datetime

        updated = datetime.fromtimestamp(time_obj["updated"] / 1000, tz=UTC).isoformat()
        print(f"  Updated    : {updated}")

    tk = data.get("tokens") or {}
    cache = tk.get("cache") or {}
    inp = tk.get("input", 0)
    cread = cache.get("read", 0)
    cwrite = cache.get("write", 0)
    print()
    print("  Tokens (cumulative):")
    print(f"    Input     : {fmt_tokens(inp)}")
    print(f"    Out+Rea   : {fmt_tokens(tk.get('output', 0) + tk.get('reasoning', 0))}")
    print(f"    Cache.R   : {fmt_tokens(cread)}")
    print(f"    Cache.W   : {fmt_tokens(cwrite)}")
    cost = data.get("cost")
    if cost is not None:
        print(f"    Cost      : ${cost:.4f}")

    # Cache hit rate: cumulative cache.read / (input + cache.read)
    # = fraction of total LLM-bound input that came from cache (i.e. didn't need recompute)
    total_in = inp + cread
    if total_in > 0:
        hit_pct = (cread / total_in) * 100
        print()
        print("  Cache efficiency:")
        print(f"    Hit rate  : {hit_pct:5.1f}%  ({fmt_tokens(cread)} cached / {fmt_tokens(total_in)} total input)")

    print()
    ctx = fetch_session_context(config, ses_id)
    print(f"  Context (current LLM window): {fmt_tokens(ctx)}")

    meta = data.get("metadata") or {}
    if meta:
        print()
        print("  Metadata:")
        for k, v in sorted(meta.items()):
            print(f"    {k:<11}: {v}")

    if args.detail:
        reply = fetch_last_reply(config, ses_id)
        if reply:
            print()
            print("  ── Last reply ──")
            for line in reply.splitlines():
                print(f"  {line}")


def cmd_overview_wt(args: argparse.Namespace, config: Config) -> None:
    """Show one worktree's session pool only.

    For the main worktree, parity with ``collect_overview`` is enforced:
    recent-window filter, per-agent session count limit, and PM-session
    grouping all run on the same indexed pipeline so the rendered table
    matches what ``overview`` would show for that single worktree. For
    pool worktrees (wt_N) only the recent-window filter applies — no PM
    grouping, no per-agent limit (those are main-worktree concepts only).
    """
    wt_id = args.wt
    wts = collect_worktree_list(config.repo)
    target = next((w for w in wts if w["id"] == wt_id), None)
    if target is None:
        known = ", ".join(w["id"] for w in wts) or "(none)"
        fail(f"worktree not found: {wt_id} (known: {known})")
    enrich_worktree_status(target)
    sess_list = collect_wt_sessions(config, target["id"], target["path"])

    # Resolve --all / --recent the same way cmd_overview does, so callers
    # see consistent behavior across the two entry points.
    show_all = bool(getattr(args, "all", False))
    if show_all:
        recent_seconds: int | None = None
        args.show_unwatch = True  # --all implies --show-unwatch
    else:
        recent_arg = getattr(args, "recent", None)
        if recent_arg is None:
            recent_seconds = _OVERVIEW_RECENT_DEFAULT_SECONDS
        else:
            recent_seconds = _parse_duration(recent_arg)

    # Normalize to (wt_id, agent, updated_ms) tuples for the filter, but
    # also keep the raw session dict so we can render after filtering.
    indexed: list[dict[str, Any]] = []
    for s in sess_list:
        meta = s.get("metadata") or {}
        wt_id_meta = meta.get("wt_id")
        item_wt_id = wt_id_meta if isinstance(wt_id_meta, str) and wt_id_meta else target["id"]
        agent_raw = meta.get("agent") or s.get("agent")
        updated_ms = int((s.get("time") or {}).get("updated") or 0)
        indexed.append(
            {
                "_raw": s,
                "wt_id": item_wt_id,
                "agent": normalize_agent_label(agent_raw),
                "updated_ms": updated_ms,
                "pm_session_id": "",
                "pm_current": False,
            }
        )

    # Main worktree: tag every item with its owning PM session BEFORE the
    # recent-window filter and per-agent limit run, so per-PM isolation is
    # honored at the (wt_id, pm_session_id, agent) grouping level.
    if target["id"] == "xidi-minimal":
        tag_pm_session_ownership(config, indexed)

    if show_all or recent_seconds is None or recent_seconds < 0:
        kept_indexed = indexed
    else:
        kept_indexed = _apply_recent_filter(
            indexed,
            now_ms=int(time.time() * 1000),
            recent_seconds=recent_seconds,
            pm_session_id="pm_session_id",
        )

    # Main worktree: per-agent session count limit, scoped per-PM.
    # --all overrides this so the user sees every session regardless of count.
    if target["id"] == "xidi-minimal" and not show_all:
        kept_indexed = _limit_per_agent(
            kept_indexed,
            limits={"PM": 2, "General": 2, "Janitor": 2, "Momus": 2, "Clio": 2},
            pm_session_id="pm_session_id",
            verbose=bool(getattr(args, "verbose", False)),
        )
        # Cap PM groups to current + the newest historical PM states.
        # Tagged historical PM (pm_session_id != "") outranks orphan (pm_sid="")
        # so a tagged PM with older ``updated_ms`` is not displaced by a newer
        # orphan that ``_PM_STATE_HISTORY_LIMIT_DEFAULT`` could not tag.
        pm_items = [it for it in kept_indexed if _is_pm_agent(it.get("agent"))]
        pm_groups: dict[str, list[dict[str, Any]]] = {}
        for it in pm_items:
            gid = it.get("pm_session_id", "")
            pm_groups.setdefault(gid, []).append(it)
        pm_group_limit = _PM_STATE_HISTORY_LIMIT_DEFAULT + (1 if any(any(i.get("pm_current") for i in group) for group in pm_groups.values()) else 0)
        if len(pm_groups) > pm_group_limit:
            sorted_groups = sorted(
                pm_groups.items(),
                key=lambda kv: (
                    0 if any(i.get("pm_current") for i in kv[1]) else 1,
                    0 if kv[0] else 1,  # tagged PM outranks orphan (pm_sid != "")
                    -max(int(i.get("updated_ms", 0)) for i in kv[1]),
                ),
            )
            drop_sids = {sid for sid, _ in sorted_groups[pm_group_limit:]}
            kept_indexed = [it for it in kept_indexed if it.get("pm_session_id", "") not in drop_sids]
        # Ensure all PM-owned main agents are watched by sidecar so State
        # column shows real idle/busy instead of "unwatch"
        for it in kept_indexed:
            if it.get("pm_session_id"):
                try:
                    watch_session(config, it["_raw"].get("id", ""))
                except SystemExit:
                    pass

    row = {
        **target,
        "sessions": [
            {
                **session_summary(it["_raw"]),
                "context": fetch_session_context(config, it["_raw"].get("id", "-")),
                "pm_session_id": it.get("pm_session_id", ""),
                "pm_current": it.get("pm_current", False),
            }
            for it in kept_indexed
        ],
    }
    if args.format == "json":
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return
    # Fetch sidecar /status once for the State column (parity with cmd_overview).
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        status_map = {}
    if not isinstance(status_map, dict):
        status_map = {}
    payload = {
        "time": now_utc(),
        "health": {
            "opencode": op_healthy(config),
            "sidecar": sidecar_healthy(config),
        },
        "worktrees": [row],
        "sidecar_status_map": status_map,
    }
    show_orphan = bool(getattr(args, "show_orphan", False))
    show_unwatch = bool(getattr(args, "show_unwatch", False))
    print_overview_text(payload, args.detail, config, show_orphan=show_orphan, show_unwatch=show_unwatch)


# ---------------- idle-watch ----------------


def _idle_safe_name(value: str) -> str:
    """Sanitize session ID for use in pid/log filenames.
    Keep alnum + -_. ; replace others with _.
    """
    chars: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in "-_.":
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars)


def _idle_pidfile(config: Config, session: str) -> Path:
    return pid_file(config, f"watch-session-idle-{_idle_safe_name(session)}")


def _idle_logfile(config: Config, session: str) -> Path:
    return log_file(config, f"watch-session-idle-{_idle_safe_name(session)}")


def _idle_validate_ses(name: str, value: str) -> str:
    """Validate session ID is non-empty and starts with 'ses'."""
    if not value:
        fail(f"{name} must be non-empty")
    if not value.startswith("ses"):
        fail(f"{name} must start with 'ses': {value}")
    return value


def _idle_fetch_status(config: Config, session: str) -> str:
    """GET sidecar /status and return state for `session`.

    Best-effort: if target is missing from the map, register via
    watch_session() and re-fetch. Returns 'unknown' on any HTTP error.
    """
    try:
        payload = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    state = payload.get(session)
    if state is None:
        try:
            watch_session(config, session)
            payload = http_json("GET", f"{config.sidecar}/status")
        except SystemExit:
            return "unknown"
        if not isinstance(payload, dict):
            return "unknown"
        state = payload.get(session)
    return state if isinstance(state, str) else "unknown"


def _idle_prompt_async(
    config: Config,
    notify_session: str,
    message: str,
    *,
    directory: str | None = None,
    workspace: str | None = None,
    timeout: float | None = None,
) -> bool:
    """POST prompt_async to op-server for notify_session. Return True on HTTP 204."""
    query_items: list[str] = []
    if directory:
        query_items.append("directory=" + urllib.parse.quote(directory, safe=""))
    if workspace:
        query_items.append("workspace=" + urllib.parse.quote(workspace, safe=""))
    query = "?" + "&".join(query_items) if query_items else ""
    url = f"{config.op_server}/session/{notify_session}/prompt_async{query}"
    body = {"parts": [{"type": "text", "text": message}]}
    effective_timeout = int(timeout) if timeout is not None else config.http_timeout
    try:
        http_json(
            "POST",
            url,
            body,
            expected=(204,),
            timeout=effective_timeout,
        )
        return True
    except SystemExit:
        return False


def _session_has_assistant_reply_after(config: Config, session_id: str, started_at_ms: int, limit: int = 50) -> bool:
    """Return True when the session has an assistant reply with text after ``started_at_ms``.

    This is used only by dispatch-spawned idle-watch as a race-condition
    fallback: if a task finishes before the watcher ever observes
    busy/streaming, the watcher can still notify exactly once after it sees
    that the target session produced a new assistant message.

    Requires at least one text part with non-whitespace content (tool-call-only
    messages are excluded to avoid false-positive idle-after-update notifies).
    """
    if started_at_ms <= 0:
        return False
    try:
        data = http_json(
            "GET",
            f"{config.op_server}/session/{urllib.parse.quote(session_id)}/message?limit={limit}",
        )
    except SystemExit:
        return False
    if not isinstance(data, list):
        return False
    for msg in data:
        if msg.get("info", {}).get("role") != "assistant":
            continue
        t = msg.get("info", {}).get("time", {}) or {}
        msg_ms = int(t.get("completed") or t.get("created") or 0)
        if msg_ms < started_at_ms:
            continue
        parts = msg.get("parts", [])
        if any(p.get("type") == "text" and p.get("text", "").strip() for p in parts):
            return True
    return False


def _build_idle_notify_message(
    config: Config,
    target: str,
    notify_reason: str,
    custom_message: str | None,
) -> str:
    """Compose the prompt_async body sent to the notify session.

    Two modes:

    - ``custom_message`` is set (user passed ``--message``): that exact text is
      sent verbatim. The caller has full control; no tag is added (the watcher
      shouldn't second-guess an explicit user override).
    - Default mode: prefix ``[idle-notify]`` so the receiving session can grep
      auto-notifies, then include the target session's last assistant reply at
      the moment of the busy→idle transition. The tag suffix encodes *why* the
      notify fired (busy→idle edge vs. initial-idle tick) so the receiver can
      tell them apart without cross-referencing logs.

      The last-reply fetch is best-effort: a session that just transitioned to
      idle but hasn't produced a final assistant message (e.g. failed tasks)
      yields a stub that still carries the tag + target id, so the receiver
      knows the watcher fired but content is unavailable.
    """
    if custom_message is not None:
        return custom_message

    if "busy -> idle" in notify_reason:
        tag = "[idle-notify:busy->idle]"
    elif "idle after dispatch update" in notify_reason:
        tag = "[idle-notify:idle-after-update]"
    elif "initial" in notify_reason:
        tag = "[idle-notify:initial-idle]"
    else:
        tag = "[idle-notify]"

    last_reply = fetch_last_reply(config, target)
    if last_reply is None:
        return f"{tag} target={target} (no assistant message found)"
    return f"{tag} target={target}\n\nLast assistant message:\n\n{last_reply}\n"


_IDLE_WATCH_INTERVAL_DEFAULT = 2.0
_IDLE_WATCH_TIMEOUT_DEFAULT = 10.0
_IDLE_WATCH_MAX_ERRORS_DEFAULT = 10
_IDLE_WATCH_STOP_TIMEOUT_DEFAULT = 3.0

# Auto-compact threshold: when a session's most recent step-finish reports
# ``input + cache.read`` above this number, the idle-watch fires a
# ``POST /session/{id}/summarize`` after the busy->idle notify and then sends
# a follow-up notify tagged ``[idle-notify:compact-done|compact-failed]``.
# Hardcoded per the Phase-1 spec — no CLI flag, since the intent is a safety
# net, not a per-task knob.
_AUTO_COMPACT_THRESHOLD = 180_000
_AUTO_COMPACT_HTTP_TIMEOUT = 60


def _spawn_dispatch_idle_watch(
    config: Config,
    target_sid: str,
    notify_sid: str,
    wt_path: Path,
    *,
    wt_id: str = "",
    agent: str = "",
    max_poll_seconds: int = 0,
    started_at_ms: int = 0,
) -> None:
    """Spawn a one-shot idle-watch for the dispatched session.

    Monitors sidecar /status for busy→idle transition on ``target_sid``, then
    sends a prompt_async to ``notify_sid`` (typically the PM session) and exits.

    The watcher runs as a detached background process; its pid/log are tracked
    alongside other idle-watch instances via ``_idle_pidfile`` / ``_idle_logfile``.

    ``max_poll_seconds`` (default 0 = unlimited) is forwarded to ``idle-watch`` as
    ``--max-poll-seconds``. Pass a positive value for tasks that may take longer
    than the previous hardcoded 600s budget (e.g. Daedalus 30min → 1800).

    ``started_at_ms`` enables the dispatch-aware idle-after-update fallback for
    very short tasks that complete before the watcher observes busy/streaming.
    """
    pf = _idle_pidfile(config, target_sid)
    lf = _idle_logfile(config, target_sid)

    old_pid = read_pid(pf)
    if pid_alive(old_pid):
        eprint(f"[dispatch] idle-watch already running for {target_sid} (pid {old_pid}); skip spawn")
        return

    if pf.exists():
        pf.unlink(missing_ok=True)
    lf.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "idle-watch",
        "--session",
        target_sid,
        "--notify-session",
        notify_sid,
        "--directory",
        str(wt_path),
    ]
    if wt_id:
        cmd.extend(["--wt-id", wt_id])
    if agent:
        cmd.extend(["--agent", agent])
    if max_poll_seconds > 0:
        # ``--max-poll-seconds`` is ``type=float`` end-to-end (parent
        # dispatch and child idle-watch both share ``_add_dispatch_options``
        # / ``_add_watch_common``), so the float is passed through without
        # rounding.
        cmd.extend(["--max-poll-seconds", str(max_poll_seconds)])
    if started_at_ms > 0:
        cmd.extend(["--started-at-ms", str(int(started_at_ms))])
        cmd.append("--notify-if-idle-after-update")
    log_fd = open(lf, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(config.repo),
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log_fd.close()

    _write_pid_file(pf, proc.pid)
    eprint(f"[dispatch] auto idle-watch spawned: target={target_sid} notify={notify_sid} pid={proc.pid}")


def cmd_idle_watch(args: argparse.Namespace, config: Config) -> None:
    """Foreground: poll sidecar /status; send prompt_async on busy->idle edge.

    Without --continuous, exit after first notify. With --continuous, loop
    forever. --notify-if-initial-idle also sends a prompt if first tick is
    already 'idle'.
    """
    target = _idle_validate_ses("--session", args.session)
    notify = _idle_validate_ses("--notify-session", args.notify_session)

    interval = args.interval
    timeout = args.timeout
    max_errors = args.max_errors
    continuous = bool(getattr(args, "continuous", False))
    initial_idle_notify = bool(getattr(args, "notify_if_initial_idle", False))
    idle_after_update_notify = bool(getattr(args, "notify_if_idle_after_update", False))
    started_at_ms = int(getattr(args, "started_at_ms", 0) or 0)

    if interval <= 0:
        fail("--interval must be > 0")
    if timeout <= 0:
        fail("--timeout must be > 0")
    if max_errors <= 0:
        fail("--max-errors must be > 0")
    if started_at_ms < 0:
        fail("--started-at-ms must be >= 0")
    if idle_after_update_notify and started_at_ms <= 0:
        fail("--notify-if-idle-after-update requires --started-at-ms")

    max_poll_seconds = getattr(args, "max_poll_seconds", 0) or 0
    deadline: float | None = None
    if max_poll_seconds > 0:
        deadline = time.monotonic() + max_poll_seconds

    # If the user passed --message explicitly, that wins. Otherwise the watcher
    # auto-builds a tagged notify containing the target session's last assistant
    # reply at the moment of the transition (much more useful for the receiving
    # session than the old generic "check session X last message" placeholder).
    custom_message = args.message if args.message else None

    eprint(f"[idle-watch] target={target} notify={notify} interval={interval} custom_message={custom_message!r}")

    previous_status: str | None = None
    consecutive_errors = 0
    saw_busy_or_streaming = False
    idle_after_update_notified = False
    idle_after_update_candidate = False  # secondary-confirmation: first-hit not yet confirmed
    busy_since: float | None = None  # monotonic timestamp when busy/streaming started
    stuck_notified = False  # only fire stuck notification once per busy streak

    # Auto-compact state:
    # When a busy→idle edge fires in one-shot mode, check context and compact
    # if above threshold.  Compact is a fire-and-forget /summarize call —
    # no follow-up ping, no phase tracking.  The post-compact context is
    # immediately readable (the session is idle and /summarize is synchronous).
    compact_before = 0

    while True:
        try:
            if deadline is not None and time.monotonic() > deadline:
                eprint(f"[idle-watch] max poll seconds ({max_poll_seconds}s) reached; exiting")
                return
            current_status = _idle_fetch_status(config, target)

            if current_status == "unknown" and previous_status is None and not initial_idle_notify:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    fail(f"sidecar /status returned 'unknown' for {target} after {max_errors} consecutive errors; pass --notify-if-initial-idle to send on first tick")
                time.sleep(interval)
                continue
            # P1 fix: only reset the error counter on a known status. Letting the
            # reset fire for post-baseline 'unknown' would clobber consecutive_errors
            # every tick and prevent the post-baseline handler below from
            # accumulating toward max_errors.
            if current_status != "unknown":
                consecutive_errors = 0
            # P1 fix: post-baseline 'unknown' must NOT clobber previous_status
            # (e.g. "busy"); otherwise a busy->idle transition that happens between
            # the transient sidecar error and the next successful poll is masked
            # (transition would be observed as unknown->idle, not busy->idle).
            if current_status == "unknown" and previous_status is not None:
                consecutive_errors += 1
                eprint(f"[idle-watch] sidecar /status returned 'unknown' for {target} [{consecutive_errors}/{max_errors}]; keeping previous_status={previous_status!r}")
                if consecutive_errors >= max_errors:
                    fail(f"sidecar /status returned 'unknown' for {target} after {max_errors} consecutive errors; aborting to avoid masking busy->idle transition")
                time.sleep(interval)
                continue

            if current_status in ("busy", "streaming"):
                saw_busy_or_streaming = True
                if busy_since is None:
                    busy_since = time.monotonic()
                if not stuck_notified:
                    # Use session's own time.updated (ms epoch), not watcher's
                    # busy_since. A session actively producing output refreshes
                    # its updated timestamp; only truly stalled sessions go stale.
                    ses_data = get_session_by_id(config, target)
                    if ses_data:
                        updated_ms = int((ses_data.get("time") or {}).get("updated", 0))
                        if updated_ms > 0 and (time.time() * 1000 - updated_ms) > STALE_DISPATCH_MS:
                            stuck_notified = True
                            ctx_parts = [f"target={target}"]
                            wt_id = getattr(args, "wt_id", "")
                            agent = getattr(args, "agent", "")
                            if wt_id:
                                ctx_parts.append(f"wt={wt_id}")
                            if agent:
                                ctx_parts.append(f"agent={agent}")
                            ctx_parts.append(f"stale>{STALE_DISPATCH_MS // 60000}min")
                            ctx_parts.append(f"(last updated {time.time() * 1000 - updated_ms:.0f}ms ago)")
                            msg = f"[stuck-notify] {' '.join(ctx_parts)}"
                            eprint(msg)
                            _idle_prompt_async(config, notify, msg, directory=getattr(args, "directory", None), workspace=getattr(args, "workspace", None), timeout=timeout)
            else:
                # status changed away from busy/streaming — reset stuck tracking
                busy_since = None
                stuck_notified = False

            should_notify = False
            notify_reason = ""

            if previous_status is None:
                eprint(f"[idle-watch] initial status: {current_status}")
                if initial_idle_notify and current_status == "idle":
                    should_notify = True
                    notify_reason = "initial idle (notify-if-initial-idle)"
            elif current_status != previous_status:
                eprint(f"[idle-watch] status changed: {previous_status} -> {current_status}")
                if previous_status in ("busy", "streaming") and current_status == "idle":
                    should_notify = True
                    notify_reason = "busy -> idle"

            if (
                not should_notify
                and idle_after_update_notify
                and not saw_busy_or_streaming
                and not idle_after_update_notified
                and current_status == "idle"
                and _session_has_assistant_reply_after(config, target, started_at_ms)
            ):
                if not idle_after_update_candidate:
                    # First hit — record candidate and wait one tick for secondary
                    # confirmation.  Filters out false positives from sidecar event
                    # lag (session briefly reports idle between tool calls while
                    # still active) and tool-call-only assistant messages.
                    idle_after_update_candidate = True
                    eprint("[idle-watch] idle-after-update candidate (first hit); waiting one tick for confirmation")
                    previous_status = current_status
                    time.sleep(interval)
                    continue
                # Second consecutive hit — confirmed.
                should_notify = True
                notify_reason = "idle after dispatch update"

            # Reset candidate if session is no longer idle (agent may still be working)
            if current_status != "idle":
                idle_after_update_candidate = False

            if should_notify:
                eprint(f"[idle-watch] detected {notify_reason}, sending prompt_async to {notify}")
                message_to_send = _build_idle_notify_message(config, target, notify_reason, custom_message)
                ok = _idle_prompt_async(
                    config,
                    notify,
                    message_to_send,
                    directory=getattr(args, "directory", None),
                    workspace=getattr(args, "workspace", None),
                    timeout=timeout,
                )
                if not ok:
                    eprint("[idle-watch] prompt_async failed; will retry next tick")
                else:
                    eprint("[idle-watch] async prompt accepted (204)")
                    if notify_reason == "idle after dispatch update":
                        idle_after_update_notified = True
                    if not continuous:
                        if notify_reason == "busy -> idle":
                            # Phase 0 first-notify accepted; now try to compact
                            # the target's context via /summarize (synchronous —
                            # post-compact context is immediately readable).
                            # Below-threshold / fetch-failed → silent one-shot
                            # exit.  HTTP failure → compact-failed notify.
                            eprint(f"[idle-watch] auto-compact check: target={target} threshold={_AUTO_COMPACT_THRESHOLD}")
                            result = auto_compact_session(
                                config,
                                target,
                                getattr(args, "directory", None),
                                threshold=_AUTO_COMPACT_THRESHOLD,
                                timeout=_AUTO_COMPACT_HTTP_TIMEOUT,
                            )
                            if not result.compacted:
                                if result.error is None:
                                    eprint(f"[idle-watch] auto-compact not needed (context={result.context_before}); exiting (one-shot)")
                                else:
                                    eprint(f"[idle-watch] auto-compact failed: {result.error}")
                                    fail_msg = f"[idle-notify:compact-failed] target={target} error={result.error}"
                                    _idle_prompt_async(
                                        config,
                                        notify,
                                        fail_msg,
                                        directory=getattr(args, "directory", None),
                                        workspace=getattr(args, "workspace", None),
                                        timeout=timeout,
                                    )
                                return
                            compact_before = result.context_before
                            eprint(f"[idle-watch] compact ok: pre-context={compact_before // 1000}K; sending compact-done")
                            msg = f"[idle-notify:compact-done] target={target}"
                            _idle_prompt_async(
                                config,
                                notify,
                                msg,
                                directory=getattr(args, "directory", None),
                                workspace=getattr(args, "workspace", None),
                                timeout=timeout,
                            )
                            return
                        return

            previous_status = current_status
            time.sleep(interval)
        except Exception:
            eprint("[idle-watch] unexpected error, aborting", exc_info=True)  # type: ignore
            return


def cmd_idle_watch_start(args: argparse.Namespace, config: Config) -> None:
    """Background: spawn idle-watch as a detached process with pid+log file."""
    target = _idle_validate_ses("--session", args.session)
    notify = _idle_validate_ses("--notify-session", args.notify_session)

    pf = _idle_pidfile(config, target)
    lf = _idle_logfile(config, target)

    old_pid = read_pid(pf)
    if old_pid is not None and pid_alive(old_pid):
        eprint(f"[idle-watch] {target}: watcher already running (pid {old_pid}, {pf})")
        raise SystemExit(1)

    if pf.exists():
        pf.unlink(missing_ok=True)

    lf.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "idle-watch",
        "--session",
        target,
        "--notify-session",
        notify,
        "--interval",
        str(args.interval),
        "--timeout",
        str(args.timeout),
        "--max-errors",
        str(args.max_errors),
    ]
    # Only forward --message when the caller explicitly set it. If omitted, the
    # child's auto-build path (fetch_last_reply + [idle-notify] tag) takes over.
    if args.message:
        cmd.extend(["--message", args.message])
    if getattr(args, "max_poll_seconds", 0):
        cmd.extend(["--max-poll-seconds", str(args.max_poll_seconds)])
    if getattr(args, "directory", None):
        cmd += ["--directory", args.directory]
    if getattr(args, "workspace", None):
        cmd += ["--workspace", args.workspace]
    if getattr(args, "continuous", False):
        cmd.append("--continuous")
    if getattr(args, "started_at_ms", 0):
        cmd.extend(["--started-at-ms", str(int(args.started_at_ms))])
    if getattr(args, "notify_if_initial_idle", False):
        cmd.append("--notify-if-initial-idle")
    if getattr(args, "notify_if_idle_after_update", False):
        cmd.append("--notify-if-idle-after-update")

    log_fd = open(lf, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(config.repo),
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log_fd.close()

    _write_pid_file(pf, proc.pid)

    eprint(f"[idle-watch] started target={target} notify={notify} pid={proc.pid} log={lf}")


def cmd_idle_watch_stop(args: argparse.Namespace, config: Config) -> None:
    """Stop background watcher: SIGTERM, escalate to SIGKILL on --force."""
    target = _idle_validate_ses("--session", args.session)
    pf = _idle_pidfile(config, target)
    pid = read_pid(pf)

    if pid is None:
        eprint(f"[idle-watch] {target}: not running (no pid file at {pf})")
        return

    if not pid_alive(pid):
        eprint(f"[idle-watch] {target}: stale pid file (pid {pid} not alive); removing")
        pf.unlink(missing_ok=True)
        return

    eprint(f"[idle-watch] {target}: sending SIGTERM to pid {pid}")
    # Signal only the recorded PID, not the process group. ``start_new_session=True``
    # in the spawner already isolates the child into its own session/group, so
    # killpg(pid, ...) was an unneeded group-wide broadcast — and a footgun:
    # if the recorded PID were ever recycled by the OS before we reached this
    # line, killpg would have signalled whatever unrelated group now owns it.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        eprint(f"[idle-watch] {target}: pid {pid} already exited before SIGTERM")
    except PermissionError as exc:
        fail(f"cannot SIGTERM pid {pid} (permission denied): {exc}")

    stop_timeout = args.stop_timeout
    deadline = time.monotonic() + stop_timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            eprint(f"[idle-watch] {target}: stopped (pid {pid})")
            pf.unlink(missing_ok=True)
            return
        time.sleep(0.1)

    if args.force:
        eprint(f"[idle-watch] {target}: SIGTERM timeout; sending SIGKILL to pid {pid}")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            eprint(f"[idle-watch] {target}: pid {pid} already exited before SIGKILL")
        except PermissionError as exc:
            fail(f"cannot SIGKILL pid {pid} (permission denied): {exc}")
        time.sleep(0.1)
        if pid_alive(pid):
            fail(f"failed to kill pid {pid} even with SIGKILL")
        eprint(f"[idle-watch] {target}: killed (pid {pid})")
        pf.unlink(missing_ok=True)
        return

    fail(f"timed out after {stop_timeout}s waiting for pid {pid} to exit; pass --force to SIGKILL")


def cmd_idle_watch_status(args: argparse.Namespace, config: Config) -> None:
    """Check background watcher state. Exit 0 running, 1 no pid, 2 stale."""
    target = _idle_validate_ses("--session", args.session)
    pf = _idle_pidfile(config, target)
    lf = _idle_logfile(config, target)
    pid = read_pid(pf)

    if pid is None:
        eprint(f"[idle-watch] {target}: not running (no pid file at {pf})")
        raise SystemExit(1)

    if not pid_alive(pid):
        eprint(f"[idle-watch] {target}: stale pid file (pid {pid} not alive)")
        raise SystemExit(2)

    eprint(f"[idle-watch] {target}: running (pid {pid}, log {lf})")


def cmd_watch_status(args: argparse.Namespace, config: Config) -> None:
    """Compatibility wrapper for ``watch status``.

    By default this mirrors the legacy top-level ``status`` command, so:
      - ``watch status`` -> sidecar /status
      - ``watch status --detail`` -> sidecar /sessions
      - ``watch status --session ses_xxx`` -> sidecar /sessions/{id}

    Use ``--watcher-process --session ses_xxx`` for the old detached
    idle-watch PID/log status check.
    """
    if getattr(args, "watcher_process", False):
        if not getattr(args, "session", None):
            fail("watch status --watcher-process requires --session")
        cmd_idle_watch_status(args, config)
        return
    ns = argparse.Namespace(
        session=getattr(args, "session", None),
        detail=bool(getattr(args, "detail", False)),
    )
    cmd_status(ns, config)


def cmd_idle_watch_restart(args: argparse.Namespace, config: Config) -> None:
    """Force-stop existing watcher (if any), then start a new one."""
    target = _idle_validate_ses("--session", args.session)
    stop_args = argparse.Namespace(
        session=target,
        stop_timeout=args.stop_timeout,
        force=True,
    )
    cmd_idle_watch_stop(stop_args, config)
    cmd_idle_watch_start(args, config)


def _add_idle_watch_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register the 5 idle-watch-* subcommands."""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--session",
        required=True,
        help="Target session ID to watch (must start with 'ses').",
    )
    common.add_argument(
        "--message",
        default=None,
        help=(
            "Override the auto-built notify body. When omitted, the watcher sends a "
            "tagged message: '[idle-notify:<reason>] target=<sid>\\n\\nLast assistant "
            "message:\\n```<body>```' (or a stub if the last reply cannot be fetched). "
            "Set this to send arbitrary text instead of the auto-built body."
        ),
    )
    common.add_argument(
        "--interval",
        type=float,
        default=_IDLE_WATCH_INTERVAL_DEFAULT,
        help=f"Poll interval in seconds. Default: {_IDLE_WATCH_INTERVAL_DEFAULT}",
    )
    common.add_argument(
        "--max-poll-seconds",
        type=float,
        default=0,
        help=("Maximum total poll time in seconds before watcher exits regardless of state. 0 = no limit. Default: 0 (infinity)"),
    )
    common.add_argument(
        "--started-at-ms",
        type=int,
        default=0,
        help="Dispatch start timestamp in epoch milliseconds; used with --notify-if-idle-after-update.",
    )
    common.add_argument(
        "--timeout",
        type=float,
        default=_IDLE_WATCH_TIMEOUT_DEFAULT,
        help=(f"Timeout (seconds) for HTTP /status and prompt_async. Default: {_IDLE_WATCH_TIMEOUT_DEFAULT}"),
    )
    common.add_argument(
        "--max-errors",
        type=int,
        default=_IDLE_WATCH_MAX_ERRORS_DEFAULT,
        help=f"Consecutive error limit before fail. Default: {_IDLE_WATCH_MAX_ERRORS_DEFAULT}",
    )
    common.add_argument(
        "--directory",
        default=None,
        help="Optional directory query param for prompt_async.",
    )
    common.add_argument(
        "--workspace",
        default=None,
        help="Optional workspace query param for prompt_async.",
    )
    common.add_argument(
        "--stop-timeout",
        type=float,
        default=_IDLE_WATCH_STOP_TIMEOUT_DEFAULT,
        help=(f"Stop timeout (seconds) after SIGTERM. Default: {_IDLE_WATCH_STOP_TIMEOUT_DEFAULT}"),
    )
    common.add_argument(
        "--wt-id",
        default="",
        help="Worktree ID for context in stuck-notify messages.",
    )
    common.add_argument(
        "--agent",
        default="",
        help="Agent name for context in stuck-notify messages.",
    )

    def _add_notify_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--notify-session",
            required=True,
            help="Session ID to notify when target becomes idle (must start with 'ses').",
        )
        parser.add_argument(
            "--continuous",
            action="store_true",
            help="Keep watching after first notify; send on every busy -> idle edge.",
        )
        parser.add_argument(
            "--notify-if-initial-idle",
            action="store_true",
            help="Notify once if target is already 'idle' at first tick.",
        )
        parser.add_argument(
            "--notify-if-idle-after-update",
            action="store_true",
            help="Notify when target is idle and has an assistant reply after --started-at-ms; avoids missing very short dispatched tasks.",
        )

    p = sub.add_parser(
        "idle-watch",
        parents=[common],
        help="Foreground: poll sidecar /status and send prompt_async on busy->idle.",
    )
    _add_notify_args(p)
    p.set_defaults(func=cmd_idle_watch)

    p = sub.add_parser(
        "idle-watch-start",
        parents=[common],
        help="Background: spawn idle-watch as a detached watcher process.",
    )
    _add_notify_args(p)
    p.set_defaults(func=cmd_idle_watch_start)

    p = sub.add_parser(
        "idle-watch-stop",
        parents=[common],
        help="Stop background watcher (SIGTERM, escalate to SIGKILL on --force).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Send SIGKILL if process does not exit within --stop-timeout.",
    )
    p.set_defaults(func=cmd_idle_watch_stop)

    p = sub.add_parser(
        "idle-watch-status",
        parents=[common],
        help="Check background watcher state (exit 0 running, 1 no pid, 2 stale).",
    )
    p.set_defaults(func=cmd_idle_watch_status)

    p = sub.add_parser(
        "idle-watch-restart",
        parents=[common],
        help="Force-stop existing watcher, then start a new one.",
    )
    _add_notify_args(p)
    p.set_defaults(func=cmd_idle_watch_restart)


# ---------------- unified command wrappers ----------------


def cmd_service(args: argparse.Namespace, config: Config) -> None:
    """Unified service manager: service <start|stop|status|restart> [opencode|sidecar|all]."""
    action = args.action
    component = args.component

    def _one(name: str, act: str) -> None:
        ns = argparse.Namespace(action=act)
        if name == "opencode":
            cmd_opencode_serve_service(ns, config)
        elif name == "sidecar":
            cmd_sidecar_service(ns, config)
        else:
            fail(f"unknown service component: {name}")

    if action == "status" and component == "all":
        print(
            json.dumps(
                {
                    "opencode": {
                        "healthy": op_healthy(config),
                        "pid": read_pid(pid_file(config, "opencode-server")),
                        "pidFile": str(pid_file(config, "opencode-server")),
                        "logFile": str(log_file(config, "opencode-server")),
                        "url": config.op_server,
                    },
                    "sidecar": {
                        "healthy": sidecar_healthy(config),
                        "pid": read_pid(pid_file(config, "session-status-server")),
                        "pidFile": str(pid_file(config, "session-status-server")),
                        "logFile": str(log_file(config, "session-status-server")),
                        "url": config.sidecar,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if component == "all":
        order = ["opencode", "sidecar"] if action in ("start", "restart") else ["sidecar", "opencode"]
        for name in order:
            _one(name, action)
        return
    _one(component, action)


def _render_repair_stuck_prompt(wt_id: str, agent: str, branch: str, wt_path: Path, old_sid: str) -> str:
    """Generate a continuation prompt for ``pool repair_stuck``."""
    lines: list[str] = []
    try:
        log_out = git(wt_path, "log", "--oneline", "-10", capture=True).stdout.strip()
    except Exception:
        log_out = "(git log failed)"
    lines.append(f"前一个 {agent} session（{old_sid}）在分支 {branch}（{wt_id}）上工作时卡住，可能已部分完成。")
    lines.append("")
    lines.append("请检查分支当前状态：")
    lines.append(f"- git log 查看已完成 commits：\n```\n{log_out}\n```")
    lines.append("- git diff / git status 查看未提交修改")
    lines.append("- pytest / ruff / mypy 查看当前质量")
    lines.append("")
    lines.append("确认已完成部分后，继续完成剩余工作。不要重写已完成的 commits。")
    lines.append("")
    lines.append(_hard_constraints())
    return "\n".join(lines)


def cmd_pool_repair_stuck(args: argparse.Namespace, config: Config) -> None:
    """Repair a stuck session — same branch, new session, auto-prompt."""
    check_services(config)
    wt_id = args.wt_id
    validate_wt_id(wt_id)
    state = read_state(config, wt_id)
    agent = args.agent
    branch = state.get("branch", "")
    wt_path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
    if not branch:
        fail(f"{wt_id} has no active branch; use pool prepare first")
    old_sid = state.get(f"{agent}_session_id", "")
    old_session_status = ""
    if old_sid:
        status_map = http_json("GET", f"{config.sidecar}/status")
        session_status = "unknown"
        if isinstance(status_map, dict):
            session_status = str(status_map.get(old_sid, "unknown"))
        old_session_status = session_status
        if session_status not in ("busy", "streaming"):
            eprint(f"note: old session {old_sid} is {session_status} (not stuck); continuing anyway")
    # Build continuation prompt (preview-safe: no side effects before --yes)
    prompt_text = _render_repair_stuck_prompt(wt_id, agent, branch, wt_path, old_sid)
    if args.task:
        prompt_text = args.task.strip() + "\n\n---\n\n" + prompt_text
    # Build dispatch body
    provider_id, model_id, variant = opencode_model(config, agent, override=getattr(args, "model", None))
    body: dict[str, Any] = {
        "agent": agent,
        "model": prompt_model(provider_id, model_id),
        "parts": [{"type": "text", "text": prompt_text}],
    }
    if variant:
        body["variant"] = variant
    model_label = f"{provider_id}/{model_id}" + (f":{variant}" if variant else "")
    preview = {
        "send": bool(args.yes),
        "wt_id": wt_id,
        "agent": agent,
        "branch": branch,
        "old_session": old_sid or "(none)",
        "model": model_label,
        "directory": str(wt_path),
        "prompt": prompt_text,
    }
    if not args.yes:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        print()
        notify_flag = f" --notify-session {args.notify_session}" if args.notify_session else ""
        model_flag = f" --model {args.model}" if getattr(args, "model", None) else ""
        print(f"python3 scripts/session-worktree-mgr.py pool repair_stuck {args.wt_id} {agent} --yes{notify_flag}{model_flag}")
        return
    # --yes: unwatch old stuck session (soft delete), create fresh one,
    # dispatch. When old session is still busy/streaming, unwatching is
    # unsafe (two sessions would operate on the same worktree concurrently);
    # require --force to override the gate and unwatch anyway.
    if old_sid:
        if old_session_status in ("busy", "streaming") and not bool(getattr(args, "force", False)):
            fail(f"old session {old_sid} is still {old_session_status}; pass --force to unwatch before continuing, or stop the session first")
        elif old_session_status in ("busy", "streaming"):
            eprint(f"warning: old session {old_sid} is still {old_session_status}; --force unwatching anyway")
        delete_session(config, old_sid, hard=False)
        _add_tombstone(config, wt_id, old_sid)
        eprint(f"archived/unwatched stuck session: {old_sid}")
    update_state(config, wt_id, {f"{agent}_session_id": ""})
    new_ses = ensure_session(config, wt_id, wt_path, agent, recreate_existing=True, model_override=getattr(args, "model", None))
    sid = new_ses["id"]
    persist_session(config, wt_id, agent, new_ses, model_override=getattr(args, "model", None))
    watch_session(config, sid)
    query = urllib.parse.urlencode({"directory": str(wt_path)})
    url = f"{config.op_server}/session/{sid}/prompt_async?{query}"
    dispatch_started_at_ms = int(time.time() * 1000)
    http_json("POST", url, body, expected=(204,))
    clear_stored_model_override(config, wt_id, agent, sid)
    print(f"continued -> {wt_id}-{agent} ({sid}) branch={branch} model={model_label} directory={wt_path}")
    notify_sid = args.notify_session or config.pm_session_id
    if notify_sid:
        _idle_validate_ses("--notify-session", notify_sid)
        _spawn_dispatch_idle_watch(
            config,
            sid,
            notify_sid,
            wt_path,
            wt_id=wt_id,
            agent=agent,
            max_poll_seconds=args.max_poll_seconds,
            started_at_ms=dispatch_started_at_ms,
        )


def cmd_session_show(args: argparse.Namespace, config: Config) -> None:
    ns = argparse.Namespace(
        session=args.session_id,
        format=args.format,
        detail=args.detail,
    )
    cmd_overview_session(ns, config)


def cmd_session_status(args: argparse.Namespace, config: Config) -> None:
    sid = args.session_id
    exists = get_session_by_id(config, sid) is not None
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        status_map = {}
    state = "unwatch"
    if isinstance(status_map, dict):
        state = str(status_map.get(sid, "unwatch"))
        if state == "unknown":
            state = "unwatch"
    payload = {"sessionID": sid, "exists": exists, "state": state, "tracked": state != "unwatch"}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"sessionID={sid}")
    print(f"exists={str(exists).lower()}")
    print(f"state={state}")
    print(f"tracked={str(state != 'unwatch').lower()}")


def cmd_session_last(args: argparse.Namespace, config: Config) -> None:
    ns = argparse.Namespace(session=args.session_id, limit=args.limit)
    cmd_last(ns, config)


def cmd_session_dispatch(args: argparse.Namespace, config: Config) -> None:
    ns = argparse.Namespace(
        session=args.session_id,
        wt_id=None,
        agent=args.agent,
        task=args.task,
        yes=args.yes,
        force=args.force,
        notify_session=args.notify_session,
        require_no_busy=args.require_no_busy,
        max_poll_seconds=args.max_poll_seconds,
        model=getattr(args, "model", None),
    )
    cmd_dispatch(ns, config)


def cmd_session_delete(args: argparse.Namespace, config: Config) -> None:
    sid = args.session_id
    exists = get_session_by_id(config, sid) is not None
    state_touched: list[str] = []
    payload = {
        "dryRun": not args.yes,
        "sessionID": sid,
        "exists": exists,
        "mode": "hard-delete" if args.hard else "soft-delete/tombstone",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not args.yes:
        print()
        print("确认删除后加 --yes")
        return
    if args.hard:
        state_touched = _remove_session_pointer_by_session_id(config, sid)
    else:
        state_touched = _add_tombstone_by_session_id(config, sid)
        if not state_touched:
            eprint(f"warning: no persisted state pointer found for {sid}; soft delete will only unwatch it")
    delete_session(config, sid, hard=args.hard)
    if state_touched:
        label = "cleaned from state" if args.hard else "tombstoned in state"
        eprint(f"{label}: {', '.join(state_touched)}")
    eprint(f"{'deleted' if args.hard else 'tombstoned'} session: {sid}{' (hard)' if args.hard else ''}")


def cmd_legacy_prepare(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} pool prepare --branch ..." instead of top-level "prepare".')
    cmd_prepare(args, config)


def cmd_legacy_release(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} pool release wt_N" instead of top-level "release".')
    cmd_release(args, config)


def cmd_legacy_dispatch(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} pool dispatch wt_N Agent ..." or "{PROG} session dispatch ses_xxx ..." instead of top-level "dispatch".')
    cmd_dispatch(args, config)


def cmd_legacy_status(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        _warn_deprecated(f'use "{PROG} session status {args.session}" instead of top-level "status --session".')
    else:
        _warn_deprecated(f'use "{PROG} service status" for services or "{PROG} overview" for project state.')
    cmd_status(args, config)


def cmd_legacy_last(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} session last {args.session}" instead of top-level "last --session".')
    cmd_last(args, config)


def cmd_legacy_overview(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        _warn_deprecated(f'use "{PROG} session show {args.session}" instead of "overview --session".')
    cmd_overview(args, config)


# ---------------- parser ----------------


def _add_dispatch_options(
    parser: argparse.ArgumentParser,
    *,
    allow_session: bool,
    task_required: bool = True,
    force_help: str | None = None,
) -> None:
    if allow_session:
        parser.add_argument("--session", default=None, help=argparse.SUPPRESS)
    if task_required:
        parser.add_argument("--task", required=True, help="Task text to send. Without --yes this only previews the prompt.")
    else:
        # ``pool repair_stuck`` auto-generates the continuation prompt when --task
        # is omitted; --task is an optional override on top of it.
        parser.add_argument("--task", default="", help="Optional task override. If omitted, the continuation prompt is auto-generated.")
    parser.add_argument("--yes", action="store_true", help="Actually send the prompt. Without this flag, print a preview only.")
    parser.add_argument(
        "--force",
        action="store_true",
        help=force_help or "For busy/streaming sessions, soft-archive and recreate; healthy idle/unknown sessions are reused.",
    )
    parser.add_argument(
        "--notify-session",
        default=env("PM_CURRENT_SESSION_ID", ""),
        help="PM session to notify when target becomes idle. Defaults to $PM_CURRENT_SESSION_ID, then current PM session if available.",
    )
    parser.add_argument(
        "--require-no-busy",
        action="store_true",
        help="Refuse dispatch unless session is idle/unknown/unwatch. busy/streaming always fail.",
    )
    parser.add_argument(
        "--max-poll-seconds",
        type=float,
        default=0,
        help="Max poll seconds for auto idle-watch. 0 = unlimited. Recommended: 1800 for long backend tasks.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=("Override session/prompt model. Format: 'providerID/modelID' or 'providerID/modelID:variant'. Default: read from agent definition in opencode.json."),
    )


def _add_overview_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--detail", action="store_true")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--wt", help="Show one pool worktree, e.g. wt_1 or 1.")
    target_group.add_argument("--main", action="store_true", help="Show main repository sessions only.")
    recent_group = parser.add_mutually_exclusive_group()
    recent_group.add_argument(
        "--recent",
        default=None,
        help=(f"Recent window, e.g. 3d / 24h / 30m / 30s. Default: {_OVERVIEW_RECENT_DEFAULT_SECONDS // 86400}d. Use 0 to keep only the newest session per group."),
    )
    recent_group.add_argument("--all", action="store_true", help="Disable filtering and show every session.")
    parser.add_argument("--show-orphan", action="store_true", help="Include orphan main-worktree sessions. Default: hidden.")
    parser.add_argument("--show-unwatch", action="store_true", help="Include unwatched sessions. Default: hidden.")
    parser.add_argument("--verbose", action="store_true", help="Emit informational warnings to stderr.")


def _add_watch_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", required=True, help="Target session ID to watch, must start with ses.")
    parser.add_argument(
        "--message",
        default=None,
        help="Override notify body. Omit to send tagged last-assistant-message summary.",
    )
    parser.add_argument("--interval", type=float, default=_IDLE_WATCH_INTERVAL_DEFAULT)
    parser.add_argument("--max-poll-seconds", type=float, default=0, help="0 = no limit")
    parser.add_argument("--started-at-ms", type=int, default=0, help="Dispatch start timestamp in epoch milliseconds; used with --notify-if-idle-after-update.")
    parser.add_argument("--timeout", type=float, default=_IDLE_WATCH_TIMEOUT_DEFAULT)
    parser.add_argument("--max-errors", type=int, default=_IDLE_WATCH_MAX_ERRORS_DEFAULT)
    parser.add_argument("--directory", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--stop-timeout", type=float, default=_IDLE_WATCH_STOP_TIMEOUT_DEFAULT)


def _add_watch_notify(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--notify-session", required=True, help="Session ID to notify when target becomes idle.")
    parser.add_argument("--continuous", action="store_true", help="Keep watching after first notify.")
    parser.add_argument("--notify-if-initial-idle", action="store_true", help="Notify if target is idle at first tick.")
    parser.add_argument("--notify-if-idle-after-update", action="store_true", help="Notify if target is idle and has assistant output after --started-at-ms.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenCode worktree/session manager with explicit resource-oriented CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Command model:

  One session by ID:
    {PROG} session show ses_xxx
    {PROG} session status ses_xxx
    {PROG} session last ses_xxx
    {PROG} session dispatch ses_xxx --task "..." --yes
    {PROG} session delete ses_xxx --yes

  Multiple sessions by filters:
    {PROG} sessions list --wt wt_1
    {PROG} sessions list --main
    {PROG} sessions delete --wt wt_1 --agent Daedalus --keep-latest --yes
    {PROG} sessions create --agent Janitor

  Worktree pool lifecycle:
    {PROG} pool init --size 10
    {PROG} pool status --verify
    {PROG} pool repair wt_1
    {PROG} pool prepare --branch feat_xxx
    {PROG} pool dispatch wt_1 Daedalus --task "..." --yes
    {PROG} pool release wt_1

  Services and global inspection:
    {PROG} service status
    {PROG} service start all
    {PROG} overview
    {PROG} overview --wt wt_1
    {PROG} overview --main

Do NOT infer these forms:
  sessions list --session ses_xxx     # wrong; use: session show ses_xxx
  overview --session ses_xxx          # deprecated; use: session show ses_xxx
  status --session ses_xxx            # deprecated; use: session status ses_xxx
""",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{service,pool,session,sessions,overview,watch}")

    # New unified service interface
    service = sub.add_parser(
        "service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage OpenCode and sidecar services.",
        epilog=f"""\
Examples:
  {PROG} service status
  {PROG} service start all
  {PROG} service restart sidecar
  {PROG} service stop all
""",
    )
    service.add_argument("action", choices=["start", "stop", "status", "restart"])
    service.add_argument("component", nargs="?", choices=["opencode", "sidecar", "all"], default="all")
    service.set_defaults(func=cmd_service)

    # Legacy service names, hidden from help but kept for old scripts.
    op_svc = sub.add_parser("opencode-serve-service", help=argparse.SUPPRESS)
    op_svc.add_argument("action", choices=["start", "stop", "status", "restart"])
    op_svc.set_defaults(func=cmd_opencode_serve_service)
    sidecar_svc = sub.add_parser("sidecar-service", help=argparse.SUPPRESS)
    sidecar_svc.add_argument("action", choices=["start", "stop", "status", "restart"])
    sidecar_svc.set_defaults(func=cmd_sidecar_service)

    # Pool resource
    pool = sub.add_parser(
        "pool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage long-lived wt_N worktree pool and pool-owned sessions.",
        epilog=f"""\
Use cases:
  First setup:     {PROG} pool init --size 10
  Health check:    {PROG} pool status --verify
  Repair wt:       {PROG} pool repair wt_1 --reset --force-copy
  Start a task:    {PROG} pool prepare --branch feat_xxx
  Send task:       {PROG} pool dispatch wt_1 Daedalus --task "..." --yes
  Release wt:      {PROG} pool release wt_1 --force
""",
    )
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)

    pool_init = pool_sub.add_parser("init", help="Create/reuse and warm up wt_N pool.")
    pool_init.add_argument("--size", type=int, default=10)
    pool_init.add_argument("--agents")
    pool_init.add_argument("--reset", action="store_true")
    pool_init.add_argument("--force-copy", action="store_true")
    pool_init.set_defaults(func=cmd_pool_init)

    pool_repair = pool_sub.add_parser("repair", help="Repair one wt_N worktree and its sessions.")
    pool_repair.add_argument("wt_id")
    pool_repair.add_argument("--agents")
    pool_repair.add_argument("--reset", action="store_true")
    pool_repair.add_argument("--force-copy", action="store_true")
    pool_repair.set_defaults(func=cmd_pool_repair)

    pool_status = pool_sub.add_parser("status", help="Show pool state. Use --verify to check OpenCode sessions exist.")
    pool_status.add_argument("--size", type=int, default=10)
    pool_status.add_argument("--agents")
    pool_status.add_argument("--verify", action="store_true")
    pool_status.set_defaults(func=cmd_pool_status)

    pool_prepare = pool_sub.add_parser("prepare", help="Grab an idle initialized worktree and check out a task branch.")
    pool_prepare.add_argument("--branch", "-b", required=True)
    pool_prepare.add_argument("--agents")
    pool_prepare.add_argument("--force-branch", action="store_true")
    pool_prepare.set_defaults(func=cmd_prepare)

    pool_dispatch = pool_sub.add_parser("dispatch", help="Dispatch a task to a pool worktree agent session.")
    pool_dispatch.add_argument("wt_id", help="Pool worktree id, e.g. wt_1")
    pool_dispatch.add_argument("agent", help="Agent name, e.g. Daedalus")
    _add_dispatch_options(pool_dispatch, allow_session=False)
    pool_dispatch.set_defaults(func=cmd_dispatch, session=None)

    pool_continue = pool_sub.add_parser("repair_stuck", help="Repair a stuck session — archive old, create new session on same branch, auto-generate continuation prompt.")
    pool_continue.add_argument("wt_id", help="Pool worktree id, e.g. wt_1")
    pool_continue.add_argument("agent", help="Agent name, e.g. Daedalus")
    _add_dispatch_options(
        pool_continue,
        allow_session=False,
        task_required=False,
        force_help="Allow repair_stuck to archive a busy/streaming old session; repair_stuck always recreates.",
    )
    pool_continue.set_defaults(func=cmd_pool_repair_stuck, session=None)

    pool_release = pool_sub.add_parser("release", help="Reset a task worktree and mark it idle.")
    pool_release.add_argument("target", help="wt_N or worktree path")
    pool_release.add_argument("--force", action="store_true", help="Discard uncommitted changes with git reset --hard && git clean -fd.")
    pool_release.set_defaults(func=cmd_release)

    # Single-session resource
    session = sub.add_parser(
        "session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Operate on exactly one OpenCode session by session ID.",
        epilog=f"""\
Examples:
  {PROG} session show ses_xxx
  {PROG} session status ses_xxx
  {PROG} session last ses_xxx
  {PROG} session dispatch ses_xxx --task "..." --yes
  {PROG} session delete ses_xxx --yes

Do not use "sessions list --session". For one session, use "session show/status/last".
""",
    )
    session_sub = session.add_subparsers(dest="session_command", required=True)

    session_show = session_sub.add_parser("show", help="Show one session metadata, tokens, context, and optional last reply.")
    session_show.add_argument("session_id")
    session_show.add_argument("--format", choices=["text", "json"], default="text")
    session_show.add_argument("--detail", action="store_true")
    session_show.set_defaults(func=cmd_session_show)

    session_status = session_sub.add_parser("status", help="Show one session sidecar state: idle/busy/streaming/unwatch.")
    session_status.add_argument("session_id")
    session_status.add_argument("--format", choices=["text", "json"], default="text")
    session_status.set_defaults(func=cmd_session_status)

    session_last = session_sub.add_parser("last", help="Print the last assistant reply for one session.")
    session_last.add_argument("session_id")
    session_last.add_argument("--limit", type=int, default=50)
    session_last.set_defaults(func=cmd_session_last)

    session_dispatch = session_sub.add_parser("dispatch", help="Dispatch a task directly to one session ID.")
    session_dispatch.add_argument("session_id")
    session_dispatch.add_argument("--agent", default=None, help="Optional agent override if session metadata lacks agent.")
    session_dispatch.add_argument(
        "--pm-session-id",
        default=None,
        help=(
            "Override the current PM session scope (default: $PM_SESSION_ID or .pm/pm-session-info.json). "
            "Used for PM session ownership checks (P1-3): dispatch is rejected if --session does not belong to this PM."
        ),
    )
    _add_dispatch_options(session_dispatch, allow_session=False)
    session_dispatch.set_defaults(func=cmd_session_dispatch)

    session_delete = session_sub.add_parser("delete", help="Soft-delete/tombstone or hard-delete one session by ID.")
    session_delete.add_argument("session_id")
    session_delete.add_argument("--hard", action="store_true", help="DELETE from OpenCode server. Irreversible.")
    session_delete.add_argument("--yes", action="store_true")
    session_delete.set_defaults(func=cmd_session_delete)

    # Multiple-sessions resource
    sessions_parser = sub.add_parser(
        "sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List/create/delete multiple sessions by explicit filters. Use 'session ...' for one ID.",
        epilog=f"""\
Examples:
  {PROG} sessions create --agent Janitor
  {PROG} sessions list --wt wt_1
  {PROG} sessions list --main --agent Janitor
  {PROG} sessions delete --wt wt_1 --agent Daedalus --keep-latest --yes

Wrong:
  {PROG} sessions list --session ses_xxx
Right:
  {PROG} session show ses_xxx
""",
    )
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_command", required=True)

    sessions_create = sessions_sub.add_parser("create", help="Create/reuse a persistent main-repo agent session.")
    sessions_create.add_argument("--agent", required=True, help="Agent name, e.g. Janitor, General, Momus, Clio.")
    sessions_create.add_argument(
        "--force",
        action="store_true",
        help="Hard-delete the existing session and create a new one. Without --force, busy/streaming sessions refuse creation; stale/oversized sessions are auto-rebuilt.",
    )
    sessions_create.add_argument("--directory", default=None, help="Directory for the session. Default: repo root.")
    sessions_create.add_argument(
        "--pm-session-id",
        default=None,
        help=("Override the current PM session scope (default: $PM_SESSION_ID or .pm/pm-session-info.json). Used for per-PM main.state isolation (P1-3)."),
    )
    sessions_create.add_argument(
        "--model",
        default=None,
        help=("Override session model. Format: 'providerID/modelID' or 'providerID/modelID:variant'. Default: read from agent definition in opencode.json."),
    )
    sessions_create.set_defaults(func=cmd_session_create)

    sessions_list = sessions_sub.add_parser("list", help="List sessions by --wt/--main/--path filters.")
    sessions_list.add_argument("target", nargs="?", help=argparse.SUPPRESS)  # legacy positional target
    sessions_list.add_argument("--wt", help="Worktree id, e.g. wt_1 or 1.")
    sessions_list.add_argument("--main", action="store_true", help="Main repository sessions.")
    sessions_list.add_argument("--path", help="Explicit worktree path.")
    sessions_list.add_argument("--session", help=argparse.SUPPRESS)
    sessions_list.add_argument("--agent")
    sessions_list.add_argument("--agents")
    sessions_list.add_argument("--format", choices=["text", "json"], default="text")
    sessions_list.add_argument(
        "--pm-session-id",
        default=None,
        help=("Override the current PM session scope (default: $PM_SESSION_ID or .pm/pm-session-info.json). With --main, lists only sessions owned by this PM (P1-3 isolation)."),
    )
    sessions_list.set_defaults(func=cmd_sessions_list)

    sessions_delete = sessions_sub.add_parser("delete", help="Delete/tombstone sessions by --wt/--main/--path filters.")
    sessions_delete.add_argument("target", nargs="?", help=argparse.SUPPRESS)  # legacy positional target
    sessions_delete.add_argument("--wt", help="Worktree id, e.g. wt_1 or 1.")
    sessions_delete.add_argument("--main", action="store_true", help="Main repository sessions.")
    sessions_delete.add_argument("--path", help="Explicit worktree path.")
    sessions_delete.add_argument("--session", help=argparse.SUPPRESS)
    sessions_delete.add_argument("--agent")
    sessions_delete.add_argument("--agents")
    sessions_delete.add_argument("--keep-latest", action="store_true")
    sessions_delete.add_argument("--hard", action="store_true", help="DELETE from OpenCode server. Irreversible.")
    sessions_delete.add_argument("--yes", action="store_true")
    sessions_delete.set_defaults(func=cmd_sessions_delete)

    # Global overview
    overview = sub.add_parser(
        "overview",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Global/session landscape overview. For one session, prefer: session show ses_xxx.",
        epilog=f"""\
Examples:
  {PROG} overview
  {PROG} overview --wt wt_1
  {PROG} overview --main
  {PROG} overview --all

Deprecated compatibility:
  {PROG} overview --session ses_xxx   # use: {PROG} session show ses_xxx
""",
    )
    _add_overview_filters(overview)
    overview.add_argument("--session", help=argparse.SUPPRESS)
    overview.add_argument("--watch", action="store_true", help="Continuously refresh overview (Ctrl+C to stop).")
    overview.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds (default: 5.0, requires --watch).")
    overview.set_defaults(func=cmd_legacy_overview)

    # Watch resource (new names), backed by existing idle-watch implementation.
    watch = sub.add_parser(
        "watch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage idle-watch processes.",
        epilog=f"""\
Examples:
  {PROG} watch idle --session ses_target --notify-session ses_pm
  {PROG} watch start --session ses_target --notify-session ses_pm
  {PROG} watch status
  {PROG} watch status --session ses_target
  {PROG} watch status --watcher-process --session ses_target
  {PROG} watch stop --session ses_target --force
""",
    )
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)

    watch_idle = watch_sub.add_parser("idle", help="Foreground one-shot/continuous busy->idle watcher.")
    _add_watch_common(watch_idle)
    _add_watch_notify(watch_idle)
    watch_idle.set_defaults(func=cmd_idle_watch)

    watch_start = watch_sub.add_parser("start", help="Start a detached idle watcher.")
    _add_watch_common(watch_start)
    _add_watch_notify(watch_start)
    watch_start.set_defaults(func=cmd_idle_watch_start)

    watch_stop = watch_sub.add_parser("stop", help="Stop a detached idle watcher.")
    _add_watch_common(watch_stop)
    watch_stop.add_argument("--force", action="store_true")
    watch_stop.set_defaults(func=cmd_idle_watch_stop)

    watch_status = watch_sub.add_parser("status", help="Show sidecar watch status; optionally inspect detached watcher process.")
    watch_status.add_argument("--detail", action="store_true", help="Show detailed sidecar session status, same as top-level status --detail.")
    watch_status.add_argument("--session", help="Show sidecar status for one session, same as top-level status --session.")
    watch_status.add_argument("--watcher-process", action="store_true", help="With --session, check detached idle-watch process pid/log status instead of sidecar status.")
    watch_status.set_defaults(func=cmd_watch_status)

    watch_restart = watch_sub.add_parser("restart", help="Restart detached idle watcher.")
    _add_watch_common(watch_restart)
    _add_watch_notify(watch_restart)
    watch_restart.set_defaults(func=cmd_idle_watch_restart)

    # Legacy top-level aliases, hidden from help but kept compatible.
    prepare = sub.add_parser("prepare", help=argparse.SUPPRESS)
    prepare.add_argument("--branch", "-b", required=True)
    prepare.add_argument("--agents")
    prepare.add_argument("--force-branch", action="store_true")
    prepare.set_defaults(func=cmd_legacy_prepare)

    dispatch = sub.add_parser("dispatch", help=argparse.SUPPRESS)
    dispatch.add_argument("wt_id", nargs="?")
    dispatch.add_argument("agent", nargs="?")
    dispatch.add_argument("--session", default=None)
    _add_dispatch_options(dispatch, allow_session=False)
    dispatch.set_defaults(func=cmd_legacy_dispatch)

    release = sub.add_parser("release", help=argparse.SUPPRESS)
    release.add_argument("target")
    release.add_argument("--force", action="store_true")
    release.set_defaults(func=cmd_legacy_release)

    status = sub.add_parser("status", help=argparse.SUPPRESS)
    status.add_argument("--detail", action="store_true")
    status.add_argument("--session")
    status.set_defaults(func=cmd_legacy_status)

    last = sub.add_parser("last", help=argparse.SUPPRESS)
    last.add_argument("--session", required=True)
    last.add_argument("--limit", type=int, default=50)
    last.set_defaults(func=cmd_legacy_last)

    # Legacy idle-watch top-level aliases.
    _add_idle_watch_subparsers(sub)
    return parser


def main() -> None:
    # Build/parse CLI before loading repo-bound config so ``-h`` works from
    # anywhere. This is important for AI self-correction after a failed call.
    parser = build_parser()
    args = parser.parse_args()
    config = Config.load()
    # CLI ``--pm-session-id`` overrides env / JSON file default.  Applied
    # post-Config.load so the override flows through every Config consumer;
    # state-file paths in ``_main_state_file`` re-derive on each call.
    # ``object.__setattr__`` bypasses the frozen-dataclass guard.
    pm_sid_override = getattr(args, "pm_session_id", None) or env("PM_SESSION_ID", "")
    if pm_sid_override:
        object.__setattr__(config, "pm_session_id", pm_sid_override)
    args.func(args, config)


if __name__ == "__main__":
    main()
