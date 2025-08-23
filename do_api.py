#!/usr/bin/env python3
import time
import requests
from typing import Dict, Any, List, Optional

API = "https://api.digitalocean.com/v2"

class DOAPIError(Exception):
    def __init__(self, message: str, status: int = 0, payload: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}

class DigitalOceanAPI:
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
        except Exception:
            data = {}
        msg = data.get("message") or data.get("error") or r.text
        raise DOAPIError(msg, status=r.status_code, payload=data)

    # --------------------------
    # Sizing / Regions / Capacity
    # --------------------------
    def recommended_sizes(self) -> Dict[str, str]:
        # Friendly TF2-appropriate sizes -> DO slugs
        return {
            "small": "s-2vcpu-2gb",
            "medium": "s-2vcpu-4gb",
            "large": "s-4vcpu-8gb",
            "xlarge": "s-8vcpu-16gb",
        }

    def get_account_info(self) -> Dict[str, Any]:
        r = requests.get(f"{API}/account", headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        return r.json().get("account", {})

    def list_all_droplets(self) -> List[Dict[str, Any]]:
        droplets: List[Dict[str, Any]] = []
        page = 1
        while True:
            r = requests.get(
                f"{API}/droplets",
                headers=self._headers(),
                params={"page": page, "per_page": 200},
                timeout=60,
            )
            if not r.ok:
                self._handle_error(r)
            data = r.json()
            droplets.extend(data.get("droplets", []))
            links = data.get("links", {})
            if not links or "pages" not in links or "next" not in links["pages"]:
                break
            page += 1
        return droplets

    def capacity_remaining(self) -> Optional[int]:
        """
        Returns remaining droplet capacity for this account.
        """
        info = self.get_account_info()
        limit = int(info.get("droplet_limit", 0))
        cur = len(self.list_all_droplets())
        return max(0, limit - cur)

    def list_regions(self) -> List[Dict[str, Any]]:
        r = requests.get(f"{API}/regions", headers=self._headers(), timeout=30)
        if not r.ok:
            self._handle_error(r)
        regions = []
        for reg in r.json().get("regions", []):
            if reg.get("available", False):
                regions.append({"slug": reg["slug"], "name": reg.get("name", reg["slug"])})
        return regions

    # --------------------------
    # SSH Keys
    # --------------------------
    def list_ssh_keys(self) -> List[Dict[str, Any]]:
        out = []
        page = 1
        while True:
            r = requests.get(f"{API}/account/keys", headers=self._headers(), params={"page": page, "per_page": 200}, timeout=30)
            if not r.ok:
                self._handle_error(r)
            data = r.json()
            out.extend(data.get("ssh_keys", []))
            links = data.get("links", {})
            if not links or "pages" not in links or "next" not in links["pages"]:
                break
            page += 1
        return out

    def ensure_ssh_key(self, pub_key: str) -> str:
        for k in self.list_ssh_keys():
            if k.get("public_key", "").strip() == pub_key.strip():
                return str(k["id"])
        r = requests.post(
            f"{API}/account/keys",
            headers=self._headers(),
            json={"name": "tf2ctl", "public_key": pub_key},
            timeout=30,
        )
        if not r.ok:
            self._handle_error(r)
        return str(r.json()["ssh_key"]["id"])

    # --------------------------
    # Droplets
    # --------------------------
    def create_server(self, name: str, region: str, size: str, ssh_key_id: str, public_key: str, tags: List[str]) -> Dict[str, Any]:
        payload = {
            "name": name,
            "region": region,
            "size": size,
            "image": "ubuntu-22-04-x64",
            "ssh_keys": [ssh_key_id],
            "backups": False,
            "ipv6": True,
            "user_data": None,
            "private_networking": None,
            "volumes": None,
            "tags": tags,
        }
        r = requests.post(f"{API}/droplets", headers=self._headers(), json=payload, timeout=60)
        if r.status_code >= 400:
            self._handle_error(r)
        return r.json()["droplet"]

    def wait_for_active_ip(self, droplet_id: int, timeout: int = 900, poll: int = 8) -> Dict[str, Any]:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            r = requests.get(f"{API}/droplets/{droplet_id}", headers=self._headers(), timeout=30)
            if not r.ok:
                self._handle_error(r)
            data = r.json().get("droplet", {})
            last = data
            if data.get("status") == "active":
                for net in data.get("networks", {}).get("v4", []):
                    if net.get("type") == "public":
                        return {"ip": net.get("ip_address", ""), "region": data.get("region", {}).get("slug", "")}
            time.sleep(poll)
        return {"ip": "", "last": last}

    def delete_server(self, droplet_id: int):
        r = requests.delete(f"{API}/droplets/{droplet_id}", headers=self._headers(), timeout=60)
        if r.status_code not in (204, 404):
            self._handle_error(r)
