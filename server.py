# ---------------- server.py ----------------
import sqlite3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import socketio
import socket
import os
import asyncio
from fastapi.staticfiles import StaticFiles
import aiohttp
import json
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv
from urllib.parse import urlparse
import hashlib
from collections import deque

# ---------------- Globals ----------------
DB_PATH = "chat.db"
DESTROYED_ROOMS = set()
ROOM_USERS = {}  # { room: { username: sid } }
LAST_MESSAGE = {}  # {(room, username): (text, ts)}
subscriptions: dict[str, list[dict]] = {}  # username -> [subscription objects]

# Push de-duplication: per-endpoint recent payload IDs sent
PUSH_RECENT: dict[str, deque] = {}  # endpoint -> deque of (push_id, ts)
PUSH_RECENT_MAX = 100
PUSH_RECENT_WINDOW = timedelta(seconds=30)

# Load environment variables
load_dotenv()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")


# ---------------- Database ----------------
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
    existing_cols = [row[1] for row in c.fetchall()]
    if "filename" not in existing_cols:
        c.execute("ALTER TABLE messages ADD COLUMN filename TEXT")
    if "mimetype" not in existing_cols:
        c.execute("ALTER TABLE messages ADD COLUMN mimetype TEXT")
    if "filedata" not in existing_cols:
        c.execute("ALTER TABLE messages ADD COLUMN filedata TEXT")
    conn.commit()
    conn.close()


def count_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    result = c.fetchone()[0]
    conn.close()
    return result


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
        "INSERT INTO messages (room, sender, text, filename, mimetype, filedata, ts) VALUES (?,?,?,?,?,?,?)",
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
    deleted = cleanup_old_messages()
    if deleted > 0:
        asyncio.create_task(
            sio.emit(
                "cleanup",
                {"message": f"{deleted} old messages (48h+) were removed."},
                room=room,
            )
        )


def load_messages(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT sender, text, filename, mimetype, filedata, ts FROM messages WHERE room=? ORDER BY id ASC",
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

# Single, properly-configured Socket.IO server
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


# ---------------- Helper ----------------
async def broadcast_users(room):
    users = [
        {"name": username, "status": "online"} for username in ROOM_USERS.get(room, {})
    ]
    await sio.emit("users_update", {"room": room, "users": users}, room=room)


def normalize_endpoint(endpoint: str) -> str | None:
    """Normalize push endpoint so the same device matches reliably."""
    if not endpoint:
        return None
    try:
        parsed = urlparse(endpoint)
        # Keep only scheme + netloc + path (drop query/fragment/etc.)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return endpoint.split("?")[0] if endpoint else endpoint


def make_push_id(room: str, sender: str, text: str, timestamp_iso: str) -> str:
    """Stable ID for this push content (same message → same id)."""
    basis = f"{room}|{sender}|{text}|{timestamp_iso}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def should_send_push(endpoint: str, push_id: str, now: datetime) -> bool:
    """
    Return True if we should send (not seen recently).
    Stores id and prunes old entries.
    """
    dq = PUSH_RECENT.setdefault(endpoint, deque())
    # Already seen?
    for pid, t in dq:
        if pid == push_id and (now - t) <= PUSH_RECENT_WINDOW:
            return False
    # Append and prune
    dq.append((push_id, now))
    while len(dq) > PUSH_RECENT_MAX:
        dq.popleft()
    # Also prune old entries by time
    while dq and (now - dq[0][1]) > PUSH_RECENT_WINDOW:
        dq.popleft()
    return True


# ---------------- Routes ----------------
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

    # Already joined with same sid → do nothing
    if old_sid == sid:
        return {"success": True, "message": "Already in room"}

    # If joined from another device/browser → replace old sid
    if old_sid and old_sid != sid:
        try:
            await sio.leave_room(old_sid, room)
        except Exception:
            pass

    ROOM_USERS[room][username] = sid
    await sio.enter_room(sid, room)
    await broadcast_users(room)

    # Send only missed messages
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

    # Don’t send "joined" system msg on reload
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
async def message(sid, data):
    room = data.get("room")
    sender = data.get("sender")
    text = (data.get("text") or "").strip()
    sender_sub = data.get("subscription")  # sender’s subscription object
    now = datetime.now(timezone.utc)

    if not text:
        return

    # Duplicate chat message filter (spam key)
    key = (room, sender)
    last = LAST_MESSAGE.get(key)
    if last and last[0] == text and (now - last[1]).total_seconds() < 1.5:
        return
    LAST_MESSAGE[key] = (text, now)

    if room in DESTROYED_ROOMS:
        return

    # Save to DB
    save_message(room, sender, text=text)

    # Emit to room (real-time via socket.io)
    await sio.emit(
        "message",
        {"sender": sender, "text": text, "ts": now.isoformat()},
        room=room,
    )
    print(f"🟢 Message emitted in room {room}: {sender}: {text}")

    # Build push payload + ID
    payload = {
        "title": f"New message in {room}",
        "body": f"{sender}: {text}",
        "url": f"/?room={room}",
        "timestamp": now.isoformat(),
    }
    push_id = make_push_id(room, sender, text, payload["timestamp"])

    # Extract and normalize sender endpoint
    sender_endpoint = None
    if sender_sub and isinstance(sender_sub, dict):
        sender_endpoint = normalize_endpoint(sender_sub.get("endpoint"))
        print(f"📌 Sender endpoint: {sender_endpoint}")
    else:
        print("⚠️ No subscription provided with message")

    # Iterate over all users’ subscriptions
    for user, subs in list(subscriptions.items()):
        for sub in list(subs):
            if not sub:
                continue

            target_endpoint = normalize_endpoint(sub.get("endpoint"))
            if not target_endpoint:
                continue

            print(f"🔍 Comparing sender={sender_endpoint} vs target={target_endpoint}")

            # 🚫 Skip exact sender device (same endpoint)
            if sender_endpoint and target_endpoint == sender_endpoint:
                print(f"⏭️ Skipping push for sender {sender} (same endpoint)")
                continue

            # 🚫 Skip all pushes for sender if no endpoint info
            if not sender_endpoint and user == sender:
                print(f"⏭️ Skipping all pushes for sender {sender} (no endpoint info)")
                continue

            # 🚫 Skip push if user is active in ANY room
            is_active_anywhere = any(user in users for users in ROOM_USERS.values())
            if is_active_anywhere:
                print(f"⏭️ {user} is active in some room, skipping push.")
                continue

            # ✅ Deduplication check
            if not should_send_push(target_endpoint, push_id, now):
                print(f"⏭️ Duplicate push suppressed for endpoint {target_endpoint}")
                continue

            try:
                print(f"📤 Sending push to {user} ({target_endpoint[:50]}...)")
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:anitsaha976@gmail.com"},
                )
            except WebPushException as e:
                print(f"❌ Push failed for {user}: {e}")

