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

// ✅ Handle push events with room info + active check
self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  console.log("📩 Push event received:", data);

  event.waitUntil((async () => {
    const allClients = await clients.matchAll({ includeUncontrolled: true });
    let isClientFocused = allClients.some(client => client.focused);

    if (!isClientFocused) {
      // Show system notification if no client focused
      const title = data.room ? `Room: ${data.room}` : "Realtime Chat";
      const options = {
        body: data.body || "New message",
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        data: {
          url: data.url || `/chat/${data.room || ""}`,
          room: data.room || null
        }
      };

      self.registration.showNotification(title, options);
    } else {
      // Send message to client (handle inside app UI, e.g. toast)
      allClients.forEach(client => {
        client.postMessage({
          type: "PUSH_MESSAGE",
          room: data.room || null,
          body: data.body || "New message",
          url: data.url || `/chat/${data.room || ""}`
        });
      });
    }
  })());
});

// ✅ Handle click on notification
self.addEventListener("notificationclick", event => {
  event.notification.close();

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(clientsArr => {
      if (clientsArr.length > 0) {
        const client = clientsArr[0];
        client.focus();
        if (event.notification.data?.url) {
          client.navigate(event.notification.data.url);
        }
      } else {
        clients.openWindow(event.notification.data?.url || "/");
      }
    })
  );
});

