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
| `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | From step 2 below |
| `SHAREPOINT_FOLDER_URL` | Sharing link of the destination folder (step 2) |

4. Deploy. The first build takes ~5–10 minutes. Your URL looks like `https://turingiq-video-generator.onrender.com`.

**Free plan limits:** the service sleeps after 15 minutes without visits (the first visit after that takes ~1 minute to wake up), and it has 512 MB RAM. Draft mode is the safest; quality mode with many slides may run out of memory.

## 2. Connect a SharePoint / OneDrive folder

You need one app registration in Microsoft Entra ID (Azure AD). A Microsoft 365 admin must grant consent.

1. <https://entra.microsoft.com> → **App registrations → New registration** → name it `TuringIQ Video Uploader` → Register.
2. Copy **Application (client) ID** → `MS_CLIENT_ID`, and **Directory (tenant) ID** → `MS_TENANT_ID`.
3. **Certificates & secrets → New client secret** → copy the *Value* → `MS_CLIENT_SECRET`. (Note the expiry date and renew it before it runs out.)
4. **API permissions → Add → Microsoft Graph → Application permissions → `Files.ReadWrite.All`** → **Grant admin consent**.
5. In SharePoint/OneDrive, open the folder where videos should go → **Share / Copy link** → paste that link as `SHAREPOINT_FOLDER_URL`.
   Share the same folder (view or edit) with whoever should see the videos.

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
| `MS_TENANT_ID` / `MS_CLIENT_ID` / `MS_CLIENT_SECRET` / `SHAREPOINT_FOLDER_URL` | optional | — | All four needed for SharePoint upload |
| `MAX_CLIP_WORKERS` | no | `1` | Parallel ffmpeg encodes. Raise on bigger plans |
