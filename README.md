# TF2CTL — Team Fortress 2 Server Manager (CLI)

TF2CTL is a cross-platform CLI tool that automates provisioning and managing **TF2 game servers** in the cloud. It can spin up Linux VMs, install Docker, launch the TF2 container, upload your custom content (maps, configs, plugins), and give you a simple menu to manage and monitor servers.
This is mainly geared toward competitive play, but can be easily modified as needed.

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

[Check the wiki on this repo for more info](https://github.com/Full-Buff/tf2ctl/wiki)

---

## Uninstall / Cleanup

1. Delete all servers from the "Manage a server" or "Bulk actions" menus to avoid further cloud charges.
2. Remove the local `./tf2ctl/` directory to wipe all saved configurations, keys, logs, and server records.

---

## Local Dev Info

### Run this to get started:
```bash
make install-dev
```
Or without make:
```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
pre-commit install
```

### To lint on demand run:
```bash
make lint
```
Or:
```bash
python -m pylint $(git ls-files '*.py')
```

### Hooks will auto-run on each commit. To run them against the whole repo manually:
```bash
pre-commit run --all-files
```
