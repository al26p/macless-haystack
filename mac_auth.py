import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

import mh_config

OSFINDMYE = "/opt/osfindmye"
MAC_ANISETTE = os.path.join(OSFINDMYE, "tools", "machoadi", "mac_anisette.py")
RESYNC = os.path.join(OSFINDMYE, "tools", "machoadi", "resync.py")
AUTH_PROBE = os.path.join(OSFINDMYE, "auth_probe.py")

FETCH_URL = "https://gateway.icloud.com/acsnservice/fetch"

_headers_cache = {"window": None, "value": None}

_login_lock = threading.Lock()
_login = {"state": "waiting_manual_login", "detail": "", "proc": None}


class ReloginRequired(Exception):
    """Apple no longer accepts this session and only a new login can renew it."""


def relogin_message():
    logging.error(
        "Apple requires a new login for this account. No automatic login is attempted (that would loop).\n"
        "Start one manually:\n"
        "  curl -X POST http://<endpoint>:6176/login\n"
        "then submit the 2FA code:\n"
        "  curl -X POST http://<endpoint>:6176/2fa -H 'Content-Type: application/json' -d '{\"code\":\"123456\"}'")


def _data_dir():
    d = mh_config.getConfigPath()
    os.makedirs(d, exist_ok=True)
    return d


def state_dir():
    return os.path.join(_data_dir(), "macadi")


def session_file():
    return os.path.join(_data_dir(), "session.json")


def _promote_session():
    """Copy auth_probe's output (session.json) to the file the endpoint reads (auth.json)."""
    if not os.path.exists(session_file()):
        return False
    with open(session_file()) as stream:
        saved = json.load(stream)
    with open(mh_config.getConfigFile(), "w") as stream:
        json.dump({"dsid": saved["dsid"], "searchPartyToken": saved["searchPartyToken"]}, stream)
    return True


def recover_session():
    """Adopt an existing session.json as the active session (e.g. after a restart)."""
    return _promote_session()


def has_session():
    return os.path.exists(mh_config.getConfigFile())


def status():
    state = _login.get("state")
    if has_session():
        state = "authenticated"
    return {"state": state, "detail": _login.get("detail", "")}


def is_provisioned():
    store = os.path.join(state_dir(), "store")
    if not os.path.exists(os.path.join(state_dir(), "identity.json")):
        return False
    if not os.path.isdir(store):
        return False
    return any(name.startswith("adi") for name in os.listdir(store))


def ensure_machine():
    if not is_provisioned():
        os.makedirs(state_dir(), exist_ok=True)
        result = subprocess.run([sys.executable, MAC_ANISETTE, "provision", "--state", state_dir()])
        if result.returncode != 0:
            raise RuntimeError("os-findmy-e: machine provisioning failed")
    return state_dir()


