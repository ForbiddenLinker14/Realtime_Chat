# ---------------- server.py ----------------
import os
import json
import socket
import sqlite3
import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import socketio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import aiohttp
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv

# ---------------- Globals ----------------
DB_PATH = "chat.db"
DESTROYED_ROOMS = set()
ROOM_USERS = {}  # { room: { username: sid } }
LAST_MESSAGE = {}  # {(room, username): (text, ts)}
subscriptions = {}  # username -> [subscription objects]
USER_STATUS = {}  # sid -> {"user": username, "active": bool}

# Push de-duplication: per-endpoint recent payload IDs sent
PUSH_RECENT = {}  # endpoint -> deque[(push_id, ts)]
PUSH_RECENT_MAX = 100
PUSH_RECENT_WINDOW = timedelta(seconds=30)

# ---------------- Env / VAPID ----------------
load_dotenv()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")  # required on client to subscribe
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")  # required on server to send push
if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
    print("⚠️  Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY in your .env")


# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            sender TEXT NOT NULL,
            text TEXT,
            filename TEXT,
            mimetype TEXT,
            filedata TEXT,
            ts TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def migrate_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA table_info(messages)")
    existing = [r[1] for r in c.fetchall()]
    for col in ("filename", "mimetype", "filedata"):
        if col not in existing:
            c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


def count_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    n = c.fetchone()[0]
    conn.close()
    return n


def cleanup_old_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    before = count_messages()
    c.execute("DELETE FROM messages WHERE ts < datetime('now', '-48 hours')")
    conn.commit()
    after = count_messages()
    conn.close()
    return before - after


