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

self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  console.log("📩 Push event received:", data);

  event.waitUntil((async () => {
    const allClients = await clients.matchAll({ includeUncontrolled: true });
    const isClientFocused = allClients.some(client => client.focused);

    const title = data.title || "Realtime Chat";

    // Build multi-line body
    const roomLine = data.room ? `Room: ${data.room}` : "";
    const msgLine = data.sender && data.text ? `${data.sender}: ${data.text}` : "New message";
    const body = `${roomLine}\n${msgLine}`;

    if (!isClientFocused) {
      const options = {
        body: body,
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        data: {
          url: data.url || `/chat/${data.room || ""}`,
          room: data.room || null
        }
      };
      await self.registration.showNotification(title, options);
    } else {
      allClients.forEach(client => {
        client.postMessage({
          type: "PUSH_MESSAGE",
          room: data.room || null,
          body: body,
          url: data.url || `/chat/${data.room || ""}`
        });
      });
    }
  })());
});

// ✅ Notification click handler
self.addEventListener("notificationclick", event => {
  event.waitUntil(
    (async () => {
      // Close notification first (prevents lingering)
      event.notification.close();

      const allClients = await clients.matchAll({
        type: "window",
        includeUncontrolled: true
      });

      if (allClients.length > 0) {
        const client = allClients[0];
        await client.focus();
        if (event.notification.data?.url) {
          await client.navigate(event.notification.data.url);
        }
      } else {
        await clients.openWindow(event.notification.data?.url || "/");
      }
    })()
  );
});
