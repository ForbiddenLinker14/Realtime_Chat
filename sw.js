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

// self.addEventListener("push", event => {
//   const data = event.data ? event.data.json() : {};
//   console.log("📩 Push event received:", data);

//   const timestampText = data.timestamp
//     ? `\nSent at: ${new Date(data.timestamp).toLocaleTimeString()}`
//     : "";

//   const options = {
//     body: (data.body || "No body") + timestampText, // append timestamp
//     icon: "/icons/icon-192.png",
//     badge: "/icons/icon-192.png",
//     data: {
//       url: data.url || "/",
//       timestamp: data.timestamp
//     }
//   };

//   event.waitUntil(
//     self.registration.showNotification(data.title || "New Message", options)
//   );
// });
self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  console.log("📩 Push event received:", data);

  // Format relative time
  let relativeTime = "Now";
  if (data.timestamp) {
    const diffMs = Date.now() - new Date(data.timestamp).getTime();
    const diffSec = Math.floor(diffMs / 1000);

    if (diffSec < 60) {
      relativeTime = "Now";
    } else if (diffSec < 3600) {
      relativeTime = `${Math.floor(diffSec / 60)}m`;
    } else if (diffSec < 86400) {
      relativeTime = `${Math.floor(diffSec / 3600)}h`;
    } else {
      relativeTime = `${Math.floor(diffSec / 86400)}d`;
    }
  }

  // Always use custom title
  const title = "Realtime Chat";

  const options = {
    body: `${data.title ? data.title + ": " : ""}${data.body || "No body"}`,
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    data: {
      url: data.url || "/",
      timestamp: data.timestamp
    }
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

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





