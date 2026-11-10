"""Host agent (Windows). P2 sees ONLY the File Explorer window, can browse folders,
but cannot open files. Files move only after the owner approves each request.
Run: python agent.py ws://SERVER:8000/ws/agent
All rules are enforced HERE, so a modified viewer cannot bypass them."""
import argparse, asyncio, ctypes, hashlib, io, json, os, re, secrets, shutil, subprocess, sys, tempfile, threading, time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlparse

if sys.platform != "win32":
    sys.exit("This version needs Windows (it controls File Explorer).")

import mss, pyautogui, websockets, win32gui, win32com.client
from PIL import Image

ctypes.windll.user32.SetProcessDPIAware()
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

FPS, MAX_W, JPEG_Q = 8, 1280, 60
CHUNK, MAX_BYTES = 64 * 1024, 500 * 1024 * 1024
REQUEST_TTL = 300                        # seconds the owner has to answer a request (5 minutes)
NAV_KEYS = {"up", "down", "left", "right", "home", "end", "pageup", "pagedown", "backspace", "enter"}
MIN_GAP = ctypes.windll.user32.GetDoubleClickTime() / 1000 + 0.1  # no accidental OS double-clicks
APPDIR = Path(os.environ.get("APPDATA", ".")) / "MiniRemote"
APPDIR.mkdir(parents=True, exist_ok=True)
LOG, CONFIG = APPDIR / "audit.log", APPDIR / "config.json"
sct = (getattr(mss, "MSS", None) or mss.mss)()
shell = None
CUR = {"hwnd": None, "rect": None}      # the Explorer window currently shared
LAST = {"click": 0.0, "dblclick": 0.0}
DIALOG_LOCK = threading.Lock()          # one popup at a time


def load_cfg():
    try: return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception: return {}


def save_cfg(c):
    CONFIG.write_text(json.dumps(c, indent=2), encoding="utf-8")


def normalize(u):
    u = re.sub(r"^http://", "ws://", re.sub(r"^https://", "wss://", u.strip().rstrip("/")))
    if not u.startswith(("ws://", "wss://")): u = "wss://" + u
    return u if u.endswith("/ws/agent") else u + "/ws/agent"


def log(line):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def ask(title, text):
    return ctypes.windll.user32.MessageBoxW(0, text, title, 0x4 | 0x20 | 0x40000 | 0x10000) == 6


