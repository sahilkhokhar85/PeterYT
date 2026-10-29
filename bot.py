import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))  # backup polling (seconds)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. http://1.2.3.4:8080
PORT = int(os.getenv("PORT", "8080"))
SECRET = os.getenv("WEBSUB_SECRET", "change-me-secret")
HUB = "https://pubsubhubbub.appspot.com/subscribe"

BASE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(BASE, "channels.txt")
STATE_FILE = os.path.join(BASE, "state.json")

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1; SOCS=CAI"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

LOCK = threading.RLock()


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("seen", {})
    s.setdefault("resolved", {})
    return s


STATE = _load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f)
    os.replace(tmp, STATE_FILE)


def load_channels():
    out = []
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line)
    except FileNotFoundError:
        log.warning("channels.txt nahi mila")
    return out


def resolve_channel_id(entry):
    """entry: UC... id, @handle, ya channel URL -> channel id"""
    if re.fullmatch(r"UC[\w-]{22}", entry):
        return entry
    with LOCK:
        if entry in STATE["resolved"]:
            return STATE["resolved"][entry]
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", entry)
    if m:
        cid = m.group(1)
    else:
        url = entry
        if entry.startswith("@"):
            url = f"https://www.youtube.com/{entry}"
        elif not entry.startswith("http"):
            url = f"https://www.youtube.com/@{entry}"
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        m = (
            re.search(r'"channelId":"(UC[\w-]{22})"', r.text)
            or re.search(r"channel_id=(UC[\w-]{22})", r.text)
            or re.search(r"youtube\.com/channel/(UC[\w-]{22})", r.text)
        )
        if not m:
            raise ValueError(f"channel id nahi mila: {entry}")
        cid = m.group(1)
    with LOCK:
        STATE["resolved"][entry] = cid
        save_state()
    return cid


def fetch_feed(cid):
    r = requests.get(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.findtext("a:title", default=cid, namespaces=NS)
    videos = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        title = e.findtext("a:title", default="", namespaces=NS)
        if vid:
            videos.append((vid, title))
    videos.reverse()  # purana pehle, naya baad me
    return channel, videos


def send_telegram(text):
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("telegram send fail (%s): %s", attempt + 1, e)
            time.sleep(2)
    return False


def deliver(cid, channel, vid, title):
    """Naya video bhejo (dedupe ke saath). Push aur polling dono yahi use karte hain."""
    with LOCK:
        seen = STATE["seen"].setdefault(cid, [])
        if vid in seen:
            return
        url = f"https://www.youtube.com/watch?v={vid}"
        msg = f"🔔 <b>{html.escape(channel)}</b>\n{html.escape(title)}\n{url}"
        if send_telegram(msg):
            seen.append(vid)
            STATE["seen"][cid] = seen[-100:]
            save_state()
            log.info("sent: %s - %s", channel, title)


def sync_channel(cid, channel, videos):
    """Polling: naya channel ho to purane videos seed karo (spam nahi), warna naye bhejo."""
    with LOCK:
        if cid not in STATE["seen"]:
            STATE["seen"][cid] = [v[0] for v in videos]
            save_state()
            log.info("%s add hua (%d purane videos skip)", channel, len(videos))
            return
    for vid, title in videos:
        deliver(cid, channel, vid, title)


def poll_one(entry):
    cid = resolve_channel_id(entry)
    channel, videos = fetch_feed(cid)
    sync_channel(cid, channel, videos)
    return cid


# ---------------- Push (WebSub) ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/yt" and "hub.challenge" in q:
            topic = q.get("hub.topic", [""])[0]
            if "youtube.com/xml/feeds/videos.xml" in topic:
                log.info("hub verify: %s %s", q.get("hub.mode", [""])[0], topic)
                self._reply(200, q["hub.challenge"][0].encode())
                return
            self._reply(404)
            return
        self._reply(200, b"ok")

    def do_POST(self):
        if urlparse(self.path).path != "/yt":
            self._reply(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        sig = self.headers.get("X-Hub-Signature", "")
        try:
            algo, hexsig = sig.split("=", 1)
            good = hmac.new(SECRET.encode(), body, getattr(hashlib, algo)).hexdigest()
            if not hmac.compare_digest(good, hexsig):
                raise ValueError("bad signature")
        except Exception:
            log.warning("push reject: signature galat/missing")
            self._reply(403)
            return

        self._reply(204)
        try:
            root = ET.fromstring(body)
            for e in root.findall("a:entry", NS):
                vid = e.findtext("yt:videoId", namespaces=NS)
                cid = e.findtext("yt:channelId", namespaces=NS)
                title = e.findtext("a:title", default="", namespaces=NS)
                name = e.findtext("a:author/a:name", default=cid or "", namespaces=NS)
                if vid and cid:
                    deliver(cid, name, vid, title)
        except Exception:
            log.exception("push parse error")


def subscribe(cid):
    r = requests.post(
        HUB,
        data={
            "hub.callback": f"{PUBLIC_URL}/yt",
            "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={cid}",
            "hub.verify": "async",
            "hub.mode": "subscribe",
            "hub.lease_seconds": "432000",
            "hub.secret": SECRET,
        },
        timeout=20,
    )
    if r.status_code not in (202, 204):
        raise RuntimeError(f"hub {r.status_code}: {r.text[:200]}")


def subscriber_loop():
    subscribed = {}
    while True:
        for entry in load_channels():
            try:
                cid = poll_one(entry)  # pehle seed, phir subscribe
                if time.time() - subscribed.get(cid, 0) > 24 * 3600:
                    subscribe(cid)
                    subscribed[cid] = time.time()
                    log.info("subscribed: %s", cid)
            except Exception as e:
                log.warning("subscribe skip %s: %s", entry, e)
        time.sleep(300)


# ---------------- Main ----------------

def main():
    if PUBLIC_URL:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=subscriber_loop, daemon=True).start()
        log.info("Push mode ON: %s/yt (port %s)", PUBLIC_URL, PORT)
    else:
        log.warning("PUBLIC_URL set nahi hai -> sirf polling mode")

    log.info("Backup polling har %ss", INTERVAL)
    while True:
        for entry in load_channels():
            try:
                poll_one(entry)
            except Exception as e:
                log.warning("skip %s: %s", entry, e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
