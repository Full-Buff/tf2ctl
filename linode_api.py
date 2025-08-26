#!/usr/bin/env python3
# pylint: disable=duplicate-code
import time
import secrets
import string
from typing import Dict, Any, List, Optional
import requests

API = "https://api.linode.com/v4"

class LinodeAPIError(Exception):
    def __init__(self, message: str, status: int = 0, payload: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}

class LinodeAPI:
    # pylint: disable=too-many-branches,too-many-statements,too-many-locals,too-many-nested-blocks,too-many-arguments,too-many-positional-arguments
    """
    Minimal Linode v4 provider:
      - list_regions()
      - recommended_sizes()
      - capacity_remaining() -> None (Linode doesn't expose limits via API)
      - ensure_ssh_key() (profile key)
      - create_server()
      - wait_for_active_ip()
      - delete_server()

    Docs:
      - Regions: GET /regions
      - Create instance: POST /linode/instances
      - List instances: GET /linode/instances
      - Get instance: GET /linode/instances/{id}
      - Add SSH key to profile: POST /profile/sshkeys
    """
    def __init__(self, token: str):
        self.token = token.strip()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _handle_error(self, r: requests.Response):
        try:
            data = r.json()
        except LinodeAPIError:
            data = {}
        msg = data.get("errors") or data.get("message") or r.text
        # errors can be a list of {"reason": "..."}
        if isinstance(msg, list) and msg and isinstance(msg[0], dict) and "reason" in msg[0]:
            msg = msg[0]["reason"]
        raise LinodeAPIError(str(msg), status=r.status_code, payload=data)

    # --------------------------
    # Sizing / Regions / Capacity
    # --------------------------
    def recommended_sizes(self) -> Dict[str, str]:
        # TF2-appropriate, shared CPU plans
        # Examples: g6-standard-1 (2GB/1vCPU), g6-standard-2 (4GB/2vCPU) ...
        return {
            "small": "g6-standard-1",
            "medium": "g6-standard-2",
            "large": "g6-standard-4",
            "xlarge": "g6-standard-8",
        }

    def list_regions(self) -> List[Dict[str, Any]]:
        r = requests.get(f"{API}/regions", headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        out = []
        for reg in r.json().get("data", []):
            out.append({"id": reg.get("id"), "label": reg.get("label")})
        return out

    def capacity_remaining(self) -> Optional[int]:
        """
        Linode does NOT expose per-account instance limits via API;
        return None so the CLI skips capacity gating for this provider.
        """
        return None  # See community reference; limits must be checked via support.

    # --------------------------
    # SSH Keys (Profile)
    # --------------------------
    def list_profile_keys(self) -> List[Dict[str, Any]]:
        r = requests.get(f"{API}/profile/sshkeys", headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        return r.json().get("data", [])

    def ensure_ssh_key(self, pub_key: str) -> str:
        # Try to find an exact match
        for k in self.list_profile_keys():
            if k.get("ssh_key", "").strip() == pub_key.strip():
                return str(k.get("id"))
        # Create new
        r = requests.post(
            f"{API}/profile/sshkeys",
            headers=self._headers(),
            json={"label": "tf2ctl", "ssh_key": pub_key},
            timeout=30,
        )
        if not r.ok:
            self._handle_error(r)
        return str(r.json().get("id"))

    # --------------------------
    # Instances
    # --------------------------
    def _rand_root_pass(self, length: int = 20) -> str:
        # Linode requires a root password when creating from an image.
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def create_server(self, name: str, region: str, size: str, ssh_key_id: str, public_key: str, tags: List[str]) -> Dict[str, Any]:
        """
        Creates a Linode instance with Ubuntu 22.04 image.
        We pass `authorized_keys` with the *public key string* (works even if not in profile).
        """
        # Linode creation uses authorized_keys not an ID from profile
        # pylint: disable=unused-argument
        payload = {
            "label": name,
            "region": region,
            "type": size,
            "image": "linode/ubuntu22.04",
            "root_pass": self._rand_root_pass(),
            "authorized_keys": [public_key],
            "tags": tags or [],
            # network defaults to public; ipv4 assigned automatically
        }
        r = requests.post(f"{API}/linode/instances", headers=self._headers(), json=payload, timeout=60)
        if not r.ok:
            self._handle_error(r)
        return r.json()

    def wait_for_active_ip(self, linode_id: int, timeout: int = 900, poll: int = 8) -> Dict[str, Any]:
        """
        Wait until instance is 'running' and an IPv4 address is present.
        """
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            r = requests.get(f"{API}/linode/instances/{linode_id}", headers=self._headers(), timeout=30)
            if not r.ok:
                self._handle_error(r)
            data = r.json()
            last = data
            status = data.get("status")
            ipv4 = data.get("ipv4") or []
            if status == "running" and ipv4:
                # Prefer the first public IPv4
                ip = ipv4[0]
                return {"ip": ip, "region": data.get("region", "")}
            time.sleep(poll)
        return {"ip": "", "last": last}

    def delete_server(self, linode_id: int):
        r = requests.delete(f"{API}/linode/instances/{linode_id}", headers=self._headers(), timeout=60)
        if r.status_code not in (200, 204, 404):
            self._handle_error(r)
