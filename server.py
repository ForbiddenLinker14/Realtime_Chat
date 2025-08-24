import sqlite3
from datetime import datetime
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
import socketio
import socket
import os
import asyncio
from datetime import datetime, timezone
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

DB_PATH = "chat.db"
DESTROYED_ROOMS = set()
ROOM_USERS = {}  # { room: {sid: username} }


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
    max_http_buffer_size=5 * 1024 * 1024,
)
app = FastAPI()

sio_app = socketio.ASGIApp(sio, socketio_path="socket.io")
app.mount("/socket.io", sio_app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/sitemap.xml")
def sitemap():
    urls = [
        
        "https://realtime-chat-1mv3.onrender.com/",
        "https://realtime-chat-1mv3.onrender.com/about.html",
        "https://realtime-chat-1mv3.onrender.com/privacy-policy.html",
        "https://realtime-chat-1mv3.onrender.com/terms-of-service.html",
        "https://realtime-chat-1mv3.onrender.com/disclaimer.html",
        "https://realtime-chat-1mv3.onrender.com/contact.html",
        "https://realtime-chat-1mv3.onrender.com/blog.html",
    ]

    xml_content = '<?xml version="1.0" encoding="UTF-8"?>'
    xml_content += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'

    for url in urls:
        xml_content += f"<url><loc>{url}</loc></url>"

    xml_content += "</urlset>"

    return Response(content=xml_content, media_type="application/xml")


@app.get("/robots.txt")
def robots():
    content = """User-agent: *
Allow: /

Sitemap: https://realtime-chat-1mv3.onrender.com/sitemap.xml
"""
    return Response(content=content, media_type="text/plain")


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

    # remove users in this room
    if room in ROOM_USERS:
        del ROOM_USERS[room]

    await sio.emit(
        "clear",
        {"room": room, "message": "Room destroyed. All messages cleared."},
        room=room,
    )
    await sio.emit("room_destroyed", {"room": room}, room=room)

    # kick everyone out
    namespace = "/"
    if namespace in sio.manager.rooms and room in sio.manager.rooms[namespace]:
        sids = list(sio.manager.rooms[namespace][room])
        for sid in sids:
            await sio.leave_room(sid, room, namespace=namespace)

    return JSONResponse({"status": "ok", "message": f"Room {room} destroyed."})


# ---------------- Helper ----------------
async def broadcast_users(room):
    """Send users only for the specific room"""
    users = []
    for sid, username in ROOM_USERS.get(room, {}).items():
        users.append({"name": username, "status": "online"})
    await sio.emit("users_update", {"room": room, "users": users}, room=room)


# ---------------- Socket.IO Events ----------------
@sio.event
async def join(sid, data):
    room = data["room"]
    sender = data["sender"]
    last_ts = data.get("lastTs")
    if room in DESTROYED_ROOMS:
        DESTROYED_ROOMS.remove(room)
    await sio.enter_room(sid, room)

    if room not in ROOM_USERS:
        ROOM_USERS[room] = {}
    ROOM_USERS[room][sid] = sender

    await broadcast_users(room)

    deleted = cleanup_old_messages()
    if deleted > 0:
        await sio.emit(
            "cleanup",
            {"message": f"{deleted} old messages (48h+) were removed."},
            room=room,
        )

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

    # delete room if empty
    if room in ROOM_USERS and not ROOM_USERS[room]:
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
async def disconnect(sid):
    for room, users in list(ROOM_USERS.items()):
        if sid in users:
            username = users[sid]
            del users[sid]
            if not users:
                del ROOM_USERS[room]
            await broadcast_users(room)
    print(f"Client {sid} disconnected")


# ---------------- Background Cleanup ----------------
@app.on_event("startup")
async def schedule_cleanup():
    async def loop_cleanup():
        while True:
            deleted = cleanup_old_messages()
            if deleted > 0:
                await sio.emit(
                    "cleanup",
                    {"message": f"{deleted} old messages (48h+) were removed."},
                )
            await asyncio.sleep(3600)

    asyncio.create_task(loop_cleanup())


# serve icons and manifest.json
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


# Serve everything in BASE_DIR (so /index.html, /style.css, etc. work)
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

# ---------------- Run Server ----------------
if __name__ == "__main__":
    import uvicorn

    init_db()
    migrate_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA table_info(messages)")
    print("📂 Messages table columns:", [row[1] for row in c.fetchall()])
    conn.close()
    local_ip = socket.gethostbyname(socket.gethostname())
    print("🚀 Server running at:")
    print("   ➤ Local:   http://127.0.0.1:8000")
    print(f"   ➤ Network: http://{local_ip}:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