@sio.event
async def file(sid, data):
    room = data["room"]
    if room in DESTROYED_ROOMS:
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
    room = data["room"]
    username = data["sender"]
    if room in ROOM_USERS and username in ROOM_USERS[room]:
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
    # check if sid still mapped to a username
    for room, users in list(ROOM_USERS.items()):
        for username, user_sid in list(users.items()):
            if user_sid == sid:
                # make sure not reconnected with new sid
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


# ---------------- Background Cleanup + KeepAlive ----------------
@app.on_event("startup")
async def startup_tasks():
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
                        print(f"[KeepAlive] Pinged {url} - {resp.status}")
            except Exception as e:
                print(f"[KeepAlive] Error: {e}")
            await asyncio.sleep(300)

    asyncio.create_task(loop_cleanup())
    asyncio.create_task(ping_self())


# ----------------------
# REST endpoint to subscribe
# ----------------------
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

    # Normalize endpoint for reliable deduplication
    endpoint = normalize_endpoint(subscription.get("endpoint"))
    if not endpoint:
        return JSONResponse({"error": "invalid endpoint"}, status_code=400)

    if all(normalize_endpoint(s.get("endpoint")) != endpoint for s in subs):
        subs.append(subscription)

    print(f"✅ Subscription saved for {sender} (total={len(subs)})")
    return {"message": f"Subscribed {sender}"}


# ----------------------
# Test push endpoint
# ----------------------
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
                    print(f"⏭️ Duplicate test push suppressed for {endpoint}")
                    continue
                print(f"📤 Sending push to {user} ({endpoint[:50]}...)")
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:example@domain.com"},
                )
            except WebPushException as e:
                print(f"❌ Push failed for {user}: {e}")
    return {"status": "Push notification sent"}


# ---------------- Static Files ----------------
app.mount("/icons", StaticFiles(directory="icons"), name="icons")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE_DIR, "manifest.json"))


@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "sw.js"))


@app.get("/sitemap.xml")
def sitemap():
    base_url = "https://realtime-chat-1mv3.onrender.com"
    static_pages = [
        "index.html",
        "about.html",
        "blog.html",
        "contact.html",
        "disclaimer.html",
        "privacy-policy.html",
        "terms-of-service.html",
    ]

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for page in static_pages:
        url = page.replace("index.html", "")
        if url == "":
            loc = f"{base_url}/"
        else:
            loc = f"{base_url}/{url}"
        xml += f"  <url><loc>{loc}</loc></url>\n"
    xml += "</urlset>"
    return Response(content=xml, media_type="application/xml")


@app.get("/ads.txt")
def ads():
    return FileResponse("ads.txt", media_type="text/plain")


@app.get("/robots.txt")
def robots():
    return FileResponse("robots.txt", media_type="text/plain")


app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

# ---------------- Run Server ----------------
if __name__ == "__main__":
    import uvicorn

    init_db()
    migrate_db()
    local_ip = socket.gethostbyname(socket.gethostname())
    print("🚀 Server running at:")
    print("   ➤ Local:   http://127.0.0.1:8000")
    print(f"   ➤ Network: http://{local_ip}:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
