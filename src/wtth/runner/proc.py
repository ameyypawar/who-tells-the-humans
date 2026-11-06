"""Subprocess handling: process groups, timeout ladder, orphan sweep, log harvest. Owner: WP3.

No shell, no pipes: stdout and stderr go to files. Each child leads its own session (start_new_session=True), so
its pgid == pid and the whole tree (CLI + its MCP server) can be signalled as one group.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from wtth.types import ProcResult, SlotDirs

PS = "/bin/ps"
SYSCTL = "/usr/sbin/sysctl"
_POLL_S = 0.05
_RSS_INTERVAL_S = 3.0
_RSS_FIRST_S = 0.5
_STRAGGLER_WAIT_S = 1.5
_KILL_WAIT_S = 3.0


# ---- group helpers ----------------------------------------------------------------------------------

def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group_gone(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


def _signal_group(pgid: int, sig: int) -> bool:
    """Send sig to the group; False if the group no longer exists."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def kill_group(pgid: int, grace_int_s: float, grace_term_s: float) -> str:
    """SIGINT -> wait grace_int_s -> SIGTERM -> wait grace_term_s -> SIGKILL. Returns how far it went:
    'none' (group already gone), 'sigint', 'sigterm' or 'sigkill' (the stage after which the group was gone)."""
    if not _group_alive(pgid):
        return "none"
    _signal_group(pgid, signal.SIGINT)
    if _wait_group_gone(pgid, grace_int_s):
        return "sigint"
    _signal_group(pgid, signal.SIGTERM)
    if _wait_group_gone(pgid, grace_term_s):
        return "sigterm"
    _signal_group(pgid, signal.SIGKILL)
    _wait_group_gone(pgid, _KILL_WAIT_S)
    return "sigkill"


# ---- run_process ------------------------------------------------------------------------------------

async def _rss_loop(pgid: int, out: list[int]) -> None:
    loop = asyncio.get_running_loop()
    await asyncio.sleep(_RSS_FIRST_S)  # a sample taken at exec time only sees the bare launcher
    while True:
        try:
            got = await loop.run_in_executor(None, sample_rss_kb, [pgid])
        except Exception:  # noqa: BLE001 - sampling is best effort
            got = {}
        v = got.get(pgid)
        if v is not None:
            out.append(v)
        await asyncio.sleep(_RSS_INTERVAL_S)


async def run_process(argv: Sequence[str], env: Mapping[str, str], cwd: str | Path, stdout_path: Path,
                      stderr_path: Path, timeout_s: float, grace_int_s: float = 10.0,
                      grace_term_s: float = 5.0, rss_sampler: bool = False,
                      cancel: asyncio.Event | None = None) -> ProcResult:
    """Run one child to completion, to the timeout, or until `cancel` is set (`cancel` is an
    extra keyword-only-by-use argument with a default; the frozen positional signature is unchanged).

    kill_stage: None for a normal exit; 'sigint'|'sigterm'|'sigkill' when the timeout or cancel ladder ran;
    'straggler_<stage>' when the child exited by itself but left group members that had to be killed.
    """
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    with open(stdout_path, "wb") as out_fh, open(stderr_path, "wb") as err_fh:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=dict(env), stdin=asyncio.subprocess.DEVNULL,
            stdout=out_fh, stderr=err_fh, start_new_session=True)
    pgid = proc.pid
    rss: list[int] = []
    rss_task = asyncio.ensure_future(_rss_loop(pgid, rss)) if rss_sampler else None
    timed_out = False
    kill_stage: str | None = None
    wait_t = asyncio.ensure_future(proc.wait())
    cancel_t = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
    try:
        waiters = {wait_t} | ({cancel_t} if cancel_t is not None else set())
        done, _ = await asyncio.wait(waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if wait_t not in done:
            timed_out = cancel_t is None or cancel_t not in done
            kill_stage = await loop.run_in_executor(None, kill_group, pgid, grace_int_s, grace_term_s)
            await proc.wait()
        else:
            # leader exited: make sure nothing is left in the group (the MCP server child)
            gone = await loop.run_in_executor(None, _wait_group_gone, pgid, _STRAGGLER_WAIT_S)
            if not gone:
                stage = await loop.run_in_executor(None, kill_group, pgid, grace_int_s, grace_term_s)
                kill_stage = f"straggler_{stage}"
    except BaseException:
        # outer cancellation or any error: never leave a live group behind
        await asyncio.shield(loop.run_in_executor(None, kill_group, pgid, 0.5, 0.5))
        if proc.returncode is None:
            await asyncio.shield(proc.wait())
        raise
    finally:
        for t in (wait_t, cancel_t, rss_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (cancel_t, rss_task):
            if t is not None:
                try:
                    await t
                except BaseException:  # noqa: BLE001
                    pass
    rc = proc.returncode
    exit_code = rc if rc is not None and rc >= 0 else None
    term_signal = -rc if rc is not None and rc < 0 else None
    if rss_sampler:
        got = await loop.run_in_executor(None, sample_rss_kb, [pgid])
        if pgid in got:
            rss.append(got[pgid])
    return ProcResult(
        exit_code=exit_code, term_signal=term_signal, timed_out=timed_out, kill_stage=kill_stage,
        wall_ms=int((time.monotonic() - t0) * 1000), stdout_path=stdout_path, stderr_path=stderr_path, pgid=pgid,
        peak_rss_kb=max(rss) if rss else None)


# ---- orphans ----------------------------------------------------------------------------------------

def _ps_rows() -> list[tuple[int, int, int, str, str]]:
    out = subprocess.run([PS, "-ww", "-axo", "pid=,pgid=,ppid=,stat=,command="], capture_output=True, text=True,
                         timeout=20, check=False).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4]))
        except ValueError:
            continue
    return rows


