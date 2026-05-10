"""
Broker session token persistence.
Prevents repeated authentication calls that cause rate-limiting.

Uses Fernet encryption if cryptography is installed.
Falls back to base64 encoding otherwise.
"""
import os
import json
import base64
import logging
from datetime import datetime
from config.settings import SETTINGS

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.warning("cryptography not installed — session tokens stored in base64")


class SessionStore:
    def __init__(self, filepath: str = None, key: str = None):
        self.filepath = filepath or SETTINGS.SESSION_FILE
        # Derive encryption key from API key if not provided
        if key is None:
            raw = (SETTINGS.ANGEL_API_KEY or "default_key").encode()
            self._key = base64.urlsafe_b64encode(raw.ljust(32)[:32])
        else:
            self._key = key.encode() if isinstance(key, str) else key

        if HAS_CRYPTO:
            self._fernet = Fernet(self._key)
        else:
            self._fernet = None

    def save(self, session_data: dict):
        """Save session data encrypted to disk. Atomic write via tmp file."""
        payload = json.dumps(session_data).encode()

        if self._fernet:
            encrypted = self._fernet.encrypt(payload)
        else:
            encrypted = base64.b64encode(payload)

        tmp_path = self.filepath + ".tmp"
        with open(tmp_path, 'wb') as f:
            f.write(encrypted)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, self.filepath)
        os.chmod(self.filepath, 0o600)
        logger.debug("Session saved")

    def load(self) -> dict:
        """Load and decrypt session data. Returns None if missing/expired."""
        if not os.path.exists(self.filepath):
            return None

        try:
            with open(self.filepath, 'rb') as f:
                encrypted = f.read()

            if self._fernet:
                decrypted = self._fernet.decrypt(encrypted)
            else:
                decrypted = base64.b64decode(encrypted)

            data = json.loads(decrypted)

            # Check if session is expired
            saved_at = data.get("_saved_at", "")
            if saved_at:
                from datetime import datetime
                saved = datetime.fromisoformat(saved_at)
                age = (datetime.now() - saved).total_seconds()
                if age > 86400:  # 24 hours
                    logger.info("Session expired (>24h old)")
                    return None

            return data

        except Exception as e:
            logger.warning(f"Session load failed: {e}")
            return None

    def clear(self):
        """Remove saved session file."""
        if os.path.exists(self.filepath):
            os.unlink(self.filepath)
