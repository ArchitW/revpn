# revpn — VPN Rotator

Rotate through free [VPN Gate](https://www.vpngate.net/) OpenVPN profiles to run a command inside a VPN namespace on Linux. Profiles are tried one at a time; failures are quarantined to a `vpn_profiles_failed/` folder, and successful profiles are marked as used so you don't repeat them on the same day.

Built on [`vopono`](https://github.com/jamesmcm/vopono), which runs the command in an isolated network namespace routed through the VPN.

## Requirements

- Linux with `ip` (iproute2), `sudo`, and `openvpn`
- [`vopono`](https://github.com/jamesmcm/vopono) on `PATH`
- Python 3 (standard library only, no pip dependencies)
- Passwordless or cached `sudo` credentials for cleanup of orphaned interfaces/namespaces (`sudo -n` is used; `sudo -v` is attempted once on a TTY)

## Install as a global command (recommended)

Symlink the script into a directory on your `PATH` (e.g. `~/.local/bin`):

```bash
ln -sf "$(pwd)/main.py" ~/.local/bin/revpn
```

Then call it from **any** directory, like any other command:

```bash
cd ~/some-project
revpn 'firefox'
revpn --list
revpn --fetch
```

`revpn` resolves `vpn_profiles/`, `vpn_profiles_failed/`, and `vpn_state.json` **relative to the repository** (not your current directory), so the profile pool and state are shared no matter where you invoke it from. The command you run (firefox, etc.) executes in the directory you invoke from.

If you'd rather not install a global command, run `python3 main.py ...` from the repository root instead — everything below applies to both.

## Quick start

```bash
# 1. Download VPN Gate profiles into vpn_profiles/
revpn --fetch            # or: python3 fetch_profiles.py

# 2. Run the default command (papapopo) inside the VPN
revpn                    # or: python3 main.py

# 3. Run your own command (runs in the current directory)
revpn 'firefox'
revpn -- mytool --flag value
```

## Downloading profiles

`revpn --fetch` (or `python fetch_profiles.py`) fetches the VPN Gate server list from `https://www.vpngate.net/api/iphone/`, decodes the base64 OpenVPN configs, and writes them to `vpn_profiles/` as `vpngate_<hostname>_<proto>_<port>.ovpn`.

```bash
revpn --fetch                        # download all, skip duplicates
python fetch_profiles.py --limit 10  # only grab 10 new profiles
python fetch_profiles.py --out /tmp/vg   # write elsewhere
python fetch_profiles.py --force     # ignore dedup and rewrite
```

`revpn --fetch` downloads all new profiles with default settings. Use `python fetch_profiles.py` directly when you want `--limit`, `--out`, or `--force`. Dedup is based on the `remote` host already present in existing `.ovpn` files. Rows whose configs can't be decoded or lack a `remote` line are skipped and counted as failed.

## `main.py` / `revpn` — run a command through rotating VPNs

```bash
revpn [options] [--] [COMMAND ...]
```

Behavior:

- Picks the next unused profile from `vpn_profiles/` (each profile is used at most once per calendar day; the counter resets automatically or with `--reset`).
- Runs `vopono exec --custom <profile> --protocol openvpn <command>`.
- On failure (non-zero exit / timeout), the profile is moved to `vpn_profiles_failed/` and the next profile is tried.
- On success, the profile is recorded in `vpn_state.json` as used today.
- Cleans up orphaned `vo_c_*` interfaces and stale network namespaces before and after each run.

### Command resolution

The command is handed to `vopono exec` as a single shell-split string (`shlex.join`). The executable is looked up with `shutil.which()` first, because vopono launches through `sudo --set-home`, whose `PATH` may not include your user's tool directories. If vopono still reports "command not found", the command is retried wrapped in `sh -c 'exec "$(command -v ...)" ...'`. A command resolution problem never quarantines a profile — the script exits with an error instead.

The command can be passed as separate words (`revpn mytool --flag`) or one quoted string (`revpn "mytool --flag"`). Default: `papapopo`.

### Interactive vs batch mode

- **Interactive (stdin is a TTY):** the app inherits the terminal; no timeout — it runs until you quit. Suitable for TUI apps.
- **Batch:** stdout/stderr are captured, a 30-second timeout is enforced, and the whole process group is killed on timeout so `openvpn` is not orphaned.

### Options

| Flag | Description |
|---|---|
| `-v`, `--verbose` | Verbose vopono output |
| `--fetch` | Download VPN Gate profiles into `vpn_profiles/` and exit |
| `--reset` | Reset daily usage tracking |
| `--list` | List available profiles |
| `--list-failed` | List failed (quarantined) profiles |
| `--restore-failed PROFILE` | Move a failed profile back to active |
| `--cleanup-only` | Delete leftover `vo_c_*` interfaces/namespaces and exit |
| `--no-cleanup` | Skip cleanup of leftover interfaces/namespaces |
| `-h`, `--help` | Show help |

Exit codes: `0` on success, `1` if no profiles are available or the command can't be found, `130` on Ctrl-C.

## Files

| Path | Purpose |
|---|---|
| `vpn_profiles/` | Active `.ovpn` profiles (downloaded by `fetch_profiles.py`) |
| `vpn_profiles_failed/` | Quarantined profiles that failed to connect/run |
| `vpn_state.json` | Per-day usage tracking (`used_today` + `date`) |
| `commands` | Scratch notes with example invocations (not used by the scripts) |

## Examples

```bash
# See what we have
revpn --list
revpn --list-failed

# Restore a quarantined profile
revpn --restore-failed vpngate_vpn718282215.opengw.net_tcp_995.ovpn

# Re-download fresh profiles
revpn --fetch

# Clean up stray interfaces/namespaces without running anything
revpn --cleanup-only
```

## Notes & caveats

- VPN Gate is a free community service — expect many profiles to be slow or dead. The rotator handles this by quarantining failures.
- Failed profiles accumulate in `vpn_profiles_failed/`; periodically review them (`--list-failed`) or restore ones worth retrying.
- Usage tracking is per calendar day and stored in `vpn_state.json`; delete or `--reset` it to start over.
- Profile data (`vpn_profiles/`, `vpn_profiles_failed/`, `vpn_state.json`) lives next to the scripts, so it is shared regardless of where you run `revpn` from. These paths are git-ignored.
- If you move the repository, re-run the `ln -sf` command so the `revpn` symlink points at the new location.
- `commands` is a personal scratch file and not part of the tooling.