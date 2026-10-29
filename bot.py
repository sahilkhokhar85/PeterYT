import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds
BASE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(BASE, "channels.txt")
STATE_FILE = os.path.join(BASE, "state.json")

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1; SOCS=CAI"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("seen", {})
    s.setdefault("resolved", {})
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
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


def resolve_channel_id(entry, state):
    """entry: UC... id, @handle, ya channel URL -> channel id"""
    if re.fullmatch(r"UC[\w-]{22}", entry):
        return entry
    if entry in state["resolved"]:
        return state["resolved"][entry]
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
            or re.search(r'channel_id=(UC[\w-]{22})', r.text)
            or re.search(r'youtube\.com/channel/(UC[\w-]{22})', r.text)
        )
        if not m:
            raise ValueError(f"channel id nahi mila: {entry}")
        cid = m.group(1)
    state["resolved"][entry] = cid
    save_state(state)
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
            videos.append((vid, title, f"https://www.youtube.com/watch?v={vid}"))
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
            time.sleep(3)
    return False


def check_all():
    state = load_state()
    for entry in load_channels():
        try:
            cid = resolve_channel_id(entry, state)
            channel, videos = fetch_feed(cid)
        except Exception as e:
            log.warning("skip %s: %s", entry, e)
            continue

        first_time = cid not in state["seen"]
        seen = state["seen"].setdefault(cid, [])

        if first_time:
            # pehli baar: purane videos ka spam nahi, bas yaad rakh lo
            seen.extend(v[0] for v in videos)
            save_state(state)
            log.info("%s add hua (%d purane videos skip)", channel, len(videos))
            continue

        for vid, title, url in videos:
            if vid in seen:
                continue
            msg = f"🔔 <b>{html.escape(channel)}</b>\n{html.escape(title)}\n{url}"
            if send_telegram(msg):
                seen.append(vid)
                state["seen"][cid] = seen[-100:]
                save_state(state)
                log.info("sent: %s - %s", channel, title)
            else:
                break  # agle round me dobara try hoga


def main():
    log.info("Bot start. Har %ss me check karega.", INTERVAL)
    while True:
        try:
            check_all()
        except Exception:
            log.exception("loop error")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
