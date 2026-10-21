"""One-time Microsoft sign-in that prints a refresh token for MS_REFRESH_TOKEN.

Usage:  python get_token.py <tenant-id> <client-id>
Then open the link shown, enter the code, and sign in with your Microsoft 365 account.
Re-run it when the app reports "Microsoft sign-in expired" (about every 90 days).
"""
import sys, time
import httpx

SCOPE = "https://graph.microsoft.com/Files.ReadWrite.All offline_access"


def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: python get_token.py <tenant-id> <client-id>")
    tenant, client_id = sys.argv[1].strip(), sys.argv[2].strip()
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

    r = httpx.post(f"{base}/devicecode", data={"client_id": client_id, "scope": SCOPE}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Could not start sign-in: {r.text}")
    flow = r.json()
    print("\n" + flow["message"] + "\n")

    interval = int(flow.get("interval", 5))
    deadline = time.time() + int(flow.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        r = httpx.post(f"{base}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": flow["device_code"],
        }, timeout=30)
        data = r.json()
        if r.status_code == 200:
            print("Signed in. Paste this into Render as MS_REFRESH_TOKEN:\n")
            print(data["refresh_token"])
            return
        err = data.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        sys.exit(f"Sign-in failed: {data.get('error_description', data)}")
    sys.exit("Sign-in timed out — run the script again.")


if __name__ == "__main__":
    main()
