"""Notification channels: desktop (Windows/macOS/Linux) and email (SMTP)."""
from __future__ import annotations

import shutil
import smtplib
import subprocess
import sys
from email.mime.text import MIMEText
from typing import Any


def notify_desktop(title: str, body: str) -> bool:
    text = body[:400]
    try:
        if sys.platform.startswith("win"):
            return _notify_windows(title, text)
        if sys.platform == "darwin":
            script = f'display notification {text!r} with title {title!r}'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
            return True
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, text], check=False, timeout=10)
            return True
    except (subprocess.SubprocessError, OSError):
        return False
    return False


def _notify_windows(title: str, text: str) -> bool:
    safe_title = title.replace("'", "''")
    safe_text = text.replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n = $t.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($t.CreateTextNode('{safe_title}')) | Out-Null;"
        f"$n.Item(1).AppendChild($t.CreateTextNode('{safe_text}')) | Out-Null;"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'Hedge Monitor').Show($toast);"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], check=False, timeout=15
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def notify_email(cfg: dict[str, Any], password: str, subject: str, body: str) -> bool:
    email = cfg.get("email", {})
    if not email.get("enabled"):
        return False
    host = email.get("smtp_host")
    port = int(email.get("smtp_port", 587))
    username = email.get("username", "")
    from_addr = email.get("from_addr", username)
    to_addrs = email.get("to_addrs", []) or []
    if not (host and username and password and to_addrs):
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(from_addr, to_addrs, msg.as_string())
    return True


class Notifier:
    def __init__(self, notify_cfg: dict[str, Any], email_password: str) -> None:
        self.cfg = notify_cfg
        self.password = email_password

    def send(self, subject: str, body: str) -> None:
        print(f"\n=== ALERT: {subject} ===\n{body}\n")
        if self.cfg.get("desktop"):
            notify_desktop(subject, body)
        try:
            notify_email(self.cfg, self.password, subject, body)
        except (smtplib.SMTPException, OSError) as exc:
            print(f"[email failed] {exc}")
