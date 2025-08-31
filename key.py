import base64
from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization

# Generate VAPID keys
vapid = Vapid01()
vapid.generate_keys()

# ✅ Export private key as raw bytes → base64url (for pywebpush)
private_der = vapid.private_key.private_bytes(
    encoding=serialization.Encoding.DER,              # DER raw bytes
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
private_b64url = base64.urlsafe_b64encode(private_der).decode("utf-8").rstrip("=")

# Export public key PEM (optional)
public_pem = vapid.public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
).decode()

# Export public key for frontend (base64url)
public_der = vapid.public_key.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint
)
public_b64url = base64.urlsafe_b64encode(public_der).decode('utf-8').rstrip("=")

# Print results
print("✅ Private key (base64url, use in .env VAPID_PRIVATE_KEY):\n", private_b64url)
print("✅ Public PEM (optional, for server use):\n", public_pem)
print("✅ Public Base64URL (use in frontend VAPID_PUBLIC_KEY):\n", public_b64url)
