#!/usr/bin/env python3
"""
Download OpenVPN profiles from VPN Gate into vpn_profiles/ so main.py can use them.

Source endpoint: https://www.vpngate.net/api/iphone/

The response is NOT JSON. It is a small custom text format:
    *vpn_servers                      <- marker line, ignore
    #HostName,IP,Score,...            <- commented header, ignore
    public-vpn-41,219.100.37.5,...    <- one comma-separated row per server

Each data row has 15 fields. The ones we care about are near the front:
    [0] HostName      e.g. public-vpn-41
    [1] IP            e.g. 219.100.37.5
    [3] Ping (ms)
    [4] Speed (bytes/s)
    [6] CountryShort  e.g. JP
    [14] OpenVPN_ConfigData_Base64    <- base64 of a full .ovpn

Base64 never contains commas, so the LAST field is always the config and is
safe to take with split(',')[-1], even if the Message field (index 13) happens
to contain commas.

Usage:
    python fetch_profiles.py                 # download all, skip duplicates
    python fetch_profiles.py --limit 10      # only grab 10 new ones
    python fetch_profiles.py --out /tmp/vg   # write elsewhere
    python fetch_profiles.py --force         # ignore dedup and rewrite
"""

import argparse
import base64
import binascii
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

API_URL = "https://www.vpngate.net/api/iphone/"
BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "vpn_profiles"
FETCH_TIMEOUT = 30
# Some endpoints reject non-browser user agents; VPN Gate is lenient but this
# keeps us on the safe side.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_lines(url: str) -> List[str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    return text.splitlines()


def parse_row(line: str) -> Optional[Dict[str, str]]:
    """Turn one data row into metadata + decoded config, or None if unusable."""
    parts = line.split(",")
    if len(parts) < 15:
        return None

    b64 = parts[-1].strip()
    if not b64:
        return None
    try:
        config = base64.b64decode(b64).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        return None
    if "remote " not in config:
        return None

    return {
        "hostname": parts[0].strip(),
        "ip": parts[1].strip(),
        "ping": parts[3].strip(),
        "speed": parts[4].strip(),
        "country": parts[6].strip(),
        "config": config,
    }


def remote_host_port(config: str) -> Optional[Tuple[str, str]]:
    """First `remote <host> <port>` line from a config."""
    for line in config.splitlines():
        if line.startswith("remote "):
            toks = line.split()
            if len(toks) >= 3:
                return toks[1], toks[2]
    return None


def proto_of(config: str) -> str:
    """First `proto <tcp|udp>` line, defaulting to tcp."""
    for line in config.splitlines():
        if line.startswith("proto "):
            toks = line.split()
            if len(toks) >= 2:
                return toks[1].strip()
    return "tcp"


def existing_remotes(directory: Path) -> Set[str]:
    """Collect the remote hosts already present in a directory of .ovpn files."""
    found: Set[str] = set()
    if not directory.exists():
        return found
    for ovpn in directory.glob("*.ovpn"):
        try:
            text = ovpn.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("remote "):
                toks = line.split()
                if len(toks) >= 2:
                    found.add(toks[1])
    return found


def download_profiles(out_dir=None, force=False, limit=0) -> None:
    """Download VPN Gate profiles into out_dir (default: vpn_profiles/)."""
    out_dir = Path(out_dir) if out_dir else PROFILES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {API_URL} ...")
    try:
        lines = fetch_lines(API_URL)
    except Exception as e:  # noqa: BLE001 - surface any fetch error cleanly
        sys.exit(f"Failed to fetch VPN Gate list: {e}")

    known_remotes = existing_remotes(out_dir)

    downloaded = skipped = failed = 0
    for line in lines:
        if not line or line.startswith("*") or line.startswith("#"):
            continue
        if limit and downloaded >= limit:
            break

        row = parse_row(line)
        if not row:
            failed += 1
            continue

        hostport = remote_host_port(row["config"])
        if not hostport:
            failed += 1
            continue
        host, port = hostport
        proto = proto_of(row["config"])

        if not force and host in known_remotes:
            skipped += 1
            continue

        filename = f"vpngate_{row['hostname']}_{proto}_{port}.ovpn"
        dest = out_dir / filename
        if dest.exists() and not force:
            skipped += 1
            continue

        try:
            dest.write_text(row["config"])
        except OSError as e:
            print(f"  ! could not write {filename}: {e}", file=sys.stderr)
            failed += 1
            continue

        known_remotes.add(host)
        downloaded += 1
        print(
            f"  + {filename}  "
            f"({row['country']}, ping {row['ping']}ms, speed {row['speed']})"
        )

    print(
        f"\nDone. downloaded={downloaded} skipped={skipped} failed={failed}\n"
        f"Profiles in {out_dir}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Download OpenVPN profiles from VPN Gate into vpn_profiles/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        default=str(PROFILES_DIR),
        help=f"Directory to write .ovpn files into (default: {PROFILES_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore dedup and re-download servers already present",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Stop after downloading N new profiles (0 = no limit)",
    )
    args = parser.parse_args()

    download_profiles(out_dir=args.out, force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