def headers():
    window = int(time.time()) // 30
    if _headers_cache["value"] is None or _headers_cache["window"] != window:
        result = subprocess.run(
            [sys.executable, MAC_ANISETTE, "headers", "--state", state_dir()],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("os-findmy-e: headers failed: " + result.stderr.strip())
        _headers_cache["value"] = json.loads(result.stdout)
        _headers_cache["window"] = window
    return _headers_cache["value"]


def _reset_headers_cache():
    _headers_cache["value"] = None
    _headers_cache["window"] = None


def try_resync():
    """Answer Apple's anisette resync non-interactively. Returns the machine outcome."""
    _reset_headers_cache()
    try:
        result = subprocess.run(
            [sys.executable, RESYNC, "--state", state_dir(),
             "--rounds", "1", "--delay", "0", "--max-requests", "6"],
            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        logging.warning("os-findmy-e: resync timed out")
        return "error"
    outcome = "error"
    for line in reversed(result.stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and '"summary"' in stripped:
            try:
                summary = json.loads(stripped).get("summary") or []
                if summary:
                    outcome = summary[0]
            except ValueError:
                pass
            break
    if result.returncode == 3:
        return "cooldown"
    logging.warning("os-findmy-e: resync outcome=%s rc=%s", outcome, result.returncode)
    _reset_headers_cache()
    return outcome


def mark_relogin_required():
    """Drop the dead session and set the machine aside so a fresh one is provisioned on re-login."""
    for path in (session_file(), mh_config.getConfigFile()):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    state = state_dir()
    if os.path.isdir(state):
        try:
            shutil.move(state, state + ".stale-" + str(int(time.time())))
        except OSError as error:
            logging.warning("os-findmy-e: could not set the machine aside: %s", error)
    with _login_lock:
        _login["state"] = "waiting_manual_login"
        _login["detail"] = "session expired; POST /login to re-authenticate"
    relogin_message()


def fetch(data, second_factor='sms'):
    """POST the acsnservice fetch, answering a lapsed machine once, else raising ReloginRequired."""
    import requests

    if not has_session():
        raise ReloginRequired("no session; POST /login to authenticate")
    response = requests.post(FETCH_URL, auth=_auth(), headers=headers(), json=data, timeout=60)
    if response.status_code in (401, 403):
        outcome = try_resync()
        if outcome == "revived":
            logging.info("os-findmy-e: machine resynced, retrying fetch")
            response = requests.post(FETCH_URL, auth=_auth(), headers=headers(), json=data, timeout=60)
    if response.status_code in (401, 403):
        mark_relogin_required()
        raise ReloginRequired("fetch returned HTTP %d" % response.status_code)
    return response


def _auth():
    with open(mh_config.getConfigFile()) as stream:
        saved = json.load(stream)
    return saved["dsid"], saved["searchPartyToken"]


def start_login():
    """Kick off a login in the background. Returns the current status dict (ok=False on conflict)."""
    if has_session():
        return {"ok": True, "state": "authenticated", "detail": "already authenticated"}
    email = os.environ.get("APPLE_ID")
    password = os.environ.get("APPLE_PASSWORD")
    with _login_lock:
        if _login["state"] in ("running", "awaiting_2fa", "submitting"):
            return {"ok": False, "state": _login["state"], "detail": "a login is already in progress"}
        if not email or not password:
            _login["state"] = "failed"
            _login["detail"] = "APPLE_ID / APPLE_PASSWORD are not set"
            return {"ok": False, "state": "failed", "detail": _login["detail"]}
        _login["state"] = "running"
        _login["detail"] = "requesting Apple login and SMS"
        _login["proc"] = None
    threading.Thread(target=_login_worker, args=(email, password), daemon=True).start()
    return {"ok": True, "state": "running", "detail": _login["detail"]}


def _login_worker(email, password):
    try:
        ensure_machine()
        env = os.environ.copy()
        env["APPLE_ID"] = email
        env["APPLE_PASSWORD"] = password
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-u", AUTH_PROBE,
             "--macadi", state_dir(),
             "--state-dir", state_dir() + "-auth",
             "--sms", "--auto-resync",
             "--save-session", session_file()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env)
        _login["proc"] = proc

        buffer = ""
        while True:
            char = proc.stdout.read(1)
            if char == "":
                break
            buffer += char
            if "SMS code:" in buffer or "2FA code:" in buffer:
                with _login_lock:
                    if _login["state"] == "running":
                        _login["state"] = "awaiting_2fa"
                        _login["detail"] = "SMS sent; POST the code to /2fa"
                buffer = ""
            elif "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    logging.info("os-findmy-e login: %s", line.strip())
        proc.wait()
        if _promote_session():
            with _login_lock:
                _login["state"] = "authenticated"
                _login["detail"] = "login complete"
            logging.info("os-findmy-e login: authenticated")
        else:
            with _login_lock:
                _login["state"] = "failed"
                _login["detail"] = "login process exited rc=%s without a session" % proc.returncode
            logging.error("os-findmy-e login failed (rc=%s)", proc.returncode)
    except Exception as error:  # noqa: BLE001
        logging.exception("os-findmy-e login crashed")
        with _login_lock:
            _login["state"] = "failed"
            _login["detail"] = str(error)


def submit_2fa(code):
    """Feed the 2FA code to the running login. Returns a status dict (ok=False when not applicable)."""
    with _login_lock:
        proc = _login["proc"]
        if _login["state"] != "awaiting_2fa" or proc is None or proc.stdin is None:
            return {"ok": False, "state": _login["state"],
                    "detail": "no login is waiting for a code"}
        _login["state"] = "submitting"
        _login["detail"] = "submitting the code"
    proc.stdin.write(code.strip() + "\n")
    proc.stdin.flush()
    return {"ok": True, "state": "submitting", "detail": "code submitted"}


def login():
    """Synchronous, interactive login (local use). Kubernetes uses start_login()/submit_2fa()."""
    email = os.environ.get("APPLE_ID") or input("Apple ID: ").strip()
    password = os.environ.get("APPLE_PASSWORD") or getpass.getpass("Password: ")
    ensure_machine()
    env = os.environ.copy()
    env["APPLE_ID"] = email
    env["APPLE_PASSWORD"] = password
    result = subprocess.run([
        sys.executable, AUTH_PROBE,
        "--macadi", state_dir(),
        "--state-dir", state_dir() + "-auth",
        "--sms",
        "--auto-resync",
        "--save-session", session_file(),
    ], env=env)
    if result.returncode != 0 or not os.path.exists(session_file()):
        raise RuntimeError("os-findmy-e: Apple login failed")
    _promote_session()
    with open(session_file()) as stream:
        saved = json.load(stream)
    return saved["dsid"], saved["searchPartyToken"]
