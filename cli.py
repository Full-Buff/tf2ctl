#!/usr/bin/env python3
import os
import sys
import json
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, UTC  # timezone-aware UTC
from json import JSONDecodeError

# Try package-relative, then local
try:
    from tf2ctl.do_api import DigitalOceanAPI, DOAPIError
    from tf2ctl.linode_api import LinodeAPI, LinodeAPIError
    from tf2ctl.vultr_api import VultrAPI, VultrAPIError
    from tf2ctl.ssh_ops import SSHOps
except ImportError:
    from do_api import DigitalOceanAPI, DOAPIError
    from linode_api import LinodeAPI, LinodeAPIError
    from vultr_api import VultrAPI, VultrAPIError
    from ssh_ops import SSHOps

# Treat this folder as the project root
PROJECT_ROOT = Path(__file__).resolve().parent

# Self-contained state under the project directory
CONFIG_DIR = PROJECT_ROOT / ".tf2ctl"
CONFIG_PATH = CONFIG_DIR / "config.json"
SERVERS_REG_PATH = CONFIG_DIR / "servers.json"
LOGS_DIR = CONFIG_DIR / "logs"

# server_resources lives INSIDE the project directory
SERVER_RESOURCES_DIR = PROJECT_ROOT / "server_resources"

DEFAULT_TAG = "tf2ctl"

# Default ports (image uses host networking)
GAME_PORT = 27015
STV_PORT = 27020

SUPPORTED_PROVIDERS = {
    "digitalocean": "DigitalOcean",
    "linode": "Linode",
    "vultr": "Vultr",
}

# ---------------------------
# Config / Registry
# ---------------------------

def load_config() -> dict:
    CONFIG_DIR.mkdir(exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, JSONDecodeError) as exc:
            print(f"(warning) could not read config: {exc}; recreating default")
    data = {
        "provider": "digitalocean",
        "do_token": "",
        "linode_token": "",
        "vultr_token": "",
        "ssh_private_key": "",
        "ssh_public_key": "",
        "ssh_private_key_path": "",
        "ssh_public_key_path": "",
    }
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data

def save_config(cfg: dict):
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

def load_registry() -> Dict[str, Any]:
    if SERVERS_REG_PATH.exists():
        try:
            return json.loads(SERVERS_REG_PATH.read_text(encoding="utf-8"))
        except (OSError, JSONDecodeError) as exc:
            print(f"(warning) could not read servers registry: {exc}; starting empty")
    return {}

def save_registry(reg: Dict[str, Any]):
    CONFIG_DIR.mkdir(exist_ok=True)
    SERVERS_REG_PATH.write_text(json.dumps(reg, indent=2), encoding="utf-8")


# ---------------------------
# Helpers / UI
# ---------------------------

def pause():
    input("\nPress Enter to continue...")

def ask(prompt: str, default: Optional[str] = None) -> str:
    sfx = f" [{default}]" if default else ""
    val = input(f"{prompt}{sfx}: ").strip()
    return val or (default or "")

def select_provider(cfg: dict) -> str:
    print("\nChoose cloud provider:")
    keys = list(SUPPORTED_PROVIDERS.keys())
    for i, k in enumerate(keys, 1):
        print(f"{i}) {SUPPORTED_PROVIDERS[k]} ({k})")
    idx = ask("Number", "1")
    try:
        i = int(idx) - 1
        if 0 <= i < len(keys):
            cfg["provider"] = keys[i]
            save_config(cfg)
            return keys[i]
    except (ValueError, IndexError):
        pass
    print("Invalid selection; keeping current.")
    return cfg.get("provider", "digitalocean")

def ensure_token_for_provider(cfg: dict, provider: str) -> str:
    key_map = {
        "digitalocean": "do_token",
        "linode": "linode_token",
        "vultr": "vultr_token",
    }
    key = key_map.get(provider, "do_token")
    if cfg.get(key):
        return cfg[key]
    print(f"\nNo API token set for {SUPPORTED_PROVIDERS.get(provider, provider)}.")
    token = ask("Paste your API Token")
    cfg[key] = token
    save_config(cfg)
    return token

def build_api(cfg: dict):
    provider = cfg.get("provider", "digitalocean")
    token = ensure_token_for_provider(cfg, provider)
    if provider == "digitalocean":
        return DigitalOceanAPI(token)
    if provider == "linode":
        return LinodeAPI(token)
    if provider == "vultr":
        return VultrAPI(token)
    raise RuntimeError(f"Unsupported provider '{provider}'")

