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

self.addEventListener("push", function(event) {
  event.waitUntil((async () => {
    const allClients = await clients.matchAll({ includeUncontrolled: true });
    let isClientFocused = allClients.some(client => client.focused);

    if (!isClientFocused) {
      // Show notification only if no client is focused
      self.registration.showNotification("Title", {
        body: "Message text",
        icon: "/icon.png"
      });
    } else {
      // Optionally send message to client instead of showing notification
      allClients.forEach(client => {
        client.postMessage({
          type: "PUSH_MESSAGE",
          title: "Title",
          body: "Message text"
        });
      });
    }
  })());
});