def orphan_sweep(binary_path: Path, runs_root: Path, live_pgids: set[int]) -> list[int]:
    """Kill processes of a previous orchestrator: command contains both the pinned binary path and
    '<runs_root>/stimuli/', or both '-m wtth.mcp_server' and '--runs-root <runs_root>'; pgid not in live_pgids.
    SIGTERM, then SIGKILL after 2 s. Returns the pids signalled. Leaves everything else alone."""
    root = str(runs_root).rstrip("/")
    # path tokens must start and end at an argv boundary: '/x/runs' must not match '/x/runs_more' or '/y/x/runs'
    binary_re = re.compile(r"(?<![\w/.-])" + re.escape(str(binary_path)) + r"(?![\w.-])")
    stim_re = re.compile(r"(?<![\w/.-])" + re.escape(root) + r"/stimuli/")
    server_re = re.compile(r"-m wtth\.mcp_server\b.*--runs-root " + re.escape(root) + r"(?![\w/.-])")
    me, my_pgid = os.getpid(), os.getpgid(0)
    targets: dict[int, int] = {}
    for pid, pgid, _ppid, stat, cmd in _ps_rows():
        if pid == me or pgid == my_pgid or pgid in live_pgids or stat.startswith("Z"):
            continue
        cli = bool(binary_re.search(cmd) and stim_re.search(cmd))
        srv = bool(server_re.search(cmd))
        if cli or srv:
            targets[pid] = pgid
    if not targets:
        return []

    def send(sig: int) -> None:
        done_groups: set[int] = set()
        for pid, pgid in targets.items():
            try:
                if pid == pgid:  # session/group leader: take its group (the MCP child) with it
                    if pgid not in done_groups:
                        os.killpg(pgid, sig)
                        done_groups.add(pgid)
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    send(signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_pid_alive(p) for p in targets):
        time.sleep(_POLL_S)
    if any(_pid_alive(p) for p in targets):
        send(signal.SIGKILL)
        deadline = time.monotonic() + _KILL_WAIT_S
        while time.monotonic() < deadline and any(_pid_alive(p) for p in targets):
            time.sleep(_POLL_S)
    return sorted(targets)


def _pid_alive(pid: int) -> bool:
    """Alive and not a zombie (a zombie child of this process counts as dead for sweep purposes)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        out = subprocess.run([PS, "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                             check=False).stdout.strip()
    except subprocess.SubprocessError:
        return True
    return bool(out) and not out.startswith("Z")


# ---- logs / sampling --------------------------------------------------------------------------------

def harvest_mcp_log(slot: SlotDirs, cwd: str, dest_dir: Path, since_ts: float,
                    server_name: str = "workspace") -> list[Path]:  # WP4-ADD: server_name
    """Move Claude Code's MCP logs written since `since_ts` out of the slot into dest_dir. Returns the new paths."""
    base = Path(slot.home) / "Library" / "Caches" / "claude-cli-nodejs" / str(cwd).replace("/", "-")
    src = base / f"mcp-logs-{server_name}"
    if not src.is_dir():  # the CLI may sanitize the name; ours is the only server, so any mcp-logs-* dir is it
        others = sorted(base.glob("mcp-logs-*")) if base.is_dir() else []
        if not others:
            return []
        src = others[0]
    dest_dir = Path(dest_dir)
    moved: list[Path] = []
    for f in sorted(src.iterdir()):
        try:
            if not f.is_file() or f.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f.name
        shutil.move(str(f), str(target))
        moved.append(target)
    return moved


def sample_rss_kb(pgids: Iterable[int]) -> dict[int, int]:
    """Summed RSS in KB per process group (one ps call)."""
    want = set(pgids)
    if not want:
        return {}
    try:
        out = subprocess.run([PS, "-ww", "-axo", "pgid=,rss="], capture_output=True, text=True, timeout=20,
                             check=False).stdout
    except subprocess.SubprocessError:
        return {}
    acc: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            g, r = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if g in want:
            acc[g] = acc.get(g, 0) + r
    return acc


def memory_pressure_level() -> int:
    """kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical. Unknown -> 1."""
    try:
        out = subprocess.run([SYSCTL, "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True,
                             timeout=10, check=False).stdout.strip()
        return int(out)
    except (ValueError, subprocess.SubprocessError, OSError):
        return 1