def save_message(room, sender, text=None, filename=None, mimetype=None, filedata=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (room, sender, text, filename, mimetype, filedata, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            room,
            sender,
            text,
            filename,
            mimetype,
            filedata,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def load_messages(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT sender, text, filename, mimetype, filedata, ts "
        "FROM messages WHERE room=? ORDER BY id ASC",
        (room,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def clear_room(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE room=?", (room,))
    conn.commit()
    conn.close()


# ---------------- FastAPI + Socket.IO ----------------
app = FastAPI()

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    max_http_buffer_size=10 * 1024 * 1024,
    ping_interval=3,
    ping_timeout=5,
)
sio_app = socketio.ASGIApp(sio, socketio_path="socket.io")
app.mount("/socket.io", sio_app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------- Helpers ----------------
async def broadcast_users(room):
    users = [
        {"name": username, "status": "online"} for username in ROOM_USERS.get(room, {})
    ]
    await sio.emit("users_update", {"room": room, "users": users}, room=room)


def normalize_endpoint(endpoint: str) -> str | None:
    if not endpoint:
        return None
    try:
        p = urlparse(endpoint)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return endpoint.split("?")[0] if endpoint else endpoint


def make_push_id(room: str, sender: str, text: str, timestamp_iso: str) -> str:
    basis = f"{room}|{sender}|{text}|{timestamp_iso}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def should_send_push(endpoint: str, push_id: str, now: datetime) -> bool:
    dq = PUSH_RECENT.setdefault(endpoint, deque())
    for pid, t in dq:
        if pid == push_id and (now - t) <= PUSH_RECENT_WINDOW:
            return False
    dq.append((push_id, now))
    while len(dq) > PUSH_RECENT_MAX:
        dq.popleft()
    while dq and (now - dq[0][1]) > PUSH_RECENT_WINDOW:
        dq.popleft()
    return True


def user_active_foreground(username: str) -> bool:
    for users in ROOM_USERS.values():
        for uname, usid in users.items():
            if uname == username and USER_STATUS.get(usid, {}).get("active") is True:
                return True
    return False


# ---------------- REST: admin-ish ----------------
@app.delete("/clear/{room}")
async def clear_messages(room: str):
    clear_room(room)
    await sio.emit(
        "clear", {"room": room, "message": "Room history cleared."}, room=room
    )
    return JSONResponse({"status": "ok", "message": f"Room {room} cleared."})


@app.delete("/destroy/{room}")
async def destroy_room(room: str):
    clear_room(room)
    DESTROYED_ROOMS.add(room)
    if room in ROOM_USERS:
        del ROOM_USERS[room]
    await sio.emit(
        "clear",
        {"room": room, "message": "Room destroyed. All messages cleared."},
        room=room,
    )
    await sio.emit("room_destroyed", {"room": room}, room=room)

    namespace = "/"
    if namespace in sio.manager.rooms and room in sio.manager.rooms[namespace]:
        sids = list(sio.manager.rooms[namespace][room])
        for sid in sids:
            await sio.leave_room(sid, room, namespace=namespace)
    return JSONResponse({"status": "ok", "message": f"Room {room} destroyed."})


# ---------------- Socket.IO Events ----------------
@sio.event
async def join(sid, data):
    room = data["room"]
    username = data["sender"]
    last_ts = data.get("lastTs")

    if room in DESTROYED_ROOMS:
        DESTROYED_ROOMS.remove(room)

    if room not in ROOM_USERS:
        ROOM_USERS[room] = {}

    old_sid = ROOM_USERS[room].get(username)

    if old_sid == sid:
        return {"success": True, "message": "Already in room"}

    if old_sid and old_sid != sid:
        try:
            await sio.leave_room(old_sid, room)
        except Exception:
            pass

    ROOM_USERS[room][username] = sid
    await sio.enter_room(sid, room)
    await broadcast_users(room)

    # send only missed messages
    for sender_, text, filename, mimetype, filedata, ts in load_messages(room):
        if last_ts and ts <= last_ts:
            continue
        if filename:
            await sio.emit(
                "file",
                {
                    "sender": sender_,
                    "filename": filename,
                    "mimetype": mimetype,
                    "data": filedata,
                    "ts": ts,
                },
                to=sid,
            )
        else:
            await sio.emit(
                "message", {"sender": sender_, "text": text, "ts": ts}, to=sid
            )

    if not old_sid:
        await sio.emit(
            "message",
            {
                "sender": "System",
                "text": f"{username} joined!",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            room=room,
        )
    return {"success": True}


@sio.event
async def status(sid, data):
    # Client sends: { active: true|false }
    # Tracks per-connection foreground/visibility status for push suppression
    user = None
    for room, members in ROOM_USERS.items():
        for uname, usid in members.items():
            if usid == sid:
                user = uname
                break
        if user:
            break
    if not user:
        return
    is_active = bool((data or {}).get("active"))
    USER_STATUS[sid] = {"user": user, "active": is_active}
    print(f"📌 Status update: {user} is now {'ACTIVE' if is_active else 'INACTIVE'}")


@sio.event
async def message(sid, data):
    room = data.get("room")
    sender = data.get("sender")
    text = (data.get("text") or "").strip()
    sender_sub = data.get("subscription")  # sender’s push subscription (optional)
    now = datetime.now(timezone.utc)

    if not text or not room or not sender:
        return

    # duplicate message suppression (spam key)
    key = (room, sender)
    last = LAST_MESSAGE.get(key)
    if last and last[0] == text and (now - last[1]).total_seconds() < 1.5:
        return
    LAST_MESSAGE[key] = (text, now)

    if room in DESTROYED_ROOMS:
        return

    save_message(room, sender, text=text)

    await sio.emit(
        "message", {"sender": sender, "text": text, "ts": now.isoformat()}, room=room
    )
    print(f"🟢 {room} | {sender}: {text}")
    # generate pushId first
    push_id = make_push_id(room, sender, text, now.isoformat())
    payload = {
        "title": "Realtime Chat",
        "sender": sender,
        "text": text,
        "room": room,
        "url": f"/?room={room}",
        "timestamp": now.isoformat(),
        "pushId": push_id,
    }
    
    sender_endpoint = None
    if sender_sub and isinstance(sender_sub, dict):
        sender_endpoint = normalize_endpoint(sender_sub.get("endpoint"))

    for user, subs in list(subscriptions.items()):
        for sub in list(subs):
            if not sub:
                continue
            target_endpoint = normalize_endpoint(sub.get("endpoint"))
            if not target_endpoint:
                continue

            # Skip same endpoint (don't notify the tab that just sent)
            if sender_endpoint and target_endpoint == sender_endpoint:
                continue

            # If we don't know the sender endpoint, still avoid spamming the sender's other subs:
            if not sender_endpoint and user == sender:
                continue

            # Foreground suppression
            if user_active_foreground(user):
                continue

            if not should_send_push(target_endpoint, push_id, now):
                continue

            try:
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:example@domain.com"},
                )
            except WebPushException as e:
                print(f"❌ Push failed for {user}: {e}")


@sio.event
async def file(sid, data):
    room = data.get("room")
    if not room or room in DESTROYED_ROOMS:
        return
    save_message(
        room,
        data["sender"],
        filename=data["filename"],
        mimetype=data["mimetype"],
        filedata=data["data"],
    )
    await sio.emit(
        "file",
        {
            "sender": data["sender"],
            "filename": data["filename"],
            "mimetype": data["mimetype"],
            "data": data["data"],
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )


@sio.event
async def leave(sid, data):
    room = data.get("room")
    username = data.get("sender")
    if room and username and room in ROOM_USERS and username in ROOM_USERS[room]:
        del ROOM_USERS[room][username]
        if not ROOM_USERS[room]:
            del ROOM_USERS[room]
    await sio.leave_room(sid, room)
    await broadcast_users(room)
    await sio.emit("left_room", {"room": room}, to=sid)
    await sio.emit(
        "message",
        {
            "sender": "System",
            "text": f"{username} left!",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )


@sio.event
async def disconnect(sid):
    USER_STATUS.pop(sid, None)
    for room, users in list(ROOM_USERS.items()):
        for username, user_sid in list(users.items()):
            if user_sid == sid:
                if ROOM_USERS[room].get(username) != sid:
                    continue
                del users[username]
                if not users:
                    del ROOM_USERS[room]
                await broadcast_users(room)
                await sio.emit(
                    "message",
                    {
                        "sender": "System",
                        "text": f"{username} disconnected.",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                    room=room,
                )


# ---------------- Background tasks ----------------
@app.on_event("startup")
async def startup_tasks():
    init_db()
    migrate_db()

    async def loop_cleanup():
        while True:
            deleted = cleanup_old_messages()
            if deleted > 0:
                await sio.emit(
                    "cleanup",
                    {"message": f"{deleted} old messages (48h+) were removed."},
                )
            await asyncio.sleep(3600)

    async def ping_self():
        url = "https://realtime-chat-1mv3.onrender.com"
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        print(f"[KeepAlive] {resp.status}")
            except Exception as e:
                print(f"[KeepAlive] Error: {e}")
            await asyncio.sleep(300)

    asyncio.create_task(loop_cleanup())
    asyncio.create_task(ping_self())


# ---------------- Subscribe / Push test ----------------
@app.post("/api/subscribe")
async def subscribe(request: Request):
    body = await request.json()
    subscription = body.get("subscription")
    sender = body.get("sender")
    if not sender or not subscription:
        return JSONResponse(
            {"error": "sender + subscription required"}, status_code=400
        )

    subs = subscriptions.setdefault(sender, [])
    endpoint = normalize_endpoint(subscription.get("endpoint"))
    if not endpoint:
        return JSONResponse({"error": "invalid endpoint"}, status_code=400)
    if all(normalize_endpoint(s.get("endpoint")) != endpoint for s in subs):
        subs.append(subscription)
    print(f"✅ Subscription saved for {sender} (total={len(subs)})")
    return {"message": f"Subscribed {sender}", "vapidPublicKey": VAPID_PUBLIC_KEY}


@app.post("/send-push-notification")
async def send_push_notification():
    now = datetime.now(timezone.utc)
    payload = {
        "title": "Test Message",
        "body": "This is a test notification.",
        "timestamp": now.isoformat(),
    }
    push_id = make_push_id("TEST", "system", payload["body"], payload["timestamp"])
    for user, subs in list(subscriptions.items()):
        for sub in list(subs):
            try:
                endpoint = normalize_endpoint(sub.get("endpoint"))
                if not endpoint:
                    continue
                if not should_send_push(endpoint, push_id, now):
                    continue
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:example@domain.com"},
                )
            except WebPushException as e:
                print(f"❌ Push failed for {user}: {e}")
    return {"status": "ok"}


# ---------------- Static / PWA assets ----------------
app.mount("/icons", StaticFiles(directory="icons"), name="icons")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE_DIR, "manifest.json"))


@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "sw.js"))


@app.get("/sitemap.xml")
def sitemap():
    base_url = "http://127.0.0.1:8000"
    static_pages = ["index.html"]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for page in static_pages:
        url = page.replace("index.html", "")
        loc = f"{base_url}/" if url == "" else f"{base_url}/{url}"
        xml += f"  <url><loc>{loc}</loc></url>\n"
    xml += "</urlset>"
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\n", media_type="text/plain")


# Serve the static SPA root
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

# ---------------- Run ----------------
if __name__ == "__main__":
    import uvicorn

    local_ip = socket.gethostbyname(socket.gethostname())
    print("🚀 Server running at:")
    print("   ➤ Local:   http://127.0.0.1:8000")
    print(f"   ➤ Network: http://{local_ip}:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
