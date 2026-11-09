# Mini Remote (team edition)

Consent-based remote File Explorer sharing for teams. Accounts, registered devices, per-user access,
per-device policy, and a central tamper-evident audit log. The host owner still approves every
connection and every single file.

```
Host PC (agent.py/exe) <--WebSocket--> Server (FastAPI + DB) <--WebSocket--> Browser (signed-in user)
```

## Roles
- Admin: manages users, devices, access and policy; sees the audit log; can connect to any device.
- Member: sees and connects only to devices an admin granted.

## Run locally (one PC to try it)
1. `cd server`, `pip install -r requirements.txt`, `uvicorn main:app --host 0.0.0.0 --port 8000`
   First start prints the admin login (or set ADMIN_EMAIL / ADMIN_PASSWORD). Data is in `data.db`.
2. Open http://localhost:8000, sign in, Admin > Devices > create "Test PC". Copy the device key (shown once).
3. Host PC: `cd agent`, `pip install -r requirements.txt`, `python agent.py http://<server>:8000`, paste the key.
4. Admin > Devices > tick which members may connect. Members sign in and press Connect.

## Host rules (enforced by agent.py, not the browser)
Viewer sees only the File Explorer window; can browse and open folders; cannot open files, right-click, drag, type,
delete or use shortcuts; requests the selected item; owner approves each one. Admin policy per device:
max file size and whether folders may be sent (pushed live to the agent).

## Audit log (Admin > Audit log)
Logins (ok/failed/blocked), connections (requested/accepted/denied), sessions, each file request with the host's
decision and path, blocked actions, policy/access/user/device changes. Filter by user, device, event, date;
Export CSV; "Verify chain" proves no entry was edited or removed (each entry hashes the previous one).

## Free hosting: Render (app) + Neon (database)
Render's free disk is wiped on every restart, so keep data in a free Postgres (Neon).
1. neon.com: create a project, copy the connection string (postgresql://...?sslmode=require).
2. Push this folder to GitHub. Render: New > Blueprint > pick the repo (uses render.yaml).
3. In Render > Environment set DATABASE_URL (Neon string), ADMIN_EMAIL, ADMIN_PASSWORD. SECRET_KEY is generated.
4. Open https://<name>.onrender.com and sign in. Run exactly ONE instance (audit chain assumes it).

## Agent as an exe
Windows PC: `agent\build-agent.bat` -> `agent\dist\MiniRemoteAgent.exe`. First run asks for the server address and
device key, then remembers them in %APPDATA%\MiniRemote\config.json (audit.log is kept there too).
Revoke a device any time with Admin > Devices > New device key.

## Limits
Use only on computers you own or are authorised to manage. Thumbnails and the Preview pane show file contents:
turn the Preview pane off. Free Render sleeps when idle (first connect can take ~30-60 s).
