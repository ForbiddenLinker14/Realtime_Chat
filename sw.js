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

  const title = "Realtime Chat";

  const options = {
    body: `${data.title ? data.title + ": " : ""}${data.body || "No body"}`,
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    data: {
      url: data.url || "/",
      messageId: data.messageId || null,
      chatId: data.chatId || null
    },
    actions: [
      {
        action: "reply",
        title: "Reply",
        type: "text",          // ✅ Android Chrome only: inline text input
        placeholder: "Type a reply…"
      },
      {
        action: "mark-as-read",
        title: "Mark as Read"
      },
      {
        action: "mute",
        title: "Mute"
      }
    ]
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

// Handle action button clicks
self.addEventListener("notificationclick", event => {
  event.notification.close();

  // Handle action buttons
  if (event.action === "reply") {
    const replyText = event.reply; // ✅ Chrome on Android only
    console.log("User reply:", replyText);

    // Send reply to your server (fetch/WebSocket)
    if (replyText) {
      fetch("/api/reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chatId: event.notification.data.chatId,
          messageId: event.notification.data.messageId,
          reply: replyText
        })
      });
    }
    return;
  }

  if (event.action === "mark-as-read") {
    console.log("Message marked as read");
    fetch("/api/mark-read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chatId: event.notification.data.chatId,
        messageId: event.notification.data.messageId
      })
    });
    return;
  }

  if (event.action === "mute") {
    console.log("Chat muted");
    fetch("/api/mute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chatId: event.notification.data.chatId
      })
    });
    return;
  }

  // Default click (if no action button was clicked)
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






