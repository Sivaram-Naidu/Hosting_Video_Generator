"""Upload finished videos to a SharePoint / OneDrive for Business folder via Microsoft Graph.

Two auth modes (pick one):
  * Signed-in user (no admin consent):  MS_TENANT_ID, MS_CLIENT_ID, MS_REFRESH_TOKEN
    Get the refresh token by running `python get_token.py` once.
  * App-only (needs admin consent):     MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET
Plus SHAREPOINT_FOLDER_URL — the sharing link of the destination folder
("Copy link" in SharePoint/OneDrive).
"""
import os, re, time, base64, threading
from pathlib import Path
from urllib.parse import quote

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
CHUNK = 320 * 1024 * 16   # 5 MiB — Graph requires multiples of 320 KiB
DELEGATED_SCOPE = "https://graph.microsoft.com/Files.ReadWrite.All offline_access"


class SharePointStorage:
    def __init__(self, tenant_id: str, client_id: str, folder_url: str,
                 client_secret: str = "", refresh_token: str = ""):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.folder_url = folder_url
        self._token = None
        self._token_exp = 0.0
        self._target = None          # (drive_id, folder_item_id)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        env = {k: os.getenv(k, "").strip() for k in (
            "MS_TENANT_ID", "MS_CLIENT_ID", "SHAREPOINT_FOLDER_URL",
            "MS_CLIENT_SECRET", "MS_REFRESH_TOKEN")}
        if not (env["MS_TENANT_ID"] and env["MS_CLIENT_ID"] and env["SHAREPOINT_FOLDER_URL"]):
            return None
        if not (env["MS_CLIENT_SECRET"] or env["MS_REFRESH_TOKEN"]):
            return None
        return cls(env["MS_TENANT_ID"], env["MS_CLIENT_ID"], env["SHAREPOINT_FOLDER_URL"],
                   client_secret=env["MS_CLIENT_SECRET"], refresh_token=env["MS_REFRESH_TOKEN"])

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        with self._lock:
            if not self._token or time.time() > self._token_exp - 120:
                if self.client_secret:
                    form = {
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    }
                else:
                    form = {
                        "grant_type": "refresh_token",
                        "client_id": self.client_id,
                        "refresh_token": self.refresh_token,
                        "scope": DELEGATED_SCOPE,
                    }
                r = httpx.post(
                    f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                    data=form, timeout=30,
                )
                if r.status_code == 400 and not self.client_secret:
                    raise RuntimeError("Microsoft sign-in expired — run get_token.py again "
                                       "and update MS_REFRESH_TOKEN on Render")
                r.raise_for_status()
                data = r.json()
                self._token = data["access_token"]
                # Microsoft rotates refresh tokens; keep the newest one for this process
                self.refresh_token = data.get("refresh_token", self.refresh_token)
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

    def _children(self) -> list:
        """All items in the folder (follows paging)."""
        drive_id, folder_id = self._folder()
        url = f"{GRAPH}/drives/{drive_id}/items/{folder_id}/children"
        params = {"$select": "name,size,webUrl,lastModifiedDateTime", "$top": "999"}
        items = []
        while url:
            r = httpx.get(url, params=params, headers=self._headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
            items += data.get("value", [])
            url, params = data.get("@odata.nextLink"), None
        return items

    def next_serial(self) -> int:
        """1 + the highest NNN_ prefix already in the folder."""
        nums = [int(m.group(1)) for i in self._children()
                if (m := re.match(r"(\d+)_", i["name"]))]
        return max(nums, default=0) + 1

    def list_videos(self) -> list:
        items = [i for i in self._children() if i["name"].lower().endswith(".mp4")]
        items.sort(key=lambda i: i["lastModifiedDateTime"], reverse=True)
        return [{"name": i["name"], "size_mb": round(i.get("size", 0) / 1_048_576, 1),
                 "web_url": i["webUrl"]} for i in items]
