#!/usr/bin/env python3
"""
VPN Rotator for testing a command inside a VPN namespace.
- Rotates through VPNGate profiles (no repeat per day)
- Auto failover on connection failure
- Moves failed profiles to failed/ folder

The command is handed to `vopono exec` as a single shell-split string, so you
can pass arguments too:
    python main.py -- mytool --flag value

The executable is looked up with shutil.which() first - the same thing as
wrapping it in "$(which __COMMAND__)" - because vopono launches it through
`sudo --set-home`, whose PATH may not include your user's tool directories.
If vopono still reports "command not found", the command is retried wrapped in
`sh -c 'exec "$(command -v ...)" ...'`, and a command problem never burns a
profile (it exits instead of moving the profile to vpn_profiles_failed).
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

VPN_PROFILES_DIR = Path("vpn_profiles")
FAILED_DIR = Path("vpn_profiles_failed")
STATE_FILE = Path("vpn_state.json")
DEFAULT_COMMAND = "papapopo"
TIMEOUT = 30

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


def main() -> None:
    state = reset_daily_state_if_needed(load_state())
    command = parse_command(sys.argv[1:])
    resolved = resolve_executable(command)
    if resolved:
        command = [resolved, *command[1:]]
        print(f"Command: {shlex.join(command)}")
    else:
        print(
            f"Warning: {command[0]!r} not found on PATH, will retry with $(which {command[0]}) inside the namespace",
            file=sys.stderr,
        )

    while True:
        profile = select_next_profile(state)
        if not profile:
            print("No available VPN profiles")
            sys.exit(1)

        print(f"\nTrying profile: {profile.name}")
        success, returncode, output = run_vopono(profile, quote_command(command))

        if not success and looks_like_command_error(returncode, output):
            print(f"{command[0]!r} not found; retrying wrapped with $(which {command[0]})")
            success, returncode, output = run_vopono(profile, shell_wrap_command(command))
            if not success and looks_like_command_error(returncode, output):
                print(
                    f"Error: cannot find {command[0]!r} inside the VPN namespace.\n"
                    "Pass an absolute path instead, e.g.:\n"
                    f"  python main.py -- {Path(command[0]).expanduser().absolute()} <args>",
                    file=sys.stderr,
                )
                print(f"Not quarantining {profile.name}: this is a command problem, not a VPN problem.", file=sys.stderr)
                sys.exit(1)

        if success:
            state["used_today"].append(profile.name)
            save_state(state)
            print("Success! Profile marked as used today.")
            break
        print(f"Profile {profile.name} failed, moving to failed folder...")
        move_to_failed(profile)
        state = reset_daily_state_if_needed(load_state())


if __name__ == "__main__":
    main()
