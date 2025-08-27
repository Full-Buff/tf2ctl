#!/usr/bin/env python3
import io
import os
import sys
import stat
import time
import random
import string
import shutil
import subprocess
import socket
from pathlib import Path
from typing import Dict, Optional, Tuple

import paramiko
from paramiko.ssh_exception import SSHException, NoValidConnectionsError
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization


class SSHOps:
    # pylint: disable=too-many-branches,too-many-statements,too-many-locals,too-many-nested-blocks,too-many-arguments,too-many-positional-arguments
    @staticmethod
    def random_password(n: int = 16) -> str:
        chars = string.ascii_letters + string.digits + "!@#$%^&*"
        return "".join(random.choice(chars) for _ in range(n))

    @staticmethod
    def generate_ed25519_keypair(comment: str = "tf2ctl") -> tuple[str, str]:
        private_key = ed25519.Ed25519PrivateKey.generate()
        priv_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        pub_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        pub_str = pub_bytes.decode("utf-8") + f" {comment}"
        return priv_pem, pub_str

    @staticmethod
    def is_port_open(host: str, port: int = 22, timeout: float = 3.0) -> bool:
        """Check if a TCP port is open and accepting connections."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except (socket.gaierror, socket.timeout, OSError):
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def wait_for_port(host: str, port: int = 22, timeout: int = 300, check_interval: float = 5.0) -> bool:
        """Wait for a port to become available."""
        start = time.time()
        print(f"Waiting for port {port} on {host} to become available...")
        while time.time() - start < timeout:
            if SSHOps.is_port_open(host, port, timeout=3.0):
                print(f"Port {port} is now open on {host}")
                # Give SSH daemon a moment to fully initialize after port opens
                time.sleep(5)
                return True
            time.sleep(check_interval)
        print(f"Timeout: Port {port} on {host} did not become available within {timeout} seconds")
        return False

    @staticmethod
    def _connect(host: str, user: str, private_key: str, timeout: int = 20) -> paramiko.SSHClient:
        key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username=user, pkey=key, timeout=timeout, banner_timeout=timeout)
        return client

    @staticmethod
    def _connect_retry(
        host: str,
        user: str,
        private_key: str,
        attempts: int = 10,
        base_delay: float = 3.0,
        max_delay: float = 15.0,
    ) -> paramiko.SSHClient:
        """
        Robust connector that retries on transient failures like banner read errors,
        connection resets, or port not ready. Always checks port availability first.
        """
        last_exc: Optional[Exception] = None
        for i in range(1, attempts + 1):
            try:
                # Always check if port is open before attempting connection
                if not SSHOps.is_port_open(host, 22, timeout=3.0):
                    raise socket.error("Port 22 not open yet")
                return SSHOps._connect(host, user, private_key, timeout=20)
            except (SSHException, NoValidConnectionsError, OSError, ConnectionResetError, socket.error) as e:
                last_exc = e
                delay = min(max_delay, base_delay * i)
                if i < attempts:
                    print(f"SSH connection attempt {i}/{attempts} failed, retrying in {delay:.1f}s...")
                time.sleep(delay)
        # Exhausted retries
        raise last_exc if last_exc else SSHException("Unknown SSH connect failure")

    @staticmethod
    def _wait_ssh(host: str, user: str, private_key: str, timeout: int = 900) -> bool:
        """
        Wait until we can connect AND successfully run a trivial command.
        This avoids the "banner" reset race during sshd restarts.
        Always waits for port first, regardless of provider.
        """
        # First ensure port 22 is open
        if not SSHOps.wait_for_port(host, 22, timeout=min(300, timeout)):
            print(f"Port 22 never became available on {host}")
            return False

        start = time.time()
        attempt = 0
        last_exc: Optional[Exception] = None
        while time.time() - start < timeout:
            attempt += 1
            try:
                client = SSHOps._connect(host, user, private_key, timeout=15)
                # Prove the session can actually execute a command
                _, stdout, stderr = client.exec_command("echo READY", get_pty=False)
                out = stdout.read().decode("utf-8", errors="ignore").strip()
                _ = stderr.read()
                rc = stdout.channel.recv_exit_status()
                client.close()
                if rc == 0 and out == "READY":
                    print("SSH is ready and accepting commands")
                    return True
            except (SSHException, NoValidConnectionsError, OSError, ConnectionResetError, socket.error) as e:
                last_exc = e
            # Simple fixed backoff
            time.sleep(5)
        if last_exc:
            print(f"SSH not ready within timeout. Last error: {last_exc}")
        else:
            print("SSH not ready within timeout.")
        return False

    # --- Robust remote mkdir -p with POSIX paths ---
    @staticmethod
    def _mkdir_parents(sftp: paramiko.SFTPClient, path: str):
        # Normalize to POSIX
        path = path.replace("\\", "/")
        parts = [p for p in path.split("/") if p not in ("", ".")]
        cur = "/" if path.startswith("/") else ""
        for part in parts:
            new = (cur + part) if cur in ("", "/") else (cur + "/" + part)
            try:
                sftp.stat(new)
            except IOError:
                try:
                    sftp.mkdir(new)
                except IOError:
                    pass
            cur = new

    @staticmethod
    def _sftp_put_dir(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str):
        # Ensure the root exists
        SSHOps._mkdir_parents(sftp, remote_dir)
        for root, _, files in os.walk(local_dir):
            # rel may contain Windows backslashes; force POSIX for remote
            rel = os.path.relpath(root, str(local_dir))
            rel_posix = "." if rel == "." else rel.replace("\\", "/")
            dest = remote_dir if rel_posix == "." else f"{remote_dir}/{rel_posix}"
            SSHOps._mkdir_parents(sftp, dest)
            for fname in files:
                if fname.startswith("."):
                    continue
                local_path = os.path.join(root, fname)
                remote_path = f"{dest}/{fname}"
                sftp.put(local_path, remote_path)

    @staticmethod
    def configure_server(
        # pylint: disable=too-many-return-statements
        host: str,
        user: str,
        private_key: str,
        server_resources: Path,
        substitutions: Dict[str, str],
        logs_dir: Optional[Path] = None,
        log_filename: Optional[str] = None,
    ) -> bool:
        """
        Uploads server_resources, waits for cloud-init/apt to finish, runs setup.sh with bash -x,
        copies includes into the container, and saves a full log locally if logs_dir is provided.
        Returns True/False.
        """
        if not server_resources.exists():
            print(f"server_resources not found at {server_resources}")
            return False

        if not SSHOps._wait_ssh(host, user, private_key):
            return False

        # connect with retry (handles banner/connection resets)
        try:
            client = SSHOps._connect_retry(host, user, private_key, attempts=10, base_delay=3.0, max_delay=15.0)
        except (SSHException, NoValidConnectionsError, OSError, ConnectionResetError) as e:
            print(f"Unable to establish SSH session: {e}")
            return False

        try:
            # Open SFTP (retry once if needed)
            try:
                sftp = client.open_sftp()
            except paramiko.sftp.SFTPError:
                client.close()
                client = SSHOps._connect_retry(host, user, private_key, attempts=6, base_delay=3.0, max_delay=10.0)
                sftp = client.open_sftp()

            # Upload setup.sh with robust decoding/encoding
            setup_path = server_resources / "scripts" / "setup.sh"
            if not setup_path.exists():
                print(f"Missing {setup_path}")
                return False

            # Read raw bytes (avoid Windows locale issues), decode UTF-8 with surrogateescape
            raw_bytes = setup_path.read_bytes()
            text = raw_bytes.decode("utf-8", errors="surrogateescape")
            # Normalize newlines for bash
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            # Perform placeholder substitutions:
            # - ${KEY}
            # - KEY_REPLACE
            for k, v in substitutions.items():
                text = text.replace("${" + k + "}", str(v))
                text = text.replace(f"{k}_REPLACE", str(v))

            # --- Ensure setup.sh triggers a post-copy every time (idempotent) ---
            appended_postcopy = False
            if "TF2CTL_POSTCOPY" not in text:
                text += "\n# TF2CTL_POSTCOPY\nbash /root/tf2-copy.sh || true\n"
                appended_postcopy = True

            # Encode back to bytes and upload in binary mode
            out_bytes = text.encode("utf-8", errors="surrogateescape")
            tmp_remote = "/root/tf2-setup.sh"
            with sftp.open(tmp_remote, "wb") as fp:
                fp.write(out_bytes)
            sftp.chmod(tmp_remote, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            # Upload includes (configs/cfg/cfgs/maps/addons) if present
            includes = server_resources / "includes"
            if includes.exists():
                SSHOps._sftp_put_dir(sftp, includes, "/root/tf2-includes")

            # Upload the copy helper BEFORE running setup (so setup can call it)
            #pylint: disable=line-too-long
            copy_remote = "/root/tf2-copy.sh"
            copy_script = r"""#!/usr/bin/env bash
