#                                                  ওঁ নমো 
                                                 
# সিদ্ধিদাতা গণেশায় নমঃ                        সিদ্ধিদাতা গণেশায় নমঃ                                   সিদ্ধিদাতা গণেশায় নমঃ

import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
import socketio
import socket
import os
import asyncio
from fastapi.staticfiles import StaticFiles
import aiohttp

DB_PATH = "chat.db"
DESTROYED_ROOMS = set()
ROOM_USERS = {}  # { room: {sid: username} }
DISCONNECT_TIMERS = {}  # sid -> asyncio.Task (delayed disconnect broadcast)
FORCE_DISCONNECT = set()  # track sids that called manual_disconnect


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
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    max_http_buffer_size=10 * 1024 * 1024,
    ping_interval=3,  # send ping every 3s
    ping_timeout=5,  # if no pong reply within 5s -> disconnect
)
app = FastAPI()
sio_app = socketio.ASGIApp(sio, socketio_path="socket.io")
app.mount("/socket.io", sio_app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------- Helper ----------------
async def broadcast_users(room):
    users = [
        {"name": username, "status": "online"}
        for sid, username in ROOM_USERS.get(room, {}).items()
    ]
    await sio.emit("users_update", {"room": room, "users": users}, room=room)


async def handle_disconnect(sid, reason="disconnected"):
    for room, users in list(ROOM_USERS.items()):
        if sid in users:
            username = users[sid]
            del users[sid]
            if not users:
                del ROOM_USERS[room]
            await broadcast_users(room)
            await sio.emit(
                "message",
                {
                    "sender": "System",
                    "text": f"{username} {reason}.",
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
                room=room,
            )
    if sid in DISCONNECT_TIMERS:
        del DISCONNECT_TIMERS[sid]


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
    sender = data["sender"]
    last_ts = data.get("lastTs")

    if sid in DISCONNECT_TIMERS:  # cancel delayed disconnect
        DISCONNECT_TIMERS[sid].cancel()
        del DISCONNECT_TIMERS[sid]

    if room in DESTROYED_ROOMS:
        DESTROYED_ROOMS.remove(room)

    await sio.enter_room(sid, room)
    if room not in ROOM_USERS:
        ROOM_USERS[room] = {}
    ROOM_USERS[room][sid] = sender

    await broadcast_users(room)

    # send missed messages
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

    await sio.emit(
        "message",
        {
            "sender": "System",
            "text": f"{sender} joined!",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )


@sio.event
async def message(sid, data):
    room = data["room"]
    if room in DESTROYED_ROOMS:
        return
    save_message(room, data["sender"], text=data["text"])
    await sio.emit(
        "message",
        {
            "sender": data["sender"],
            "text": data["text"],
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )


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
    sender = data["sender"]
    await sio.leave_room(sid, room)
    if room in ROOM_USERS and sid in ROOM_USERS[room]:
        del ROOM_USERS[room][sid]
        if not ROOM_USERS[room]:
            del ROOM_USERS[room]
    await broadcast_users(room)
    await sio.emit("left_room", {"room": room}, to=sid)
    await sio.emit(
        "message",
        {
            "sender": "System",
            "text": f"{sender} left!",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )


@sio.event
async def manual_disconnect(sid, data):
    FORCE_DISCONNECT.add(sid)  # mark this sid
    await handle_disconnect(sid, reason="disconnected (manual)")
    if sid in DISCONNECT_TIMERS:
        DISCONNECT_TIMERS[sid].cancel()
        del DISCONNECT_TIMERS[sid]


@sio.event
async def manual_reconnect(sid, data):
    await join(
        sid,
        {"room": data["room"], "sender": data["sender"], "lastTs": data.get("lastTs")},
    )


@sio.event
async def disconnect(sid):
    # skip if we already handled via manual_disconnect
    if sid in FORCE_DISCONNECT:
        FORCE_DISCONNECT.remove(sid)
        return

    async def delayed_disconnect():
        await asyncio.sleep(0.5)
        await handle_disconnect(sid, reason="disconnected")

    DISCONNECT_TIMERS[sid] = asyncio.create_task(delayed_disconnect())


# ---------------- Background Cleanup ----------------
import aiohttp


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
            await asyncio.sleep(3600)  # run every hour

    async def ping_self():
        url = "https://realtime-chat-1mv3.onrender.com"  # <-- replace with your Render URL
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        print(f"[KeepAlive] Pinged {url} - {resp.status}")
            except Exception as e:
                print(f"[KeepAlive] Error: {e}")
            await asyncio.sleep(300)  # ping every 5 minutes

    # schedule both tasks in background
    asyncio.create_task(loop_cleanup())
    asyncio.create_task(ping_self())


# ---------------- Static Files ----------------
app.mount("/icons", StaticFiles(directory="icons"), name="icons")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE_DIR, "manifest.json"))


@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "sw.js"))


@app.get("/ads.txt")
def ads():
    return FileResponse("ads.txt", media_type="text/plain")


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
