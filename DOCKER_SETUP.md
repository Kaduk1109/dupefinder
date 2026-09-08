# Installing Duplicate Image Finder via Synology Container Manager

This packages the app as a Docker image so DSM handles the Python
environment for you — no manual `pip install` on the NAS itself.

I couldn't build/test the actual Docker image in my sandbox (no network
access there), so **please do a smoke test** (Step 7) before pointing it at
your full 120k-image library.

## 1. Get the project files onto the NAS

Copy the whole `dupefinder` folder (everything you downloaded, including
`Dockerfile`, `docker-compose.yml`, and the `.py`/`templates`/`static`
files) to a shared folder on the NAS — e.g. via File Station's upload, or
drag-and-drop over SMB if you have a share mapped. A reasonable spot:

```
/volume1/docker/dupefinder/            <- project files (Dockerfile, app.py, ...)
/volume1/docker/dupefinder/app_data/   <- leave empty; the DB + logs will live here
```

## 2. Confirm Container Manager is installed

Package Center → search "Container Manager" → Install, if it isn't already.
(On older DSM versions this is called "Docker" — same thing, same steps
below.)

## 3. Edit the two volume paths in `docker-compose.yml`

Open `docker-compose.yml` (in File Station's text editor, or on your own
computer before uploading) and change:

```yaml
- /volume1/photo:/volume1/photo
```

to your actual photo library's shared-folder path — check it in **Control
Panel → Shared Folder**, or right-click a folder in File Station → Properties.
Keep the left and right side identical (that's what lets you type the exact
path you see in File Station into the app later, instead of translating
container paths in your head).

Also update the `app_data` line to a real path on your NAS if
`/volume1/docker/dupefinder/app_data` isn't where you put things.

## 4. Create the Project in Container Manager

1. Open **Container Manager** → **Project** (left sidebar) → **Create**.
2. **Project name**: `dupefinder`.
3. **Path**: browse to the folder containing your `docker-compose.yml`
   (`/volume1/docker/dupefinder`).
4. **Source**: choose "Use existing docker-compose.yml" — it should
   auto-detect the file at that path.
5. Click **Next**. Container Manager will show you the compose file contents
   for review — confirm your edited volume paths are there.
6. Click **Next**, then **Done**. It will build the image (this runs `pip
   install` inside the container, so it needs internet access from the
   NAS — that's normal and separate from any restriction in my sandbox)
   and start the container.

Build takes a few minutes the first time. You can watch progress under
**Project → dupefinder → Logs**.

## 5. Confirm it's running

**Container Manager → Container**: you should see `dupefinder` with status
"Running". Click it → **Details** → **Logs** — you're looking for:

```
* Running on all addresses (0.0.0.0)
* Running on http://127.0.0.1:5000
```

If it exited instead, open Logs to see the error — most likely cause is a
volume path in `docker-compose.yml` that doesn't exist on the NAS yet.

## 6. Open the app

`http://<your-nas-ip>:5000`

If port 5000 is already used by something else on your NAS, change the left
side of `ports: - "5000:5000"` in the compose file (e.g. `"5050:5000"`) and
redeploy the project.

## 7. Smoke-test before the full library

In the web UI, point the scan at a **small subfolder first** (a few hundred
photos) — enter the path exactly as it appears in File Station, e.g.
`/volume1/photo/2024`. Confirm:
- the progress bar advances and completion notification appears in
  `/volume1/docker/dupefinder/app_data/scanner.log`
- the review page shows sensible duplicate groups
- deleting a test duplicate actually creates
  `<folder>/_DupeFinder_Trash/...`

Once that looks right, rerun a scan pointed at the real library root. It
will pick up where a previous scan left off if interrupted (Scenario 7), so
there's no harm in stopping/restarting the container mid-scan if needed —
just restart the scan from the web UI afterward.

## Updating the app later

If you edit any of the `.py`/template/static files, redeploy via
**Container Manager → Project → dupefinder → Action → Build** (or `docker
compose up -d --build` over SSH if you prefer the command line). Your DB and
logs are untouched since they live in the separate `/data` volume, not
inside the image.

## If you'd rather use SSH instead of the GUI

Enable SSH (**Control Panel → Terminal & SNMP**), then:

```bash
ssh admin@<nas-ip>
cd /volume1/docker/dupefinder
sudo docker compose up -d --build
```

Same result as the Container Manager GUI steps above — useful if the GUI's
Project import behaves differently across DSM versions.
