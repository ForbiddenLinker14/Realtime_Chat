import { Capacitor } from '@capacitor/core';
import { PushNotifications } from '@capacitor/push-notifications';

// Register service worker only for PWA
if ('serviceWorker' in navigator && !Capacitor.isNativePlatform()) {
  navigator.serviceWorker.register('/sw.js');
}

export async function initPush() {
  if (!Capacitor.isNativePlatform()) {
    console.log("📱 Running in browser (PWA) – push handled by sw.js");
    return;
  }

  console.log("📱 Running in native app – using FCM");

  // --- Listeners ---
  PushNotifications.addListener('registration', async (token) => {
    console.log('✅ FCM token:', token.value);

    // Send token to backend
    await fetch('/api/register-fcm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: token.value,
        user: localStorage.getItem('username')
      })
    });
  });

  PushNotifications.addListener('registrationError', (error) => {
    console.error('❌ Registration error:', error);
  });

  PushNotifications.addListener('pushNotificationReceived', (notification) => {
    console.log('📩 Push received in foreground:', notification);
    alert(`${notification.title}: ${notification.body}`);
  });

  PushNotifications.addListener('pushNotificationActionPerformed', (action) => {
    console.log('👉 Notification tapped:', action.notification.data);
    if (action.notification?.data?.url) {
      window.location.href = action.notification.data.url;
    }
  });

  // --- Permission request (Android 13+) ---
  let perm = await PushNotifications.checkPermissions();
  if (perm.receive === 'prompt') {
    perm = await PushNotifications.requestPermissions();
  }
  if (perm.receive !== 'granted') {
    console.warn('⚠️ Notifications permission not granted');
    return;
  }

  // --- Register with FCM ---
  await PushNotifications.register();

  // --- Create a channel (Android 8+) ---
  await PushNotifications.createChannel({
    id: 'chat',
    name: 'Room Chat',
    description: 'Notifications for chat messages',
    importance: 4
  });
}
