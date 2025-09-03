// push.js
import { Capacitor } from "@capacitor/core";
import { PushNotifications } from "@capacitor/push-notifications";

// ==================================================
// Service Worker (only for Web/PWA)
// ==================================================
if ("serviceWorker" in navigator && !Capacitor.isNativePlatform()) {
  navigator.serviceWorker
    .register("/sw.js")
    .then(() => console.log("⚡ Service Worker registered"))
    .catch((err) =>
      console.error("❌ Service Worker registration failed:", err)
    );
}

// ==================================================
// Init Push Notifications
// ==================================================
export async function initPush() {
  try {
    // ------------------------------------------------
    // PWA (Web browser)
    // ------------------------------------------------
    if (!Capacitor.isNativePlatform()) {
      console.log("🌐 Running in browser (PWA) – push handled by sw.js");
      return;
    }

    console.log("📱 Running in native app – using FCM");

    // --- Step 1: Check notification permission ---
    let perm = await PushNotifications.checkPermissions();
    console.log("🔎 Current push permission:", perm);
    alert("Push permission: " + JSON.stringify(perm));

    if (perm.receive === "prompt") {
      perm = await PushNotifications.requestPermissions();
      console.log("📌 User decision:", perm);
      alert("User decision: " + JSON.stringify(perm));
    }

    if (perm.receive !== "granted") {
      console.warn("⚠️ Notifications permission not granted");
      return;
    }

    // --- Step 2: Register with FCM ---
    await PushNotifications.register();
    console.log("📡 Registered with FCM");

    // --- Step 3: Create Android notification channel ---
    await PushNotifications.createChannel({
      id: "chat",
      name: "Room Chat",
      description: "Notifications for chat messages",
      importance: 4,
    });
    console.log("📡 Notification channel created");

    // --- Event: Successful registration ---
    PushNotifications.addListener("registration", async (token) => {
      console.log("✅ FCM token:", token.value);

      try {
        // Send token to backend (absolute URL required!)
        await fetch("https://realtime-chat-1mv3.onrender.com/api/register-fcm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            token: token.value,
            user: localStorage.getItem("username") || "guest",
          }),
        });
        console.log("📡 Token sent to backend");
      } catch (err) {
        console.error("❌ Failed to send token to backend:", err);
      }
    });

    // --- Event: Registration error ---
    PushNotifications.addListener("registrationError", (error) => {
      console.error("❌ Registration error:", error);
    });

    // --- Event: Push received in foreground ---
    PushNotifications.addListener("pushNotificationReceived", (notification) => {
      console.log("📩 Push received in foreground:", notification);
      alert(`${notification.title || "Notification"}: ${notification.body || ""}`);
    });

    // --- Event: Notification tapped by user ---
    PushNotifications.addListener("pushNotificationActionPerformed", (action) => {
      console.log("👉 Notification tapped:", action.notification.data);
      if (action.notification?.data?.url) {
        window.location.href = action.notification.data.url;
      }
    });
  } catch (err) {
    console.error("❌ initPush failed:", err);
  }
}
