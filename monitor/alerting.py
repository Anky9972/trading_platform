"""
OS-level alerting.
Sends notifications through all available channels.
If one channel fails, others still fire.

Channels:
  1. Desktop notification (OS-native)
  2. Terminal bell
  3. Append to alerts.log

No external services. No API keys. Works offline.
"""
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

ALERTS_LOG = "alerts.log"


def alert(message: str, level: str = "WARNING"):
    """
    Send alert through all available channels.
    Each channel is independent — one failure doesn't block others.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] [{level}] {message}"

    # Channel 1: Log file (most reliable)
    try:
        with open(ALERTS_LOG, 'a') as f:
            f.write(full_message + "\n")
            f.flush()
    except Exception as e:
        logger.error(f"Alert log write failed: {e}")

    # Channel 2: Desktop notification
    try:
        _desktop_notification(message, level)
    except Exception:
        pass  # Best effort

    # Channel 3: Terminal bell
    try:
        print(f"\a{full_message}")  # \a = ASCII bell
    except Exception:
        pass

    # Channel 4: Python logger
    log_level = getattr(logging, level.upper(), logging.WARNING)
    logger.log(log_level, f"ALERT: {message}")


def _desktop_notification(message: str, level: str):
    """Try OS-native desktop notification."""
    import platform
    system = platform.system()

    if system == "Linux":
        os.system(f'notify-send "Trading Alert [{level}]" "{message}" 2>/dev/null')

    elif system == "Darwin":  # macOS
        escaped = message.replace('"', '\\"')
        os.system(f'osascript -e \'display notification "{escaped}" '
                  f'with title "Trading Alert [{level}]"\' 2>/dev/null')

    elif system == "Windows":
        # Use PowerShell for Windows toast notification
        try:
            import subprocess
            ps_cmd = (f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms"); '
                     f'$n = New-Object System.Windows.Forms.NotifyIcon; '
                     f'$n.Icon = [System.Drawing.SystemIcons]::Warning; '
                     f'$n.Visible = $true; '
                     f'$n.ShowBalloonTip(5000, "Trading Alert [{level}]", '
                     f'"{message}", [System.Windows.Forms.ToolTipIcon]::Warning)')
            subprocess.Popen(['powershell', '-Command', ps_cmd],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
