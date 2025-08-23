# TF2CTL — Team Fortress 2 Server Manager (CLI)

TF2CTL is a cross-platform CLI tool that automates provisioning and managing **TF2 game servers** in the cloud. It can spin up Linux VMs, install Docker, launch the TF2 container, upload your custom content (maps, configs, plugins), and give you a simple menu to manage and monitor servers.

## Highlights

* One-shot **create & configure** (no manual follow-ups)
* Works on **Windows / macOS🤞 / Linux🤞**
* Fully **self-contained** in your project folder
* **Bulk create** many servers (auto-names, per-server secrets)
* Built-in troubleshooting: open SSH, view logs, reapply includes
* **Safe re-runs** (idempotent setup + copy steps)

> [!WARNING]
> **Cloud costs:** Creating servers will incur charges from your cloud provider. Remember to delete servers when you're done.

---

## Quick Start

### 1. Requirements

* **Python 3.11+** recommended (3.13 is OK).
    Windows users can run this in powershell or cmd:
    ```powershell
    winget install -e --id Python.Python.3.11
    ```
* **Install dependencies** from the project root:
    ```bash
    pip install -r requirements.txt
    ```
    *Required packages include `paramiko`, `cryptography`, and `requests`.*

### 2. Project Layout

Your project folder should be structured like this:

```plaintext
tf2ctl/
├─ cli.py
├─ ssh_ops.py
├─ do_api.py
├─ linode_api.py
└─ server_resources/
   ├─ scripts/
   │  └─ setup.sh
   └─ includes/
      ├─ cfg/      # Your configs; server.cfg will be renamed to tf2ctl.cfg
      ├─ maps/     # .bsp files
      └─ addons/   # e.g. addons/sourcemod/plugins/*.smx
```

Place your custom content under `server_resources/includes/`. The tool will copy it to the correct locations on the server:

* `cfg/` → is copied to `/home/tf2/server/tf/cfg/`
* `maps/` → is copied to `/home/tf2/server/tf/maps/`
* `addons/` → is copied to `/home/tf2/server/tf/addons/`

### 3. Run the CLI

From the project root, run this command:

```bash
python cli.py
```

### 4. Configure Provider, Token, and SSH Key

In the main menu, select "Configure provider / API token / SSH key":

1. Pick a provider (DigitalOcean or Linode).
2. Paste your provider's API token when prompted. It will be stored locally in `.tf2ctl/config.json`.
3. Generate a new Ed25519 SSH key (recommended) or supply a path to your own. Keys are stored under `.tf2ctl/`.

### 5. Create Servers

In the main menu, select "Create server(s) (auto-configure)":

1. Choose a name prefix, start number, quantity, region, server size, and starting map.
2. The tool will then:
   - Create the servers with a 1-second delay between each to be gentle on APIs.
   - Wait for public IPs to be assigned.
   - Upload `server_resources/` and run `setup.sh` on each server.
   - Copy your `includes/` content into the TF2 container.
   - Save all server credentials and connection info locally under `.tf2ctl/`.

You'll see a summary with IPs and passwords. You can also view or export connection strings at any time from the main menu.

---

## Files & Logs

### Local Files (`./tf2ctl/`)

* `config.json`: Stores provider tokens & SSH key paths.
* `servers.json`: A local registry of your servers with their IDs, IPs, and other metadata.
* `id_ed25519` / `id_ed25519.pub`: The generated SSH keys.
* `logs/*.log`: Per-server setup logs pulled from the remote machine.

### Remote Server Files

* `/root/tf2-setup.sh`: Your setup script.
* `/root/tf2-copy.sh`: A helper script for copying includes into the container.
* `/var/log/tf2-setup.log`: A log file tracing the setup and copy process.
* The alias `tf2apply` is created, which points to `bash /root/tf2-copy.sh`.

---

## Troubleshooting

* **Windows "bad permissions" on SSH key**: The tool automatically hardens file permissions. If you use your own key path (e.g., under OneDrive), you may need to fix permissions manually:
    ```powershell
    icacls "C:\path\to\id_ed25519" /inheritance:r
    icacls "C:\path\to\id_ed25519" /grant:r %USERNAME%:R
    icacls "C:\path\to\id_ed25519" /remove:g Users
    ```

* **Apt lock / setup errors**: The tool waits for cloud-init to finish, but if an error occurs, you can safely re-run the process from the Manage a server menu using "Re-configure" or "Reapply includes".

* **RCON/passwords not applied on first boot**: Your settings in `tf2ctl.cfg` are applied via `autoexec.cfg` after the server's own generated config. If a map or plugin pack later overwrites your values, ensure your desired values are in `tf2ctl.cfg`.

* **Provider limits**:
  - **DigitalOcean**: The tool automatically checks your droplet limit and warns you or adjusts the bulk creation amount.
  - **Linode**: The API doesn't expose limits, so the tool proceeds but still delays 1 second between creations to avoid rate-limiting.

---

## Developer Notes

* **Provider-agnostic design**: The core CLI calls a uniform interface. Each provider module (`do_api.py`, `linode_api.py`) simply implements the required functions:
    ```python
    list_regions() -> list[dict]
    recommended_sizes() -> dict[str, str]
    ensure_ssh_key(pub_key: str) -> str
    create_server(name, region, size, ssh_key_id, public_key, tags) -> dict
    wait_for_active_ip(id) -> dict
    delete_server(id) -> None
    capacity_remaining() -> Optional[int]
    ```

* **Idempotency**: Both the setup and copy scripts are safe to re-run. The `tf2-copy.sh` script safely renames `server.cfg`, copies files, and only adds the exec command to `autoexec.cfg` if it's missing.

* **Paths**: The tool keeps all local state in the project's `./tf2ctl/` directory and automatically normalizes Windows backslashes (`\`) to POSIX forward slashes (`/`) for remote paths.

* **Bulk create pacing**: A 1-second delay is hardcoded between server creation API calls to respect provider rate limits.

Contributions are welcome!

---

## Uninstall / Cleanup

1. Delete all servers from the "Manage a server" or "Bulk actions" menus to avoid further cloud charges.
2. Remove the local `./tf2ctl/` directory to wipe all saved configurations, keys, logs, and server records.
