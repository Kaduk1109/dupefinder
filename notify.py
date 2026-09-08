"""
Scenario 4: notify the user when a background scan finishes.

Current default (no SMTP configured): desktop notification (best-effort,
via `notify-send` if present) + a line in the scanner log, which is already
being written by scanner.py's logging setup.

Once DUPEFINDER_SMTP_HOST etc. are set (see config.py), email is sent too --
this function is the single place that needs no further changes when you're
ready to wire that up.
"""
import logging
import shutil
import smtplib
import subprocess
from email.mime.text import MIMEText

import config

log = logging.getLogger("scanner")


def _desktop_notify(title, message):
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", title, message], check=False, timeout=5)
        except Exception:
            pass  # best-effort only; NAS environments often have no desktop session


def _send_email(subject, body):
    if not config.SMTP_HOST or not config.NOTIFY_EMAIL_TO:
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = config.NOTIFY_EMAIL_TO
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
            server.starttls()
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.error("Failed to send completion email: %s", e)
        return False


def scan_complete(root_path, total_files, n_groups):
    title = "Duplicate scan complete"
    message = f"{root_path}: {total_files} files scanned, {n_groups} duplicate groups found. Ready to review."
    log.info("NOTIFY: %s", message)
    _desktop_notify(title, message)
    if _send_email(title, message):
        log.info("Completion email sent to %s", config.NOTIFY_EMAIL_TO)


def scan_failed(root_path, error_message):
    title = "Duplicate scan failed"
    message = f"{root_path}: scan stopped with an error: {error_message}"
    log.error("NOTIFY: %s", message)
    _desktop_notify(title, message)
    _send_email(title, message)


def scan_stopped(root_path, processed, total):
    title = "Duplicate scan stopped"
    message = f"{root_path}: stopped by request at {processed}/{total} files. Resume any time to continue."
    log.info("NOTIFY: %s", message)
    _desktop_notify(title, message)
    _send_email(title, message)