set -e
log="/root/tf2-setup.log"
container="tf2"

{
  echo "=== Copying resources into container: $container ==="
  if ! docker inspect "$container" >/dev/null 2>&1; then
    echo "Container $container does not exist yet; skipping copy."
    exit 0
  fi

  if ! docker ps --format '{{.Names}}' | grep -qw "$container"; then
    echo "WARN: container $container not running; trying to start..."
    docker start "$container" >/dev/null 2>&1 || true
  fi

  # If a server.cfg exists in the uploaded cfg-like dir, rename it to tf2ctl.cfg to avoid overwriting generated server.cfg
  for d in cfg cfgs configs; do
    if [ -f "/root/tf2-includes/$d/server.cfg" ]; then
      echo "Found user server.cfg in $d/, renaming to tf2ctl.cfg"
      mv "/root/tf2-includes/$d/server.cfg" "/root/tf2-includes/$d/tf2ctl.cfg"
    fi
  done

  # cfg-like dirs -> /home/tf2/server/tf/cfg/
  for d in cfg cfgs configs; do
    if [ -d "/root/tf2-includes/$d" ]; then
      echo "Copying $d/ -> /home/tf2/server/tf/cfg/"
      docker cp "/root/tf2-includes/$d/." "$container:/home/tf2/server/tf/cfg/"
    fi
  done

  # Ensure autoexec.cfg will exec our overrides on every start (after server.cfg)
  docker exec "$container" bash -lc 'CFG="/home/tf2/server/tf/cfg/autoexec.cfg"; touch "$CFG"; grep -q "^exec tf2ctl.cfg" "$CFG" || echo "exec tf2ctl.cfg" >> "$CFG"'

  # maps/ -> /home/tf2/server/tf/maps/
  if [ -d /root/tf2-includes/maps ]; then
    echo "Copying maps/ -> /home/tf2/server/tf/maps/"
    docker cp /root/tf2-includes/maps/. "$container:/home/tf2/server/tf/maps/"
  fi

  # addons/ -> /home/tf2/server/tf/addons/
  if [ -d /root/tf2-includes/addons ]; then
    echo "Copying addons/ -> /home/tf2/server/tf/addons/"
    docker cp /root/tf2-includes/addons/. "$container:/home/tf2/server/tf/addons/"
  fi

  echo "=== Finished copying resources ==="
} | tee -a "$log"
"""
            with sftp.open(copy_remote, "w") as fp:
                fp.write(copy_script)
            sftp.chmod(copy_remote, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            # Add a helpful alias on the host shell
            try:
                cmd_alias = r'''bash -lc 'PROFILE=/root/.bashrc; touch "$PROFILE"; grep -q "alias tf2apply=" "$PROFILE" || echo "alias tf2apply=\"bash /root/tf2-copy.sh\"" >> "$PROFILE"' '''
                client.exec_command(cmd_alias)
            except paramiko.ssh_exception.SSHException:
                pass

            # --- Wait for apt/dpkg/cloud-init to finish to avoid lock races ---
            wait_remote = "/root/tf2-wait.sh"
            wait_script = """#!/usr/bin/env bash
