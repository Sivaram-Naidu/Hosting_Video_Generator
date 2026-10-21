"""Upload finished videos to a SharePoint / OneDrive for Business folder via Microsoft Graph.

Uses app-only auth (client credentials). Configure with env vars:
    MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET, SHAREPOINT_FOLDER_URL
SHAREPOINT_FOLDER_URL is the sharing link of the destination folder
("Copy link" in SharePoint/OneDrive).
"""
import os, time, base64, threading
from pathlib import Path
from urllib.parse import quote

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
CHUNK = 320 * 1024 * 16   # 5 MiB — Graph requires multiples of 320 KiB


class SharePointStorage:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, folder_url: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.folder_url = folder_url
        self._token = None
        self._token_exp = 0.0
        self._target = None          # (drive_id, folder_item_id)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        keys = ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "SHAREPOINT_FOLDER_URL")
        vals = [os.getenv(k, "").strip() for k in keys]
        if not all(vals):
            return None
        return cls(*vals)

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        with self._lock:
            if not self._token or time.time() > self._token_exp - 120:
                r = httpx.post(
                    f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                self._token = data["access_token"]
                self._token_exp = time.time() + int(data.get("expires_in", 3600))
            return {"Authorization": f"Bearer {self._token}"}

    def _folder(self):
        """Resolve the folder sharing link to (drive_id, item_id) once."""
        if self._target is None:
            encoded = "u!" + base64.urlsafe_b64encode(self.folder_url.encode()).decode().rstrip("=")
            r = httpx.get(f"{GRAPH}/shares/{encoded}/driveItem",
                          headers=self._headers(), timeout=30)
            r.raise_for_status()
            item = r.json()
            self._target = (item["parentReference"]["driveId"], item["id"])
        return self._target

    # ── Operations ────────────────────────────────────────────────────────────

    def upload(self, path: str) -> dict:
        """Upload a file in chunks; returns {"name", "web_url", "size"}."""
        drive_id, folder_id = self._folder()
        name = Path(path).name
        r = httpx.post(
            f"{GRAPH}/drives/{drive_id}/items/{folder_id}:/{quote(name)}:/createUploadSession",
            headers=self._headers(),
            json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
            timeout=30,
        )
        r.raise_for_status()
        upload_url = r.json()["uploadUrl"]

        size = os.path.getsize(path)
        item = None
        with open(path, "rb") as f, httpx.Client(timeout=120) as client:
            start = 0
            while start < size:
                chunk = f.read(CHUNK)
                end = start + len(chunk) - 1
                # Upload URL is pre-authenticated — must NOT send the Bearer token
                resp = client.put(upload_url, content=chunk, headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                resp.raise_for_status()
                if resp.status_code in (200, 201):
                    item = resp.json()
                start = end + 1

        if item is None:
            raise RuntimeError(f"Upload of {name} did not complete")
        return {"name": item["name"], "web_url": item["webUrl"], "size": item.get("size", size)}

    def list_videos(self) -> list:
        drive_id, folder_id = self._folder()
        r = httpx.get(
            f"{GRAPH}/drives/{drive_id}/items/{folder_id}/children",
            params={"$select": "name,size,webUrl,lastModifiedDateTime", "$top": "200"},
            headers=self._headers(), timeout=30,
        )
        r.raise_for_status()
        items = [i for i in r.json().get("value", []) if i["name"].lower().endswith(".mp4")]
        items.sort(key=lambda i: i["lastModifiedDateTime"], reverse=True)
        return [{"name": i["name"], "size_mb": round(i.get("size", 0) / 1_048_576, 1),
                 "web_url": i["webUrl"]} for i in items]
