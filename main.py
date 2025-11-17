#!/usr/bin/env python3
"""
VPN Rotator for testing a command inside a VPN namespace.
- Rotates through VPNGate profiles (no repeat per day)
- Auto failover on connection failure
- Moves failed profiles to failed/ folder
"""

import json
import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

VPN_PROFILES_DIR = Path("vpn_profiles")
FAILED_DIR = Path("vpn_profiles_failed")
STATE_FILE = Path("vpn_state.json")
DEFAULT_COMMAND = "papapopo"
TIMEOUT = 30


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


def run_vopono(profile: Path, application: str) -> Tuple[bool, Optional[int]]:
    cmd = ["vopono", "exec", "--custom", str(profile), "--protocol", "openvpn", application]
    print(f"Running: {shlex.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        print(f"Command timed out after {TIMEOUT}s")
        return False, None
    print(out, end="")
    if err:
        print(err, end="", file=sys.stderr)
    return proc.returncode == 0, proc.returncode


def main() -> None:
    state = reset_daily_state_if_needed(load_state())
    while True:
        profile = select_next_profile(state)
        if not profile:
            print("No available VPN profiles")
            sys.exit(1)

        print(f"\nTrying profile: {profile.name}")
        success, _ = run_vopono(profile, DEFAULT_COMMAND)
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