def _harden_private_key_permissions(priv_path: Path):
    """
    Tighten key permissions for OpenSSH (Windows: NTFS ACLs; POSIX: 600).
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or os.getlogin()
            subprocess.run(f'icacls "{priv_path}" /inheritance:r', shell=True, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(f'icacls "{priv_path}" /grant:r {user}:R', shell=True, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(f'icacls "{priv_path}" /remove:g Users', shell=True, check=False,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            os.chmod(priv_path, 0o600)
    except (subprocess.CalledProcessError, PermissionError, OSError) as exc:
        print(f"(warning) could not tighten key permissions: {exc}")

def _write_key_files(cfg: dict):
    """
    Ensure the private/public key are also written to files inside .tf2ctl,
    so we can launch a real `ssh` session. Also harden permissions (Windows ACLs).
    """
    priv = cfg.get("ssh_private_key", "")
    pub  = cfg.get("ssh_public_key", "")
    if not priv or not pub:
        return
    priv_path = CONFIG_DIR / "id_ed25519"
    pub_path  = CONFIG_DIR / "id_ed25519.pub"

    if not priv_path.exists():
        priv_path.write_text(priv)
    if not pub_path.exists():
        pub_path.write_text(pub)

    _harden_private_key_permissions(priv_path)

    cfg["ssh_private_key_path"] = str(priv_path)
    cfg["ssh_public_key_path"] = str(pub_path)
    save_config(cfg)

def ensure_ssh_key(cfg: dict) -> tuple[str, str]:
    if cfg.get("ssh_private_key") and cfg.get("ssh_public_key"):
        _write_key_files(cfg)
        return cfg["ssh_private_key"], cfg["ssh_public_key"]
    print("\nNo SSH key found.")
    choice = ask("Generate a new Ed25519 key? (y/n)", "y").lower()
    if choice.startswith("y"):
        priv, pub = SSHOps.generate_ed25519_keypair(comment="tf2ctl@local")
        cfg["ssh_private_key"] = priv
        cfg["ssh_public_key"] = pub
        save_config(cfg)
        _write_key_files(cfg)
        print("Generated and stored a new SSH keypair.")
        return priv, pub

    priv_path = ask("Path to your PRIVATE key (PEM/OpenSSH)")
    pub_path  = ask("Path to your PUBLIC key (.pub)")
    priv = Path(priv_path).read_text(encoding="utf-8")
    pub  = Path(pub_path).read_text(encoding="utf-8")
    cfg["ssh_private_key"] = priv
    cfg["ssh_public_key"]  = pub
    save_config(cfg)
    _write_key_files(cfg)
    print("Stored your SSH keypair.")
    return priv, pub

def pick_region(api) -> str:
    regions = api.list_regions()
    if not regions:
        print("No regions available.")
        return ""
    print("\nAvailable Regions:")
    for i, r in enumerate(regions, 1):
        slug = r.get("slug") or r.get("id") or r.get("region") or ""
        name = r.get("name") or r.get("label") or slug
        print(f"{i}) {slug} - {name}")
    idx = int(ask("Select region number", "1"))
    chosen = regions[idx-1]
    return chosen.get("slug") or chosen.get("id")

def pick_size(api) -> str:
    print("\nRecommended sizes:")
    sizes = api.recommended_sizes()
    keys = list(sizes.keys())
    for i, k in enumerate(keys, 1):
        print(f"{i}) {k} -> {sizes[k]}")
    idx = int(ask("Select size number", "2"))
    return sizes[keys[idx-1]]

def _name_series(prefix: str, start: int, count: int) -> list[str]:
    width = len(str(start + count))
    return [f"{prefix}-{i:0{width}d}" for i in range(start, start + count)]

def _choose_server_by_name(reg: Dict[str, Any]) -> Optional[str]:
    if not reg:
        print("No known servers (registry is empty).")
        return None
    names = sorted(reg.keys())
    print("\nSelect a server:")
    for i, n in enumerate(names, 1):
        meta = reg[n]
        ip = meta.get("ip", "")
        did = meta.get("id", "")
        prov = meta.get("provider", "")
        print(f"{i}) {n:20s}  ip={ip:15s}  id={did}  [{prov}]")
    idx = ask("Number", "1")
    try:
        idx = int(idx)
        if 1 <= idx <= len(names):
            return names[idx-1]
    except (ValueError, IndexError):
        pass
    print("Invalid selection.")
    return None

def _ensure_ip_for(reg: Dict[str, Any], name: str, api) -> Optional[str]:
    meta = reg.get(name, {})
    ip = meta.get("ip")
    if ip:
        return ip
    sid = meta.get("id")
    if not sid:
        return None
    info = api.wait_for_active_ip(sid)
    ip = info.get("ip", "")
    if ip:
        meta["ip"] = ip
        reg[name] = meta
        save_registry(reg)
    return ip

def _print_summary_line(name: str, item: Dict[str, Any]):
    ip = item.get("ip", "")
    did = item.get("id", "")
    prov = item.get("provider", "")
    print(f"- {name:16s} id={did} ip={ip:15s} [{prov}]")

def _build_conn_strings(ip: str, game_port: int, stv_port: int, sv_password: str, rcon_password: str) -> Tuple[str, str, str]:
    game = f'connect {ip}:{game_port}; password "{sv_password}"' if sv_password else f'connect {ip}:{game_port}'
    stv  = f'connect {ip}:{stv_port}; password "stv"'
    rcon = f'rcon_address {ip}:{game_port}; rcon_password "{rcon_password}"'
    return game, stv, rcon


# ---------------------------
# Bulk actions
# ---------------------------

def _bulk_loop(reg: Dict[str, Any], api, cfg: dict):
    # pylint: disable=too-many-branches,too-many-statements,too-many-locals,too-many-nested-blocks
    if not reg:
        print("No servers tracked by this tool.")
        pause()
        return
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print("=== Bulk actions ===")
        print("1) Restart TF2 container on all servers")
        print("2) Run a command on all servers")
        print("3) Delete ALL servers (created by this tool)")
        print("4) Show ALL connection strings")
        print("5) Export ALL connection strings to file")
        print("6) Reapply includes (fast) on all servers")
        print("7) Back")
        sub = ask("Choose", "1")

        if sub == "1":
            priv, _ = ensure_ssh_key(cfg)
            for name in sorted(reg.keys()):
                ip = _ensure_ip_for(reg, name, api)
                if not ip:
                    print(f"{name}: no IP yet, skipping.")
                    continue
                rc, _out, err = SSHOps.run_command(ip, "root", priv, "docker restart tf2")
                print(f"{name}: {'ok' if rc == 0 else 'failed'}")
                if rc != 0:
                    print(err)
            pause()

        elif sub == "2":
            cmd = ask("Command to run", "docker ps")
            priv, _ = ensure_ssh_key(cfg)
            for name in sorted(reg.keys()):
                ip = _ensure_ip_for(reg, name, api)
                if not ip:
                    print(f"{name}: no IP yet, skipping.")
                    continue
                rc, out, err = SSHOps.run_command(ip, "root", priv, cmd)
                print(f"\n=== {name} ({ip}) exit {rc} ===")
                print(out if out else err)
            pause()

        elif sub == "3":
            confirm = ask("Type DELETE ALL to confirm deleting every server", "")
            if confirm != "DELETE ALL":
                print("Cancelled.")
                pause()
                continue
            names = list(reg.keys())
            for name in names:
                meta = reg[name]
                try:
                    api.delete_server(meta["id"])
                    print(f"Deleted {name}")
                except (DOAPIError, LinodeAPIError, VultrAPIError) as exc:
                    print(f"Failed to delete {name}: {exc}")
                reg.pop(name, None)
                meta_path = CONFIG_DIR / f"{meta.get('id','')}.json"
                if meta_path.exists():
                    try:
                        meta_path.unlink()
                    except (OSError, PermissionError, FileNotFoundError) as exc:
                        print(f"Failed to delete config for {name}: {exc}")
            save_registry(reg)
            pause()

        elif sub == "4":
            for name in sorted(reg.keys()):
                m = reg[name]
                ip = _ensure_ip_for(reg, name, api)
                if not ip:
                    print(f"{name}: no IP.")
                    continue
                g, s, r = _build_conn_strings(ip, GAME_PORT, STV_PORT, m.get("sv_password",""), m.get("rcon_password",""))
                print(f"\n{name}")
                print(f"  GAME: {g}")
                print(f"  STV : {s}")
                print(f"  RCON: {r}")
            pause()

        elif sub == "5":
            out_path = CONFIG_DIR / "connection_strings.txt"
            lines = []
            for name in sorted(reg.keys()):
                m = reg[name]
                ip = _ensure_ip_for(reg, name, api)
                if not ip:
                    continue
                g, s, r = _build_conn_strings(ip, GAME_PORT, STV_PORT, m.get("sv_password",""), m.get("rcon_password",""))
                lines.append(f"[{name}]")
                lines.append(f"GAME: {g}")
                lines.append(f"STV : {s}")
                lines.append(f"RCON: {r}")
                lines.append("")
            out_path.write_text("\n".join(lines))
            print(f"Saved to {out_path}")
            pause()

        elif sub == "6":
            priv, _ = ensure_ssh_key(cfg)
            for name in sorted(reg.keys()):
                ip = _ensure_ip_for(reg, name, api)
                if not ip:
                    print(f"{name}: no IP yet, skipping.")
                    continue
                rc, out, err = SSHOps.run_command(ip, "root", priv, "bash /root/tf2-copy.sh")
                print(f"{name}: {'applied' if rc == 0 else 'failed'}")
                if rc != 0:
                    print(err)
            pause()

        elif sub == "7":
            break
        else:
            pause()


# ---------------------------
# Main Menu
# ---------------------------

def menu():
    # pylint: disable=too-many-branches,too-many-statements,too-many-locals,too-many-nested-blocks
    cfg = load_config()
    api = build_api(cfg)
    #ssh = SSHOps()

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print("=== TF2 Server CLI ===")
        print(f"[Provider: {SUPPORTED_PROVIDERS.get(cfg.get('provider','digitalocean'))}]")
        print("1) Configure provider / API token / SSH key")
        print("2) Create server(s) (auto-configure)")
        print("3) Manage a server")
        print("4) List your servers")
        print("5) Bulk actions")
        print("6) Quit")
        choice = ask("Choose", "4")

        if choice == "1":
            prov = select_provider(cfg)
            # Rebuild API for the chosen provider
            api = build_api(cfg)
            priv, pub = ensure_ssh_key(cfg)
            try:
                key_id = api.ensure_ssh_key(pub)
                print(f"SSH key registered with {SUPPORTED_PROVIDERS.get(prov)} (id: {key_id}).")
            except (DOAPIError, LinodeAPIError, VultrAPIError) as e:
                print(f"(warning) could not register SSH key with provider: {e}")
            print(f"Config stored at: {CONFIG_PATH}")
            print(f"server_resources path: {SERVER_RESOURCES_DIR}")
            pause()

        elif choice == "2":
            # Create MANY and auto-configure
            api = build_api(cfg)  # ensure token/provider are current
            priv, pub = ensure_ssh_key(cfg)

            if not SERVER_RESOURCES_DIR.exists():
                print(f"Expected server_resources at: {SERVER_RESOURCES_DIR}")
                print("Create it with scripts/setup.sh and any includes/ you want uploaded.")
                pause()
                continue

            prefix = ask("Name prefix", "tf2")
            start_num = int(ask("Start number", "1"))
            count_req = int(ask("How many servers to create?", "1"))

            region = pick_region(api)
            size   = pick_size(api)
            start_map = ask("Start map for all servers", "cp_badlands")

            # ---------- Provider capacity (may be None if provider doesn't expose it) ----------
            remaining = None
            try:
                remaining = api.capacity_remaining()
            except (DOAPIError, LinodeAPIError, VultrAPIError):
                remaining = None

            if remaining is not None and count_req > remaining:
                if remaining <= 0:
                    print("\nYour account cannot create more servers right now (0 capacity remaining).")
                    print("Tip: delete old instances or request a limit increase from your provider.")
                    pause()
                    continue
                print(f"\nRequested {count_req} servers but your account can create only {remaining} more right now.")
                use = ask(f"Create {remaining} servers instead?", "yes")
                if use.lower() != "yes":
                    print("Cancelled.")
                    pause()
                    continue
                count = remaining
            else:
                count = count_req

            names = _name_series(prefix, start_num, count)

            print("\nWill create:")
            for n in names:
                print(f"  - {n} ({region}, {size})")
            if ask("Type 'yes' to confirm", "yes").lower() != "yes":
                pause()
                continue

            reg = load_registry()
            created = []
            failed = []

            # 1) Create instances (1s delay between calls)
            for n in names:
                print(f"\nCreating server for {n}...")
                try:
                    server = api.create_server(
                        name=n,
                        region=region,
                        size=size,
                        ssh_key_id=api.ensure_ssh_key(pub),
                        public_key=pub,
                        tags=[DEFAULT_TAG, f"tf2-{n}"]
                    )
                except (DOAPIError, LinodeAPIError, VultrAPIError) as e:
                    print(f"  -> create failed: {e}")
                    # If message indicates limit/quota, stop bulk creation
                    msg = (str(e) or "").lower()
                    if "limit" in msg or "quota" in msg:
                        print("It looks like you've reached an account limit. Stopping bulk create.")
                        break
                    failed.append(n)
                    continue

                sid = server.get("id")
                status = server.get("status", "")
                print(f"  -> created id={sid} status={status}")
                meta = {
                    "provider": cfg.get("provider", "digitalocean"),
                    "id": sid,
                    "ip": "",
                    "region": region,
                    "size": size,
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "hostname": n,
                    "rcon_password": SSHOps.random_password(16),
                    "sv_password": SSHOps.random_password(12),
                    "stv_password": "stv",
                    "start_map": start_map
                }
                reg[n] = meta
                (CONFIG_DIR / f"{sid}.json").write_text(json.dumps({
                    "hostname": meta["hostname"],
                    "rcon_password": meta["rcon_password"],
                    "sv_password": meta["sv_password"],
                    "stv_password": meta["stv_password"],
                    "start_map": meta["start_map"]
                }, indent=2))
                save_registry(reg)
                created.append(n)

                time.sleep(1.0)

            if not created:
                print("\nNo servers were created.")
                pause()
                continue

            # 2) Wait for IPs
            print("\nWaiting for servers to become active and get IPs...")
            for n in created:
                m = reg[n]
                info = api.wait_for_active_ip(m["id"])
                ip = info.get("ip", "")
                m["ip"] = ip
                reg[n] = m
                save_registry(reg)
                print(f"{n} ({m['id']}) -> IP: {ip or 'pending'}")

            # 3) Configure each (upload resources, run setup.sh, copy into container)
            print("\nConfiguring servers (uploading resources, running setup.sh, copying includes into container)...")
            for idx, n in enumerate(created, 1):
                m = reg[n]
                ip = m.get("ip")
                if not ip:
                    print(f"[{idx}/{len(created)}] {n}: No IP yet—skipping.")
                    continue
                print(f"[{idx}/{len(created)}] {n} ({ip}) configuring...")
                ok = SSHOps.configure_server(
                    host=ip,
                    user="root",
                    private_key=cfg["ssh_private_key"],
                    server_resources=SERVER_RESOURCES_DIR,
                    substitutions={
                        "SERVER_HOSTNAME": m["hostname"],
                        "RCON_PASSWORD": m["rcon_password"],
                        "SERVER_PASSWORD": m["sv_password"],
                        "START_MAP": m["start_map"],
                        "STV_PASSWORD": m["stv_password"],
                    },
                    logs_dir=LOGS_DIR,
                    log_filename=f"{n}-{m['id']}.log"
                )
                print("  -> Success." if ok else "  -> Failed. See log in .tf2ctl/logs/")

            print("\nSummary:")
            for n in created:
                m = reg[n]
                print(f"- {n:16s} id={m['id']} ip={m.get('ip',''):15s} "
                      f"rcon={m['rcon_password']} join={m['sv_password']} stv={m['stv_password']} "
                      f"[{m.get('provider')}]")
            if failed:
                print("\nFailed to create:")
                for n in failed:
                    print(f"- {n}")
            print(f"\nPer-server configs saved under: {CONFIG_DIR}")
            pause()

        elif choice == "3":
            reg = load_registry()
            name = _choose_server_by_name(reg)
            if not name:
                pause()
                continue
            m = reg[name]
            api = build_api(cfg)  # in case provider switched
            ip = _ensure_ip_for(reg, name, api)
            if not ip:
                print("No IP available yet.")
                pause()
                continue

            while True:
                os.system("cls" if os.name == "nt" else "clear")
                print(f"=== Manage: {name} ===")
                print(f"ID: {m.get('id')}  IP: {ip}  Region: {m.get('region')}  Size: {m.get('size')}  Provider: {m.get('provider')}")
                print("1) Show TF2 container logs")
                print("2) Restart TF2 container")
                print("3) Run a command (SSH)")
                print("4) Re-configure (upload & run setup.sh)")
                print("5) Open SSH session")
                print("6) Show connection strings")
                print("7) Reapply includes (fast)")
                print("8) Delete this server")
                print("9) Back")
                sub = ask("Choose", "1")

                if sub == "1":
                    priv, _ = ensure_ssh_key(cfg)
                    print("\nLatest TF2 container logs (last 200 lines):\n")
                    print(SSHOps.get_container_logs(host=ip, user="root", private_key=priv, container="tf2", tail=200))
                    pause()

                elif sub == "2":
                    priv, _ = ensure_ssh_key(cfg)
                    rc, out, err = SSHOps.run_command(ip, "root", priv, "docker restart tf2")
                    print(out or err or f"(exit {rc})")
                    pause()

                elif sub == "3":
                    cmd = ask("Command to run on server", "docker ps")
                    priv, _ = ensure_ssh_key(cfg)
                    rc, out, err = SSHOps.run_command(ip, "root", priv, cmd)
                    print(out if out else err)
                    print(f"(exit {rc})")
                    pause()

                elif sub == "4":
                    print(f"Uploading resources from: {SERVER_RESOURCES_DIR}")
                    if not SERVER_RESOURCES_DIR.exists():
                        print(f"Expected server_resources at: {SERVER_RESOURCES_DIR}")
                        pause()
                        continue
                    priv, _ = ensure_ssh_key(cfg)
                    ok = SSHOps.configure_server(
                        host=ip,
                        user="root",
                        private_key=priv,
                        server_resources=SERVER_RESOURCES_DIR,
                        substitutions={
                            "SERVER_HOSTNAME": m["hostname"],
                            "RCON_PASSWORD": m["rcon_password"],
                            "SERVER_PASSWORD": m["sv_password"],
                            "START_MAP": m["start_map"],
                            "STV_PASSWORD": m["stv_password"],
                        },
                        logs_dir=LOGS_DIR,
                        log_filename=f"{name}-{m['id']}.log"
                    )
                    print("Success." if ok else "Failed. See log in .tf2ctl/logs/")
                    pause()

                elif sub == "5":
                    ensure_ssh_key(cfg)
                    key_path = cfg.get("ssh_private_key_path") or str(CONFIG_DIR / "id_ed25519")
                    SSHOps.open_ssh_session(host=ip, user="root", private_key_path=key_path)

                elif sub == "6":
                    g, s, r = _build_conn_strings(ip, GAME_PORT, STV_PORT, m.get("sv_password",""), m.get("rcon_password",""))
                    print("\nConnection strings:")
                    print("GAME:", g)
                    print("STV :", s)
                    print("RCON:", r)
                    pause()

                elif sub == "7":
                    priv, _ = ensure_ssh_key(cfg)
                    rc, out, err = SSHOps.run_command(ip, "root", priv, "bash /root/tf2-copy.sh")
                    print(out if out else err or "(reapplied includes)")
                    pause()

                elif sub == "8":
                    if ask(f"Type 'yes' to delete {name}", "no").lower() == "yes":
                        api.delete_server(m["id"])
                        reg.pop(name, None)
                        save_registry(reg)
                        meta_path = CONFIG_DIR / f"{m['id']}.json"
                        if meta_path.exists():
                            try:
                                meta_path.unlink()
                            except (OSError, PermissionError, FileNotFoundError):
                                pass
                        print("Deleted.")
                        pause()
                        break

                elif sub == "9":
                    break
                else:
                    pause()

        elif choice == "4":
            reg = load_registry()
            if not reg:
                print("No servers tracked by this tool.")
            else:
                print("\nYour servers:")
                for name in sorted(reg.keys()):
                    _print_summary_line(name, reg[name])
            pause()

        elif choice == "5":
            _bulk_loop(load_registry(), build_api(cfg), cfg)

        elif choice == "6":
            print("Bye!")
            return

        else:
            pause()

if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\nExiting…")
        sys.exit(0)
