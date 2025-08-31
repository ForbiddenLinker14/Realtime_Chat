// sw.js - Service Worker for Room Chat

const CACHE_NAME = "chat-cache-v2"; // bumped version

self.addEventListener("install", (event) => {
  console.log("⚡ Service Worker: Installed");

  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll([
        "/",                 // homepage
        "/index.html",       // main page
        "/manifest.json",
        "/icons/icon-192.png",
        "/icons/icon-512.png"
      ]);
    })
  );

  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  console.log("⚡ Service Worker: Activated");

  // cleanup old caches
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      );
    })
  );
});

// ✅ Only intercept GET requests; let DELETE/POST/etc. go to the network
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return; // don’t cache non-GET requests
  }

  event.respondWith(
    caches.match(event.request).then((response) => {
      return (
        response ||
        fetch(event.request).catch(() => new Response("⚠️ Offline mode"))
      );
    })
  );
});

// ==================================================
// tiny IndexedDB helpers (SW-safe, no external libs)
// ==================================================
const DB_NAME = 'chat-settings';
const DB_STORE = 'kv';

function idbOpen() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(DB_STORE)) db.createObjectStore(DB_STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
function idbGet(key) {
  return idbOpen().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readonly');
    const store = tx.objectStore(DB_STORE);
    const req = store.get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}
function idbSet(key, value) {
  return idbOpen().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    const store = tx.objectStore(DB_STORE);
    const req = store.put(value, key);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  }));
}

// read helpers
async function getMuteSet() {
  const arr = (await idbGet('muteRooms')) || [];
  return new Set(arr);
}
async function setMuteSet(set) {
  await idbSet('muteRooms', Array.from(set));
}
async function getLastReadMap() {
  // { [room]: ISOString }
  return (await idbGet('lastRead')) || {};
}
async function setLastReadMap(map) {
  await idbSet('lastRead', map);
}

// ==================================================
// PUSH: show actions (Reply / Mark read / Mute)
// ==================================================
self.addEventListener('push', event => {
  const data = event.data ? event.data.json() : {};

  event.waitUntil((async () => {
    const allClients = await clients.matchAll({ includeUncontrolled: true });
    const isClientFocused = allClients.some(c => c.focused);

    const room = data.room || null;
    const title = data.title || "Realtime Chat";
    const msgLine = data.sender && data.text ? `${data.sender}: ${data.text}` : "New message";
    const roomLine = room ? `Room: ${room}` : "";
    const body = `${roomLine}\n${msgLine}`;

    // ---- respect mute + last-read ----
    const mute = await getMuteSet();
    if (room && mute.has(room)) {
      // 🔕 muted → don’t show notification, but forward if focused
      if (isClientFocused) {
        allClients.forEach(client => {
          client.postMessage({ type: "PUSH_MESSAGE", room, body, url: data.url || `/chat/${room}` });
        });
      }
      return;
    }

    const lastRead = await getLastReadMap();
    const ts = data.timestamp || new Date().toISOString();
    if (room && lastRead[room] && ts <= lastRead[room]) {
      // already read → skip showing
      if (isClientFocused) {
        allClients.forEach(client => {
          client.postMessage({ type: "PUSH_MESSAGE", room, body, url: data.url || `/chat/${room}` });
        });
      }
      return;
    }

    // ---- show notif if not focused ----
    if (!isClientFocused) {
      const isMuted = room ? mute.has(room) : false;
      const actions = [
        { action: "reply", title: "Reply" },
        { action: "mark-read", title: "Mark as read" },
        { action: isMuted ? "unmute" : "mute", title: isMuted ? "Unmute" : "Mute" },
      ];

      const options = {
        body,
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        tag: room ? `chat-${room}` : undefined,   // collapse per-room
        actions,
        data: {
          url: data.url || `/chat/${room || ""}`,
          room,
          pushId: data.pushId || null,
          timestamp: ts
        }
      };
      await self.registration.showNotification(title, options);
    } else {
      // forward silently to open clients
      allClients.forEach(client => {
        client.postMessage({
          type: "PUSH_MESSAGE",
          room,
          body,
          url: data.url || `/chat/${room || ""}`,
          pushId: data.pushId || null,
          timestamp: ts
        });
      });
    }
  })());
});

// ==================================================
// NOTIFICATION CLICKS (with actions)
// ==================================================
self.addEventListener("notificationclick", event => {
  const { action } = event;
  const payload = event.notification.data || {};
  const room = payload.room;
  const targetUrl = payload.url || "/";
  event.notification.close();

  event.waitUntil((async () => {
    async function focusOrOpen(url) {
      const clientList = await clients.matchAll({ type: "window", includeUncontrolled: true });
      if (clientList.length > 0) {
        const client = clientList[0];
        await client.focus();
        try { await client.navigate(url); } catch {}
        return client;
      }
      return clients.openWindow(url);
    }

    if (action === "reply") {
      const replyText = event.reply; // ✅ Only works on Android
      const client = await focusOrOpen(targetUrl);

      if (client) {
        if (replyText) {
          // ✅ Send inline reply text (Android)
          client.postMessage({
            type: "NOTIF_REPLY",
            room,
            reply: replyText,
            pushId: payload.pushId,
            timestamp: new Date().toISOString()
          });
        } else {
          // ✅ Desktop fallback → prefill chat box
          client.postMessage({
            type: "NOTIF_REPLY",
            room,
            hint: payload?.timestamp
              ? `Replying to message at ${payload.timestamp}`
              : "Replying…"
          });
        }
      }
      return;
    }

    if (action === "mark-read") {
      const lastRead = await getLastReadMap();
      const ts = payload.timestamp || new Date().toISOString();
      if (room && (!lastRead[room] || ts > lastRead[room])) {
        lastRead[room] = ts;
        await setLastReadMap(lastRead);
      }
      await focusOrOpen(targetUrl);
      return;
    }

    if (action === "mute" || action === "unmute") {
      if (room) {
        const mute = await getMuteSet();
        if (action === "mute") mute.add(room);
        else mute.delete(room);
        await setMuteSet(mute);
      }
      const clientList = await clients.matchAll({ type: "window", includeUncontrolled: true });
      clientList.forEach(c =>
        c.postMessage({ type: "MUTE_CHANGED", room, muted: action === "mute" })
      );
      return;
    }

    // Default click → open chat window
    await focusOrOpen(targetUrl);
  })());
});
