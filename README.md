# TuringIQ Video Generator (hosted version)

Upload slides (PPTX/PPT or a ZIP of images) + a narration transcript → get a narrated MP4 with subtitles.
Runs in Docker on Linux, so it can be hosted on Render and used from any phone, tablet or PC browser.

How it works on the server:
- **One video at a time.** If someone else is generating, you get a "please try again" message.
- **Before each new video, all files from the previous one are deleted**, so the disk and memory never fill up.
- Finished videos (and `.srt` subtitles) are **uploaded to a SharePoint / OneDrive folder**. That folder is the permanent storage.
- The site is protected by a **shared password** (`APP_PASSWORD`). Any username works.

---

## 1. Deploy on Render (free plan)

1. Push this repo to GitHub (already done).
2. On <https://dashboard.render.com> → **New → Blueprint** → pick this repo. Render reads `render.yaml`.
3. Fill in the environment variables it asks for:

| Variable | Value |
|---|---|
| `SARVAM_API_KEY` | Your Sarvam AI key |
| `APP_PASSWORD` | Password users type to open the site |
| `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_REFRESH_TOKEN` | From step 2 below |
| `SHAREPOINT_FOLDER_URL` | Sharing link of the destination folder (step 2) |

4. Deploy. The first build takes ~5–10 minutes. Your URL looks like `https://turingiq-video-generator.onrender.com`.

**Free plan limits:** the service sleeps after 15 minutes without visits (the first visit after that takes ~1 minute to wake up), and it has 512 MB RAM. Draft mode is the safest; quality mode with many slides may run out of memory.

## 2. Connect a SharePoint / OneDrive folder (no admin needed)

The app uploads as **you** (your Microsoft account), so no admin consent is required.

1. <https://entra.microsoft.com> → **App registrations → New registration** → name `TuringIQ Video Uploader` → Register.
2. Copy **Application (client) ID** → `MS_CLIENT_ID` and **Directory (tenant) ID** → `MS_TENANT_ID`.
3. **Authentication** → *Advanced settings* → **Allow public client flows = Yes** → Save.
4. **API permissions → Add a permission → Microsoft Graph → Delegated permissions** → tick
   `Files.ReadWrite.All` and `offline_access` → Add. (Do **not** click "Grant admin consent".)
   Remove the *Application* permission `Files.ReadWrite.All` if you added it earlier.
5. On your PC, in this folder:
   ```bash
   pip install httpx
   python get_token.py <tenant-id> <client-id>
   ```
   Open the link it prints, enter the code, sign in, and accept the permissions.
   Copy the long token it prints → `MS_REFRESH_TOKEN` on Render.
6. Open the destination folder in SharePoint/OneDrive → **Share / Copy link** → `SHAREPOINT_FOLDER_URL`.
   Share the same folder with whoever should see the videos.

**Renewal:** the sign-in lasts about 90 days (and ends if you change your password).
When the app shows *"Microsoft sign-in expired"*, re-run step 5 and update `MS_REFRESH_TOKEN`
in Render → your service → **Environment**.

*Alternative (admin consent):* use an **Application** permission `Files.ReadWrite.All` with admin
consent and set `MS_CLIENT_SECRET` instead of `MS_REFRESH_TOKEN`. This never expires except for the secret.

If the SharePoint variables are left empty, the app still works, but videos are only downloadable until the next video starts.

## 3. Run locally with Docker (optional)

```bash
cp .env.example .env       # fill in values
docker build -t tiq-video .
docker run --rm -p 8000:8000 --env-file .env tiq-video
```
Open <http://localhost:8000>.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SARVAM_API_KEY` | yes | — | |
| `APP_PASSWORD` | recommended | empty (no login) | |
| `MS_TENANT_ID` / `MS_CLIENT_ID` / `MS_REFRESH_TOKEN` / `SHAREPOINT_FOLDER_URL` | optional | — | All four needed for SharePoint upload (`MS_CLIENT_SECRET` can replace the refresh token) |
| `MAX_CLIP_WORKERS` | no | `1` | Parallel ffmpeg encodes. Raise on bigger plans |
