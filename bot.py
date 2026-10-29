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
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))  # backup polling (seconds)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. http://1.2.3.4:8080
PORT = int(os.getenv("PORT", "8080"))
SECRET = os.getenv("WEBSUB_SECRET", "change-me-secret")
HUB = "https://pubsubhubbub.appspot.com/subscribe"
TG = f"https://api.telegram.org/bot{TOKEN}"

BASE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(BASE, "channels.txt")  # optional: sirf pehli baar import ke liye
STATE_FILE = os.path.join(BASE, "state.json")

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1; SOCS=CAI"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

LOCK = threading.RLock()
SUBSCRIBED = {}  # cid -> last subscribe time


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("seen", {})
    s.setdefault("resolved", {})
    s.setdefault("channels", {})  # cid -> channel name
    s.setdefault("imported", [])
    s.setdefault("tg_offset", 0)
    return s


STATE = _load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f)
    os.replace(tmp, STATE_FILE)


# ---------------- YouTube helpers ----------------

def resolve_channel_id(entry):
    """entry: UC... id, @handle, channel URL ya video URL -> channel id"""
    entry = entry.strip()
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


# ---------------- Telegram send + dedupe ----------------

def send_telegram(text):
    for attempt in range(3):
        try:
            r = requests.post(
                f"{TG}/sendMessage",
                data={
                    "chat_id": CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "false",
                },
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
        if cid not in STATE["channels"]:
            return  # channel remove ho chuka hai
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
    """Naya channel ho to purane videos seed karo (spam nahi), warna naye bhejo."""
    with LOCK:
        if cid not in STATE["seen"]:
            STATE["seen"][cid] = [v[0] for v in videos]
            save_state()
            log.info("%s add hua (%d purane videos skip)", channel, len(videos))
            return
    for vid, title in videos:
        deliver(cid, channel, vid, title)


# ---------------- WebSub push ----------------

def subscribe(cid, mode="subscribe"):
    r = requests.post(
        HUB,
        data={
            "hub.callback": f"{PUBLIC_URL}/yt",
            "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={cid}",
            "hub.verify": "async",
            "hub.mode": mode,
            "hub.lease_seconds": "432000",
            "hub.secret": SECRET,
        },
        timeout=20,
    )
    if r.status_code not in (202, 204):
        raise RuntimeError(f"hub {r.status_code}: {r.text[:200]}")


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


def subscriber_loop():
    """Har channel ki subscription ko 24 ghante me renew karta rehta hai."""
    while True:
        with LOCK:
            cids = list(STATE["channels"])
        for cid in cids:
            if time.time() - SUBSCRIBED.get(cid, 0) > 24 * 3600:
                try:
                    subscribe(cid)
                    SUBSCRIBED[cid] = time.time()
                    log.info("subscribed: %s", cid)
                except Exception as e:
                    log.warning("subscribe fail %s: %s", cid, e)
        time.sleep(300)


# ---------------- Channel management ----------------

def add_channel(entry):
    cid = resolve_channel_id(entry)
    channel, videos = fetch_feed(cid)
    with LOCK:
        new = cid not in STATE["channels"]
        STATE["channels"][cid] = channel
        save_state()
    sync_channel(cid, channel, videos)
    if PUBLIC_URL:
        try:
            subscribe(cid)
            SUBSCRIBED[cid] = time.time()
        except Exception as e:
            log.warning("subscribe fail %s: %s", cid, e)
    return channel, new


def remove_channel(arg):
    arg = arg.strip()
    with LOCK:
        items = list(STATE["channels"].items())
    cid = None
    if arg.isdigit() and 1 <= int(arg) <= len(items):
        cid = items[int(arg) - 1][0]
    else:
        try:
            c = resolve_channel_id(arg)
            if c in STATE["channels"]:
                cid = c
        except Exception:
            pass
        if not cid:
            a = arg.lower().lstrip("@")
            for c, n in items:
                if a and a in n.lower():
                    cid = c
                    break
    if not cid:
        return None
    with LOCK:
        name = STATE["channels"].pop(cid, cid)
        STATE["seen"].pop(cid, None)
        save_state()
    SUBSCRIBED.pop(cid, None)
    if PUBLIC_URL:
        try:
            subscribe(cid, "unsubscribe")
        except Exception as e:
            log.warning("unsubscribe fail %s: %s", cid, e)
    return name


def import_channels_file():
    """channels.txt me kuch likha ho to ek baar bot me import kar do."""
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as f:
            lines = [l.split("#", 1)[0].strip() for l in f]
    except FileNotFoundError:
        return
    for entry in [l for l in lines if l]:
        if entry in STATE["imported"]:
            continue
        try:
            add_channel(entry)
            STATE["imported"].append(entry)
            save_state()
        except Exception as e:
            log.warning("import skip %s: %s", entry, e)


# ---------------- Telegram commands ----------------

HELP = (
    "<b>YouTube Notification Bot</b>\n\n"
    "/add &lt;@handle | link | UC id&gt; - channel add (ek saath kai bhi)\n"
    "/remove &lt;number | naam | @handle&gt; - channel hatao\n"
    "/list - saare channels\n\n"
    "Shortcut: bas channel ka link ya @handle seedha bhej do, add ho jayega."
)


def cmd_list():
    with LOCK:
        items = list(STATE["channels"].values())
    if not items:
        return "Abhi koi channel nahi hai. /add se daalo."
    lines = [f"{i}. {html.escape(n)}" for i, n in enumerate(items, 1)]
    return f"<b>{len(items)} channels:</b>\n" + "\n".join(lines) + "\n\nHatane ke liye: /remove &lt;number&gt;"


def handle_message(text):
    parts = text.split()
    if not parts:
        return
    first = parts[0]
    if first.startswith("/"):
        cmd = first.split("@")[0].lower()
        args = parts[1:]
        if cmd in ("/start", "/help"):
            send_telegram(HELP)
        elif cmd == "/list":
            send_telegram(cmd_list())
        elif cmd == "/add":
            if not args:
                send_telegram("Aise use karo: /add @MrBeast")
                return
            do_add(args)
        elif cmd in ("/remove", "/delete", "/del"):
            if not args:
                send_telegram("Aise use karo: /remove 2  (number /list me dekho)")
                return
            # naam me space ho sakta hai, isliye number/handle ko alag, warna sab jodke naam
            if all(a.isdigit() for a in args):
                targets = sorted(set(args), key=int, reverse=True)  # bade number pehle
            else:
                targets = [" ".join(args)]
            out = []
            for t in targets:
                name = remove_channel(t)
                out.append(f"🗑 Removed: {html.escape(name)}" if name else f"❌ Nahi mila: {html.escape(t)}")
            send_telegram("\n".join(out))
        else:
            send_telegram(HELP)
        return

    # command nahi hai: link / @handle / UC id ho to add kar do
    found = re.findall(r"(https?://\S+|@[\w.\-]+|UC[\w-]{22})", text)
    if found:
        do_add(found)


def do_add(entries):
    out = []
    for e in entries:
        try:
            name, new = add_channel(e)
            out.append(f"✅ Added: {html.escape(name)}" if new else f"ℹ️ Pehle se hai: {html.escape(name)}")
        except Exception as ex:
            log.warning("add fail %s: %s", e, ex)
            out.append(f"❌ Add nahi hua: {html.escape(e)}\n(link ya UC... id try karo)")
    send_telegram("\n".join(out))


def bot_loop():
    try:
        requests.post(
            f"{TG}/setMyCommands",
            data={
                "commands": json.dumps(
                    [
                        {"command": "add", "description": "Channel add karo"},
                        {"command": "remove", "description": "Channel hatao"},
                        {"command": "list", "description": "Saare channels dekho"},
                        {"command": "help", "description": "Help"},
                    ]
                )
            },
            timeout=20,
        )
    except Exception:
        pass
    offset = STATE.get("tg_offset", 0)
    while True:
        try:
            r = requests.get(
                f"{TG}/getUpdates",
                params={"timeout": 30, "offset": offset, "allowed_updates": json.dumps(["message"])},
                timeout=45,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                    continue  # sirf tumhari chat
                try:
                    handle_message(msg.get("text", "") or "")
                except Exception:
                    log.exception("command error")
            if updates:
                with LOCK:
                    STATE["tg_offset"] = offset
                    save_state()
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            time.sleep(5)


# ---------------- Main ----------------

def main():
    threading.Thread(target=bot_loop, daemon=True).start()
    if PUBLIC_URL:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=subscriber_loop, daemon=True).start()
        log.info("Push mode ON: %s/yt (port %s)", PUBLIC_URL, PORT)
    else:
        log.warning("PUBLIC_URL set nahi hai -> sirf polling mode")

    import_channels_file()
    log.info("Bot ready. Backup polling har %ss", INTERVAL)
    while True:
        with LOCK:
            cids = list(STATE["channels"])
        for cid in cids:
            try:
                channel, videos = fetch_feed(cid)
                with LOCK:
                    if cid in STATE["channels"]:
                        STATE["channels"][cid] = channel
                sync_channel(cid, channel, videos)
            except Exception as e:
                log.warning("poll skip %s: %s", cid, e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
