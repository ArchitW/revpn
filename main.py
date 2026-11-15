#!/usr/bin/env python3
"""
VPN Rotator for testing a command inside a VPN namespace.
- Rotates through VPNGate profiles (no repeat per day)
- Auto failover on connection failure
- Moves failed profiles to failed/ folder
- Runs a command (default: papapopo) inside VPN namespace via vopono

The command is handed to `vopono exec` as a single shell-split string, so you
can pass arguments too:
    revpn -- mytool --flag value

The executable is looked up with shutil.which() first - the same thing as
wrapping it in "$(which __COMMAND__)" - because vopono launches it through
`sudo --set-home`, whose PATH may not include your user's tool directories.
If vopono still reports "command not found", the command is retried wrapped in
`sh -c 'exec "$(command -v ...)" ...'`, and a command problem never burns a
profile (it exits instead of moving the profile to vpn_profiles_failed).
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

VPN_PROFILES_DIR = BASE_DIR / "vpn_profiles"
FAILED_DIR = BASE_DIR / "vpn_profiles_failed"
STATE_FILE = BASE_DIR / "vpn_state.json"
DEFAULT_COMMAND = "papapopo"
TIMEOUT = 30
INTERFACE_PREFIX = "vo_c_"

# vopono fails with one of these when it cannot exec the app at all. That is a
# command/installation problem, not a bad VPN profile.
NOT_FOUND_MARKERS = (
    "command not found",
    "no such file or directory",
    "executable file not found",
    "not found in $path",
)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"used_today": [], "date": str(date.today())}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_available_profiles() -> List[Path]:
    if not VPN_PROFILES_DIR.exists():
        return []
    return sorted(VPN_PROFILES_DIR.glob("*.ovpn"))


def get_failed_profiles() -> List[Path]:
    if not FAILED_DIR.exists():
        return []
    return sorted(FAILED_DIR.glob("*.ovpn"))


def reset_daily_state_if_needed(state: dict) -> dict:
    today = str(date.today())
    if state.get("date") != today:
        state = {"used_today": [], "date": today}
        save_state(state)
    return state


def select_next_profile(state: dict) -> Optional[Path]:
    available = get_available_profiles()
    if not available:
        return None

    used = set(state.get("used_today", []))
    unused = [p for p in available if p.name not in used]

    if not unused:
        print("All profiles used today. Resetting daily usage.")
        state["used_today"] = []
        save_state(state)
        unused = available

    return unused[0]


def move_to_failed(profile: Path) -> None:
    FAILED_DIR.mkdir(exist_ok=True)
    dest = FAILED_DIR / profile.name
    shutil.move(str(profile), str(dest))
    print(f"Moved failed profile to {dest}")


def parse_command(values: Sequence[str]) -> List[str]:
    """Turn CLI values into argv, defaulting to DEFAULT_COMMAND.

    Accepts either separate words (`main.py mytool --flag`) or one quoted
    string (`main.py "mytool --flag"`).
    """
    if not values:
        return [DEFAULT_COMMAND]
    if len(values) == 1 and " " in values[0] and not Path(values[0]).expanduser().exists():
        return shlex.split(values[0])
    return list(values)


def resolve_executable(command: Sequence[str]) -> Optional[str]:
    """Resolve the program to an absolute path, like `which` does."""
    exe = command[0]
    if "/" in exe:
        path = Path(exe).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which(exe)


def quote_command(command: Sequence[str]) -> str:
    """vopono exec takes the application as one shell-split string."""
    return shlex.join(command)


def shell_wrap_command(command: Sequence[str]) -> str:
    """Resolve the binary at exec time with $(which ...), inside the namespace."""
    wrapper = 'exec "$(command -v "$0" || which "$0")" "$@"'
    return shlex.join(["sh", "-c", wrapper, *command])


def looks_like_command_error(returncode: Optional[int], output: str) -> bool:
    if returncode == 127:
        return True
    text = (output or "").lower()
    return any(marker in text for marker in NOT_FOUND_MARKERS)


def list_vopono_interfaces() -> List[str]:
    """Return the names of leftover vopono interfaces (vo_c_*)."""
    try:
        result = subprocess.run(
            ["ip", "link", "show"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"Could not list interfaces: {e}", file=sys.stderr)
        return []

    interfaces = []
    for line in result.stdout.splitlines():
        match = re.match(r"^\d+:\s+([^@\s:]+)", line)
        if match and match.group(1).startswith(INTERFACE_PREFIX):
            interfaces.append(match.group(1))
    return interfaces


def cleanup_vopono_interfaces() -> None:
    """Delete any orphaned vo_c_* interfaces left behind by an abrupt vopono exit."""
    interfaces = list_vopono_interfaces()
    if not interfaces:
        print("No leftover vopono interfaces.")
        return
    for iface in interfaces:
        print(f"Deleting leftover interface: {iface}")
        result = subprocess.run(
            ["sudo", "-n", "ip", "link", "delete", iface],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "").strip()
            print(
                f"Failed to delete {iface}: {msg}\n"
                f"Run manually: sudo ip link delete {iface}",
                file=sys.stderr,
            )
        else:
            print(f"Deleted {iface}")


def ensure_sudo() -> bool:
    """Cache sudo credentials so cleanup can use `sudo -n` without re-prompting.

    Returns True if sudo is available without a prompt, or after a successful
    `sudo -v` on a terminal. Returns False if sudo needs a password but no
    terminal is available (batch mode).
    """
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return True
    if not sys.stdin.isatty():
        return False
    print("Caching sudo credentials for cleanup (enter password)...")
    return subprocess.run(["sudo", "-v"], capture_output=True).returncode == 0


def _active_namespaces() -> Set[str]:
    """Namespaces vopono currently tracks as running."""
    try:
        result = subprocess.run(
            ["vopono", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()
        data = json.loads(result.stdout)
        return {app["namespace_id"] for app in data.get("applications", [])}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError):
        return set()


def cleanup_stale_namespaces() -> None:
    """Delete vo_c_* network namespaces that vopono is no longer tracking."""
    active = _active_namespaces()
    try:
        result = subprocess.run(
            ["ip", "netns", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        ns = parts[0]
        if not ns.startswith(INTERFACE_PREFIX) or ns in active:
            continue
        print(f"Deleting stale namespace: {ns}")
        r = subprocess.run(
            ["sudo", "-n", "ip", "netns", "delete", ns],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            print(
                f"  could not delete {ns}: {msg}\n"
                f"  Run manually: sudo ip netns delete {ns}",
                file=sys.stderr,
            )


def cleanup_vopono() -> None:
    """Full cleanup: orphaned interfaces + stale namespaces."""
    cleanup_vopono_interfaces()
    cleanup_stale_namespaces()


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """Kill the whole process group so openvpn (a child of vopono) is not orphaned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        proc.kill()


