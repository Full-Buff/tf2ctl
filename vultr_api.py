#!/usr/bin/env python3
import time
from typing import Dict, Any, List, Optional

import requests


class VultrAPIError(RuntimeError):
    pass


class VultrAPI:
    """
    Minimal Vultr provider adapter for TF2CTL.
    Docs:
      - API base: https://api.vultr.com/v2
      - Auth: Authorization: Bearer <token>
    """

    def __init__(self, token: str):
        self.token = token.strip()
        self.base = "https://api.vultr.com/v2"

    # -------------- internal helpers --------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _handle_error(self, r: requests.Response):
        try:
            data = r.json()
        except Exception:  # pylint: disable=broad-exception-caught
            data = {}
        msg = data.get("error", {}).get("message") or data.get("message") or r.text
        raise VultrAPIError(f"{r.status_code} {r.request.method} {r.request.url} -> {msg}")

    # -------------- provider facade --------------
    @staticmethod
    def recommended_sizes() -> Dict[str, str]:
        """
        Map human-friendly choices to Vultr plan IDs.
        Common general compute plan slugs (vc2-*) are widely used.
        """
        return {
            "small": "vc2-1c-2gb",
            "medium": "vc2-2c-4gb",
            "large": "vc2-4c-8gb",
        }

    def list_regions(self) -> List[Dict[str, Any]]:
        url = f"{self.base}/regions"
        r = requests.get(url, headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        data = r.json().get("regions", [])
        # Normalize to {slug, name}
        out = []
        for reg in data:
            rid = reg.get("id") or reg.get("region") or ""
            name = reg.get("city") or reg.get("description") or rid
            out.append({"slug": rid, "name": name})
        return sorted(out, key=lambda x: x["slug"])

    def capacity_remaining(self) -> Optional[int]:
        # Vultr does not publish account droplet caps via API.
        return None

    # --- SSH Keys ---
    def ensure_ssh_key(self, public_key: str, name: str = "tf2ctl") -> str:
        """
        Return existing key ID if the exact public_key exists, otherwise create and return new ID.
        """
        # List
        r = requests.get(f"{self.base}/ssh-keys", headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        for item in r.json().get("ssh_keys", []):
            if item.get("ssh_key") == public_key:
                return item.get("id")

        # Create
        payload = {"name": name, "ssh_key": public_key}
        r = requests.post(f"{self.base}/ssh-keys", json=payload, headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        return r.json().get("ssh_key", {}).get("id")

    # --- Instances ---
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def create_server(
        self,
        name: str,
        region: str,
        size: str,
        ssh_key_id: str,
        public_key: str,  # unused but kept for signature parity
        tags: List[str],
    ) -> Dict[str, Any]:
        """
        Create an instance. Ubuntu 24.04 LTS x64 has os_id 2284 per Vultr docs.
        """
        # pylint: disable=unused-argument
        payload = {
            "region": region,
            "plan": size,
            "os_id": 2284,  # Ubuntu 24.04 LTS x64
            "label": name,
            "ssh_key_ids": [ssh_key_id],
            "tags": tags or [],
            "enable_ipv6": True,
        }
        r = requests.post(f"{self.base}/instances", json=payload, headers=self._headers(), timeout=60)
        if not r.ok:
            self._handle_error(r)
        inst = r.json().get("instance", {})
        return {"id": inst.get("id"), "status": inst.get("status"), "raw": inst}

    def get_instance(self, instance_id: str) -> Dict[str, Any]:
        r = requests.get(f"{self.base}/instances/{instance_id}", headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        return r.json().get("instance", {})

    def wait_for_active_ip(self, instance_id: str, timeout: int = 900, poll: float = 5.0) -> Dict[str, Any]:
        """
        Poll until instance.status == 'active' and a main_ip is present.
        """
        start = time.time()
        while time.time() - start < timeout:
            inst = self.get_instance(instance_id)
            status = inst.get("status")
            ip = inst.get("main_ip")
            if status == "active" and ip:
                return {"ip": ip, "region": inst.get("region")}
            time.sleep(poll)
        raise VultrAPIError(f"Timed out waiting for instance {instance_id} to become active and get IP")

    def delete_server(self, instance_id: str) -> bool:
        r = requests.delete(f"{self.base}/instances/{instance_id}", headers=self._headers(), timeout=30)
        if r.status_code in (204, 200):
            return True
        if not r.ok:
            self._handle_error(r)
        return True