def fmt(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


# ---------- consent popup: shows WHO is asking, with a countdown ----------
def who_of(m):
    """Requester identity. It comes from the server (the signed-in Google account), never from the viewer."""
    r = m.get("requester") if isinstance(m.get("requester"), dict) else {}
    email = str(r.get("email") or m.get("who") or "unknown")
    return {"name": str(r.get("name") or email), "email": email, "picture": str(r.get("picture") or "")}


def fetch_avatar(who, size=64):
    img = None
    try:
        url = who.get("picture", "")
        if url.startswith("https://") and (urlparse(url).hostname or "").endswith("googleusercontent.com"):
            with urllib.request.urlopen(url, timeout=3) as r:
                img = Image.open(io.BytesIO(r.read(2_000_000))).convert("RGB")
    except Exception:
        img = None
    if img is None:  # no picture: a green tile with the first letter
        from PIL import ImageDraw
        img = Image.new("RGB", (size, size), "#0f6b4f")
        ImageDraw.Draw(img).text((size // 2 - 4, size // 2 - 6), (who["name"][:1] or "?").upper(), fill="white")
    return img.resize((size, size))


def popup(title, who, lines, timeout=REQUEST_TTL):
    """Returns 'accept', 'deny' or 'expired'. Nothing is accepted unless the owner clicks Allow."""
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageTk
    with DIALOG_LOCK:
        pic = fetch_avatar(who)
        result = {"v": "expired"}
        root = tk.Tk()
        root.title(title); root.attributes("-topmost", True); root.resizable(False, False)
        photo = ImageTk.PhotoImage(pic)
        frm = ttk.Frame(root, padding=16); frm.pack()
        lbl = ttk.Label(frm, image=photo); lbl.image = photo; lbl.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        ttk.Label(frm, text=who["name"], font=("Segoe UI", 13, "bold")).grid(row=0, column=1, sticky="sw")
        ttk.Label(frm, text=who["email"]).grid(row=1, column=1, sticky="nw")
        ttk.Label(frm, text="\n\n".join(lines), wraplength=440, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=12)
        left_lbl = ttk.Label(frm, foreground="#a3322a"); left_lbl.grid(row=3, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(frm); bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def finish(v):
            result["v"] = v; root.destroy()
        deny_btn = ttk.Button(bar, text="Deny", command=lambda: finish("deny")); deny_btn.pack(side="right", padx=(8, 0))
        ttk.Button(bar, text="Allow", command=lambda: finish("accept")).pack(side="right")
        end = time.time() + timeout

        def tick():
            left = int(end - time.time())
            if left <= 0:
                result["v"] = "expired"; root.destroy(); return
            left_lbl.config(text=f"No answer = denied. Expires in {left // 60}:{left % 60:02d}")
            root.after(500, tick)
        root.protocol("WM_DELETE_WINDOW", lambda: finish("deny"))
        root.bind("<Escape>", lambda e: finish("deny"))
        root.update_idletasks()
        root.geometry(f"+{(root.winfo_screenwidth() - root.winfo_width()) // 2}+{(root.winfo_screenheight() - root.winfo_height()) // 3}")
        root.lift(); root.focus_force(); deny_btn.focus_set()   # Deny is the default, so a stray Enter never allows
        tick(); root.mainloop()
        return result["v"]


# ---------- File Explorer window helpers ----------
def explorer_hwnd():
    """The foreground window, but only if it is a File Explorer folder window."""
    h = win32gui.GetForegroundWindow()
    if h and win32gui.GetClassName(h) == "CabinetWClass" and win32gui.IsWindowVisible(h) and not win32gui.IsIconic(h):
        return h
    return None


def window_rect(h):
    r = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return r.left, r.top, r.right, r.bottom
    return win32gui.GetWindowRect(h)


def open_explorer():
    subprocess.Popen(["explorer.exe", "shell:MyComputerFolder"])


def selected_items():
    """Items selected in the shared Explorer window (read by the agent, never typed by P2)."""
    global shell
    try:
        if shell is None:
            shell = win32com.client.Dispatch("Shell.Application")
        for w in shell.Windows():
            try:
                if w.HWND == CUR["hwnd"]:
                    its = w.Document.SelectedItems()
                    return [its.Item(i) for i in range(its.Count)]
            except Exception:
                continue
    except Exception as e:
        log(f"selection read failed: {e}")
    return []


def only_folders_selected():
    items = selected_items()
    return bool(items) and all(i.IsFolder for i in items)


def in_file_area(x, y):
    """True only over the file list or folder tree - not the ribbon, toolbar or address bar."""
    h = win32gui.WindowFromPoint((x, y))
    for _ in range(20):
        if not h: return False
        cls = win32gui.GetClassName(h)
        if cls in ("SHELLDLL_DefView", "NamespaceTreeControl"): return True
        if cls == "CabinetWClass": return False
        h = win32gui.GetParent(h)
    return False


# ---------- screen: only the Explorer window is captured ----------
def grab_explorer():
    h = explorer_hwnd()
    if not h: return None
    l, t, r, b = window_rect(h)
    v = sct.monitors[0]
    l, t = max(l, v["left"]), max(t, v["top"])
    r, b = min(r, v["left"] + v["width"]), min(b, v["top"] + v["height"])
    if r - l < 100 or b - t < 100: return None
    shot = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
    if explorer_hwnd() != h: return None   # window changed during capture: drop the frame
    CUR["hwnd"], CUR["rect"] = h, (l, t, r, b)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > MAX_W:
        img = img.resize((MAX_W, int(img.height * MAX_W / img.width)), Image.Resampling.BILINEAR)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=JPEG_Q)
    return buf.getvalue()


async def stream(ws):
    last, notified = None, 0.0
    while True:
        t0 = time.time()
        data = grab_explorer()
        if data is None:                      # not a File Explorer window: show nothing
            CUR["hwnd"] = None; last = None
            if time.time() - notified > 1:
                await ws.send(json.dumps({"type": "restricted"})); notified = time.time()
        elif data != last:
            await ws.send(b"\x01" + data); last = data
        await asyncio.sleep(max(0.05, 1 / FPS - (time.time() - t0)))


# ---------- input: strict allow-list ----------
POLICY = {"max_file_mb": 500, "allow_folders": True}   # set by the admin, pushed by the server


def apply_input(m):
    """Apply one allowed input event. Returns a reason string when the event was blocked."""
    if CUR["hwnd"] is None or explorer_hwnd() != CUR["hwnd"]:
        return None
    t = m.get("type")
    if t == "mouse":
        l, tp, r, b = CUR["rect"]
        nx = min(max(float(m.get("x", 0)), 0), 1); ny = min(max(float(m.get("y", 0)), 0), 1)
        x, y = int(l + nx * (r - l - 1)), int(tp + ny * (b - tp - 1))
        a = m.get("action")
        if a == "move":
            pyautogui.moveTo(x, y)
        elif a == "scroll":
            pyautogui.moveTo(x, y); pyautogui.scroll(int(max(-1200, min(1200, float(m.get("dy", 0))))))
        elif a in ("click", "dblclick"):
            now = time.monotonic()
            if now - LAST[a] < MIN_GAP: return None
            LAST[a] = now
            if not in_file_area(x, y):
                return "click outside the file list"
            if a == "click":
                pyautogui.click(x, y)         # left single click only: select / navigate tree
            elif only_folders_selected():     # "double-click" = open, allowed for folders only
                pyautogui.press("enter")
            else:
                return "open blocked: selection is not a folder"
    elif t == "key":
        k = m.get("key")
        if k not in NAV_KEYS: return None     # no typing, no shortcuts, no Delete/F2/Ctrl+...
        if k == "enter" and not only_folders_selected():
            return "Enter blocked: selection is not a folder"
        pyautogui.press(k)
    return None


async def audit(ws, event, **detail):
    """Tell the server about something only this host knows (shown in the admin audit log)."""
    try:
        await ws.send(json.dumps({"type": "audit", "event": event, "detail": detail}))
    except Exception:
        pass


# ---------- file / folder request (path is read from P2's selection, never typed) ----------
def describe(p: Path):
    if p.is_file():
        return "file", p.stat().st_size, 1
    total = n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f)); n += 1
            except OSError:
                pass
        if total > MAX_BYTES:
            break
    return "folder", total, n


async def handle_file_request(ws, m):
    rid = str(m.get("req_id", ""))      # chosen by the server, so history rows always match
    who = who_of(m)

    async def deny(reason, code="denied"):
        await ws.send(json.dumps({"type": "file_denied", "req_id": rid, "reason": reason, "code": code}))

    items = selected_items()
    if len(items) != 1:
        log("FILE request rejected: need exactly one selected item")
        await audit(ws, "file_decision", decision="rejected", reason="need exactly one selected item")
        return await deny("Select exactly one file or folder first", "rejected")
    try:
        p = Path(items[0].Path).resolve(strict=True)
    except Exception:
        log("FILE request rejected: not a real path")
        await audit(ws, "file_decision", decision="rejected", reason="not a real path")
        return await deny("That item can't be sent (not a real file or folder)", "rejected")
    kind, size, count = await asyncio.to_thread(describe, p)
    # tell the server what was asked for, so the transfer history has the name, path and size
    await ws.send(json.dumps({"type": "file_info", "req_id": rid, "name": p.name or str(p), "path": str(p), "size": size, "kind": kind}))
    limit = min(MAX_BYTES, int(POLICY["max_file_mb"]) * 1024 * 1024)
    if kind == "folder" and not POLICY["allow_folders"]:
        log(f"POLICY blocked folder: {p}")
        await audit(ws, "policy_block", path=str(p), reason="folders not allowed")
        return await deny("Folders are not allowed for this device (admin policy)", "policy")
    if size > limit:
        log(f"POLICY blocked size: {p}")
        await audit(ws, "policy_block", path=str(p), size=size, reason=f"over {POLICY['max_file_mb']} MB limit")
        return await deny(f"Too large (limit {POLICY['max_file_mb']} MB)", "policy")
    extra = f" ({count} files)" if kind == "folder" else ""
    decision = await asyncio.to_thread(popup, "File transfer request", who, [
        f"{who['name']} ({who['email']}) wants the {kind} '{p.name or p}' from your computer.",
        f"Location: {p}\nSize: {fmt(size)}{extra}\n\nAllow or Deny?"])
    log(f"FILE request {kind} {p} from {who['email']} -> {decision.upper()}")
    await audit(ws, "file_decision", path=str(p), kind=kind, size=size, requester=who["email"],
                decision={"accept": "ACCEPTED", "deny": "DENIED", "expired": "EXPIRED"}[decision])
    if decision == "expired":
        return await deny("The owner did not answer within 5 minutes", "expired")
    if decision != "accept":
        return await deny("Denied by the owner", "denied")
    tmpdir = None
    try:
        if kind == "folder":
            tmpdir = tempfile.mkdtemp()
            zp = await asyncio.to_thread(shutil.make_archive, os.path.join(tmpdir, p.name or "folder"), "zip", str(p))
            send_path, name = Path(zp), (p.name or "folder") + ".zip"
        else:
            send_path, name = p, p.name
        await ws.send(json.dumps({"type": "file_offer", "req_id": rid, "name": name, "size": send_path.stat().st_size}))
        h = hashlib.sha256()
        with send_path.open("rb") as f:
            while True:
                chunk = await asyncio.to_thread(f.read, CHUNK)
                if not chunk: break
                h.update(chunk); await ws.send(b"\x02" + chunk)
        await ws.send(json.dumps({"type": "file_done", "req_id": rid, "sha256": h.hexdigest()}))
    except Exception as e:
        log(f"FILE transfer failed: {e}")
        try: await deny("The transfer failed on the host", "failed")
        except Exception: pass
    finally:
        if tmpdir: shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- session ----------
async def handle_connect(ws, st, m):
    gen, who = st["gen"], who_of(m)
    decision = await asyncio.to_thread(popup, "Incoming connection", who, [
        f"{who['name']} ({who['email']}) wants to see your File Explorer window.",
        "Other windows stay hidden, and they cannot open your files.\n\nAllow or Deny?"])
    if gen != st["gen"]: return
    if decision != "accept":
        log(f"CONNECT {decision} ({who['email']})")
        return await ws.send(json.dumps({"type": "connect_response", "accepted": False, "expired": decision == "expired"}))
    ctl = await asyncio.to_thread(ask, "Browsing",
        "Let them click through your folders?\n\nYes = browse folders\nNo = watch only")
    if gen != st["gen"]: return
    st["active"], st["control"] = True, ctl
    log(f"CONNECT accepted ({who['email']}, {'browse' if ctl else 'watch-only'})")
    await ws.send(json.dumps({"type": "connect_response", "accepted": True, "view_only": not ctl}))
    open_explorer()
    st["stream"] = asyncio.create_task(stream(ws))


def end_session(st):
    st["gen"] += 1; st["active"] = False
    if st.get("stream"):
        st["stream"].cancel(); st["stream"] = None
    CUR["hwnd"] = None
    log("SESSION ended")


async def heartbeat(ws):
    while True:  # also keeps free hosting from sleeping while a device is online
        await asyncio.sleep(25)
        await ws.send(json.dumps({"type": "ping"}))


async def run(url, cfg):
    async with websockets.connect(url, max_size=None, ping_interval=20, open_timeout=60) as ws:
        await ws.send(json.dumps({"type": "register", "device_key": cfg["device_key"]}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "registered":
            print("Server refused this device:", reply.get("message")); raise SystemExit(1)
        POLICY.update(reply.get("policy") or {})
        print(f"\n  Registered as: {reply['device']}\n  Waiting for connections. Ctrl+C to stop.\n")
        st = {"gen": 0, "active": False, "control": False, "stream": None, "opened": 0.0, "last_block": 0.0}
        tasks, hb = set(), asyncio.create_task(heartbeat(ws))
        try:
            async for raw in ws:
                if isinstance(raw, bytes): continue
                m = json.loads(raw); t = m.get("type")
                if t == "connect_request":
                    tk = asyncio.create_task(handle_connect(ws, st, m))
                elif t == "viewer_left":
                    end_session(st); continue
                elif t == "policy":
                    POLICY.update(m.get("policy") or {}); log(f"POLICY updated {POLICY}"); continue
                elif t in ("mouse", "key"):
                    if st["active"] and st["control"]:
                        try: reason = apply_input(m)
                        except Exception as e: print("input error:", e); reason = None
                        if reason:
                            log(f"BLOCKED {reason}")
                            if time.time() - st["last_block"] > 2:
                                st["last_block"] = time.time(); await audit(ws, "blocked_action", reason=reason)
                    continue
                elif t == "open_explorer":
                    if st["active"] and time.time() - st["opened"] > 2:
                        st["opened"] = time.time(); open_explorer()
                    continue
                elif t == "file_request" and st["active"]:
                    tk = asyncio.create_task(handle_file_request(ws, m))
                else:
                    continue
                tasks.add(tk); tk.add_done_callback(tasks.discard)
        finally:
            hb.cancel(); end_session(st)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="server address, e.g. https://your-app.onrender.com")
    a = ap.parse_args()
    cfg = load_cfg()
    url = a.url or os.environ.get("MINIREMOTE_URL") or cfg.get("server") or input("Server address (e.g. https://your-app.onrender.com): ")
    key = os.environ.get("MINIREMOTE_KEY") or cfg.get("device_key") or input("Device key (from your admin): ").strip()
    cfg["server"], cfg["device_key"] = normalize(url), key
    save_cfg(cfg)
    while True:
        try:
            asyncio.run(run(cfg["server"], cfg))
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("connection lost:", e, "- retrying in 5s"); time.sleep(5)