def run_vopono(
    profile: Path,
    application: str,
    verbose: bool = False,
    interactive: Optional[bool] = None,
) -> Tuple[bool, Optional[int], str]:
    cmd = ["vopono"]
    if verbose:
        cmd.append("-v")
    cmd.extend(["exec", "--custom", str(profile), "--protocol", "openvpn", application])

    print(f"Running: {shlex.join(cmd)}")
    if interactive is None:
        interactive = sys.stdin.isatty()

    try:
        if interactive:
            # Inherit the controlling terminal so TUI apps work.
            # No timeout: the app runs until the user quits; vopono's own
            # connect timeout fails bad profiles.
            proc = subprocess.Popen(cmd)
            rc = proc.wait()
            if rc == 0:
                return True, 0, ""
            return False, rc, ""
        else:
            # Batch mode: capture output and enforce a timeout, killing the
            # whole process group so openvpn does not survive as an orphan.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                proc.communicate()
                print(f"Command timed out after {TIMEOUT}s")
                return False, None, f"timed out after {TIMEOUT}s"
            if out:
                print(out)
            if err:
                print(err, file=sys.stderr)
            if proc.returncode == 0:
                return True, 0, ""
            return False, proc.returncode, (err or "") + (out or "")
    except FileNotFoundError:
        sys.exit("vopono is not installed or not on PATH")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="VPN rotator for testing a command inside a VPN namespace",
        epilog=(
            "The command defaults to 'papapopo' and is resolved via which.\n"
            "Use -- before the command if it has flags of its own:\n"
            "  revpn -- mytool --verbose --input x"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose vopono output")
    parser.add_argument("--reset", action="store_true", help="Reset daily usage tracking")
    parser.add_argument("--list", action="store_true", help="List available profiles")
    parser.add_argument("--list-failed", action="store_true", help="List failed profiles")
    parser.add_argument("--restore-failed", metavar="PROFILE", help="Move a failed profile back to active")
    parser.add_argument("--cleanup-only", action="store_true", help="Delete leftover vo_c_* interfaces and exit")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip cleanup of leftover vo_c_* interfaces")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Download VPN Gate profiles into vpn_profiles/ and exit",
    )
    parser.add_argument(
        "command",
        nargs="*",
        metavar="COMMAND",
        help=f"Command (and arguments) to run inside the VPN. Default: {DEFAULT_COMMAND}",
    )
    args = parser.parse_args()

    if args.fetch:
        import fetch_profiles

        fetch_profiles.download_profiles()
        return

    if args.list:
        profiles = get_available_profiles()
        print(f"Available profiles ({len(profiles)}):")
        for p in profiles:
            print(f"  {p.name}")
        return

    if args.list_failed:
        failed = get_failed_profiles()
        print(f"Failed profiles ({len(failed)}):")
        for p in failed:
            print(f"  {p.name}")
        return

    if args.restore_failed:
        src = FAILED_DIR / args.restore_failed
        if src.exists():
            shutil.move(str(src), str(VPN_PROFILES_DIR / args.restore_failed))
            print(f"Restored {args.restore_failed} to active profiles")
        else:
            print(f"Profile not found in failed: {args.restore_failed}")
        return

    if args.cleanup_only:
        cleanup_vopono()
        return

    state = load_state()
    if args.reset:
        state = {"used_today": [], "date": str(date.today())}
        save_state(state)
        print("Daily usage tracking reset")
        return

    state = reset_daily_state_if_needed(state)

    command = parse_command(args.command)
    resolved = resolve_executable(command)
    if resolved:
        command = [resolved, *command[1:]]
        print(f"Command: {shlex.join(command)}")
    else:
        print(
            f"Warning: {command[0]!r} not found on PATH, will retry with $(which {command[0]}) inside the namespace",
            file=sys.stderr,
        )

    if not args.no_cleanup:
        ensure_sudo()
        cleanup_vopono()

    try:
        while True:
            profile = select_next_profile(state)
            if not profile:
                print("No available VPN profiles")
                sys.exit(1)

            print(f"\nTrying profile: {profile.name}")
            success, returncode, output = run_vopono(profile, quote_command(command), args.verbose)

            if not args.no_cleanup:
                cleanup_vopono()

            if not success and looks_like_command_error(returncode, output):
                print(f"{command[0]!r} not found; retrying wrapped with $(which {command[0]})")
                success, returncode, output = run_vopono(profile, shell_wrap_command(command), args.verbose)
                if not args.no_cleanup:
                    cleanup_vopono()
                if not success and looks_like_command_error(returncode, output):
                    print(
                        f"Error: cannot find {command[0]!r} inside the VPN namespace.\n"
                        "Pass an absolute path instead, e.g.:\n"
                        f"  revpn -- {Path(command[0]).expanduser().absolute()} <args>",
                        file=sys.stderr,
                    )
                    print(f"Not quarantining {profile.name}: this is a command problem, not a VPN problem.", file=sys.stderr)
                    sys.exit(1)

            if success:
                state["used_today"].append(profile.name)
                save_state(state)
                print("Success! Profile marked as used today.")
                break
            else:
                print(f"Profile {profile.name} failed, moving to failed folder...")
                move_to_failed(profile)
                state = load_state()
                state = reset_daily_state_if_needed(state)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        if not args.no_cleanup:
            cleanup_vopono()
        sys.exit(130)
    finally:
        if not args.no_cleanup:
            cleanup_vopono()


if __name__ == "__main__":
    main()