set -e
for _ in $(seq 1 200); do
  if [ -f /var/lib/cloud/instance/boot-finished ] || [ -f /var/local/tf2ctl-init-done ]; then
    break
  fi
  sleep 3
done
for _ in $(seq 1 200); do
  if ! pgrep -x unattended-upgrade >/dev/null 2>&1 \
     && ! pgrep -x apt >/dev/null 2>&1 \
     && ! pgrep -x apt-get >/dev/null 2>&1 \
     && ! pgrep -x dpkg >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
exit 0
"""
            with sftp.open(wait_remote, "w") as fp:
                fp.write(wait_script)
            sftp.chmod(wait_remote, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            # Run the wait script and block until it completes
            _, w_out, w_err = client.exec_command(f"bash {wait_remote}", get_pty=True)
            _ = w_out.read()
            _ = w_err.read()
            _ = w_out.channel.recv_exit_status()

            # --- Run setup script with tracing, pipe all output to a remote log ---
            remote_log = "/root/tf2-setup.log"
            cmd = f"bash -x {tmp_remote} > {remote_log} 2>&1; echo __EXIT_CODE__$?"
            _, stdout, _ = client.exec_command(cmd)
            marker = stdout.read().decode("utf-8", errors="replace").strip()
            setup_rc = 1
            if "__EXIT_CODE__" in marker:
                try:
                    setup_rc = int(marker.split("__EXIT_CODE__")[-1])
                except (ValueError, IndexError):
                    setup_rc = 1

            # If we did NOT inject the call into setup.sh (rare), run copy script now
            if not appended_postcopy:
                _, c_out, _ = client.exec_command(f"bash {copy_remote}; echo __COPY_RC__$?")
                marker2 = c_out.read().decode("utf-8", errors="replace").strip()
                copy_rc = 0
                if "__COPY_RC__" in marker2:
                    try:
                        copy_rc = int(marker2.split("__COPY_RC__")[-1])
                    except (ValueError, IndexError):
                        copy_rc = 1
            else:
                copy_rc = 0  # setup.sh already invoked it; the output is in the same log

            # Pull the log locally if requested
            overall_rc = (setup_rc == 0) and (copy_rc == 0)
            if logs_dir:
                try:
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    local_log = logs_dir / (log_filename or f"{host}-setup.log")
                    sftp.get(remote_log, str(local_log))
                    print(f"(Saved setup log to {local_log})")
                except (OSError, IOError, FileNotFoundError) as e:
                    print(f"(Could not save setup log: {e})")

            # Quick verify: docker ps (non-fatal)
            client.exec_command("docker ps || true")

            sftp.close()
            client.close()
            return overall_rc
        except SSHException as e:
            print(f"SSH error: {e}")
            try:
                client.close()
            except (OSError, SSHException):
                pass
            return False
        except (OSError, socket.error) as e:
            print(f"Unexpected error during configure_server: {e}")
            try:
                client.close()
            except (OSError, SSHException):
                pass
            return False

    @staticmethod
    def run_command(host: str, user: str, private_key: str, command: str, get_pty: bool = True) -> Tuple[int, str, str]:
        """
        Run a single command over SSH, return (rc, stdout, stderr).
        """
        try:
            client = SSHOps._connect_retry(host, user, private_key, attempts=6, base_delay=3.0, max_delay=10.0)
            _, stdout, stderr = client.exec_command(command, get_pty=get_pty)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
            client.close()
            return rc, out, err
        except (SSHException) as e:
            return 1, "", f"(failed to run command) {e}"

    @staticmethod
    def get_container_logs(host: str, user: str, private_key: str, container: str = "tf2", tail: int = 200) -> str:
        try:
            client = SSHOps._connect_retry(host, user, private_key, attempts=6, base_delay=3.0, max_delay=10.0)
            cmd = f"docker logs --tail {tail} {container} 2>&1 || true"
            _, stdout, _ = client.exec_command(cmd)
            out = stdout.read().decode("utf-8", errors="replace")
            client.close()
            return out
        except (SSHException) as e:
            return f"(failed to get logs) {e}"

    @staticmethod
    def open_ssh_session(host: str, user: str, private_key_path: str):
        #pylint: disable=consider-using-with
        """
        Launch an interactive ssh session in a NEW terminal window, OS-aware.
        Fallback: run in the current console (blocking) if no terminal emulator is found.
        """
        ssh_cmd = f'ssh -i "{private_key_path}" {user}@{host}'

        # Windows: reliable form via cmd's START with empty title; keep window open
        if os.name == "nt":
            try:
                # Use shell=True so START is interpreted by cmd.exe
                subprocess.Popen(f'start "" cmd /k {ssh_cmd}', shell=True)
                return
            except (subprocess.CalledProcessError, OSError):
                # Fallback to PowerShell
                try:
                    subprocess.Popen(['powershell', '-NoProfile', '-Command',
                                      f'Start-Process cmd -ArgumentList \'/k {ssh_cmd}\''])
                    return
                except (subprocess.CalledProcessError, OSError):
                    pass

        # macOS
        if sys.platform == "darwin":
            osa = f'tell application "Terminal" to do script "{ssh_cmd}"; activate'
            try:
                subprocess.Popen(["osascript", "-e", osa])
                return
            except (subprocess.CalledProcessError, OSError):
                pass

        # Linux / others
        candidates = [
            ["x-terminal-emulator", "-e", "bash", "-lc", f"{ssh_cmd}; exec bash"],
            ["gnome-terminal", "--", "bash", "-lc", f"{ssh_cmd}; exec bash"],
            ["konsole", "-e", "bash", "-lc", f"{ssh_cmd}; exec bash"],
            ["xfce4-terminal", "-e", f"bash -lc '{ssh_cmd}; exec bash'"],
            ["xterm", "-e", ssh_cmd],
        ]
        for cmd in candidates:
            if shutil.which(cmd[0]):
                try:
                    subprocess.Popen(cmd)
                    return
                except (subprocess.CalledProcessError, OSError):
                    continue

        # Fallback: open in current console (blocking)
        try:
            subprocess.call(["ssh", "-i", private_key_path, f"{user}@{host}"])
        except FileNotFoundError:
            print("Could not find the system 'ssh' command. Install OpenSSH client or add it to PATH.")
