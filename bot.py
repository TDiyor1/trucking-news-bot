#!/usr/bin/env python3
"""US trucking news -> Telegram channel.

Polls trade-press RSS feeds, tags each new article by topic, and posts it
(headline, excerpt, link) to a Telegram channel. Stdlib only.

Usage:
  python3 bot.py --dry-run     # print what would be posted, send nothing
  python3 bot.py --seed        # mark everything currently in feeds as seen
  python3 bot.py               # post new articles
Config: .env next to this file (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import argparse, html, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
MAX_AGE = timedelta(hours=24)
MAX_PER_RUN = 12
UA = "Mozilla/5.0 (TruckingNewsBot)"

FEEDS = {
    "FreightWaves": "https://www.freightwaves.com/news/feed",
    "Transport Topics": "https://www.ttnews.com/rss.xml",
    "CCJ": "https://www.ccjdigital.com/feed",
    "Trucking Dive": "https://www.truckingdive.com/feeds/news/",
    "Supply Chain Dive": "https://www.supplychaindive.com/feeds/news/",
    "DC Velocity": "https://www.dcvelocity.com/rss",
    "Land Line": "https://landline.media/feed/",
    "Freight Caviar": "https://www.freightcaviar.com/feed/",
    "JOC": "https://www.joc.com/rss.xml",
}

# Sources that cover more than trucking: an article must hit a topic to be posted.
BROAD = {"Supply Chain Dive", "DC Velocity", "JOC", "Transport Topics", "Freight Caviar"}

TOPICS = {
    "Rates": r"\brates?\b|spot market|contract (rate|market)|freight (market|index)|load[- ]to[- ]truck|tender reject|DAT\b|SONAR|Cass|truckload",
    "Diesel": r"diesel|fuel (price|surcharge|cost)|oil prices?|crude",
    "Regulation": r"FMCSA|\bDOT\b|\bCDL\b|English proficiency|non-domiciled|hours[- ]of[- ]service|\bHOS\b|rulemaking|regulation|rule(making)?\b|Congress|\bbill\b|safety rating|CSA\b|Department of Transportation|Duffy",
    "Tariffs": r"tariff|cross-border|Mexico|Canada|USMCA|customs|trade (war|deal|policy)|imports\b",
    "Capacity": r"capacity|bankrupt|shut(s|ting)? down|ceas(es|ed) operations|insolven|layoff|carrier exits?|Chapter 11",
    "Fraud": r"fraud|scam|cargo theft|stolen|double[- ]brokering|identity theft|fictitious pickup|broker (license|bond)|\bBMC-?84\b",
    "Tech": r"\bELD\b|autonomous|self-driving|Aurora|Kodiak|telematics|\bAI\b|electric truck|platooning",
    "Ports/Rail": r"\bport\b|ports of|longshore|ILA|ILWU|railroad|\brail\b|intermodal|Union Pacific|CSX|BNSF|Norfolk Southern|container",
}
TOPIC_RE = {k: re.compile(v, re.I) for k, v in TOPICS.items()}


def load_env():
    env = dict(os.environ)
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def clean(text):
    text = re.sub(r"<(script|style).*?</\1>", " ", text or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"(The post .*? appeared first on .*)$", "", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()


def text_of(el, *names):
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(source, raw):
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom", "c": "http://purl.org/rss/1.0/modules/content/"}
    items = []
    for it in root.iter("item"):
        link = text_of(it, "link")
        body = text_of(it, "{http://purl.org/rss/1.0/modules/content/}encoded", "description")
        items.append(dict(
            source=source, title=clean(text_of(it, "title")), link=link,
            id=text_of(it, "guid") or link,
            date=parse_date(text_of(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
            body=clean(body)))
    for en in root.iter("{http://www.w3.org/2005/Atom}entry"):
        l = en.find("a:link", ns)
        link = l.get("href") if l is not None else ""
        body = text_of(en, "a:content", "a:summary") if False else (
            (en.findtext("a:content", namespaces=ns) or en.findtext("a:summary", namespaces=ns) or ""))
        items.append(dict(
            source=source, title=clean(en.findtext("a:title", namespaces=ns) or ""), link=link,
            id=en.findtext("a:id", namespaces=ns) or link,
            date=parse_date(en.findtext("a:published", namespaces=ns) or en.findtext("a:updated", namespaces=ns)),
            body=clean(body)))
    return items


def tag(item):
    hay = item["title"] + " " + item["body"][:400]
    # title hits count first so the headline drives the label
    return [k for k, rx in TOPIC_RE.items() if rx.search(hay)]


# Who the story matters to. Explicit keywords win; otherwise fall back to topic defaults.
AUDIENCE_LABEL = {"Carrier": "🚚 CARRIER", "Shipper": "📦 SHIPPER", "Broker": "🤝 BROKER"}
AUDIENCE_KW = {
    "Carrier": r"\bcarriers?\b|fleets?\b|owner[- ]operators?|\bdrivers?\b|truck parking|\bELD\b|\bCDL\b|hours[- ]of[- ]service|\bHOS\b|diesel|fuel (price|cost)|Class 8|equipment|trailers?|tolls?|inspection|CSA\b|safety rating|insurance|autonomous|truck makers?|dealers?",
    "Shipper": r"\bshippers?\b|retailers?|manufacturers?|supply chain|inventory|warehous|importers?|exporters?|ocean|container|intermodal|procurement|tender|contract rates?|\bRFP\b",
    "Broker": r"\bbrokers?\b|brokerage|\b3PLs?\b|freight forwarders?|load boards?|double[- ]brokering|\bBMC-?84\b|broker bond|spot (rates?|market)|margins?|fictitious pickup|cargo theft|\bDAT\b|Truckstop",
}
AUDIENCE_RE = {k: re.compile(v, re.I) for k, v in AUDIENCE_KW.items()}
TOPIC_AUDIENCE = {
    "Rates": {"Carrier", "Shipper", "Broker"}, "Diesel": {"Carrier"},
    "Regulation": {"Carrier", "Broker"}, "Tariffs": {"Shipper", "Broker"},
    "Capacity": {"Carrier", "Broker"}, "Fraud": {"Broker", "Carrier"},
    "Tech": {"Carrier"}, "Ports/Rail": {"Shipper", "Broker"},
}


def audience(item, tags):
    hay = item["title"] + " " + item["body"][:600]
    hits = {k for k, rx in AUDIENCE_RE.items() if rx.search(hay)}
    if not hits:
        for t in tags:
            hits |= TOPIC_AUDIENCE.get(t, set())
    if len(hits) in (0, 3):
        return "🌐 ALL: CARRIER · SHIPPER · BROKER"
    return " · ".join(AUDIENCE_LABEL[k] for k in ("Carrier", "Shipper", "Broker") if k in hits)


def excerpt(body, limit=700):
    if len(body) <= limit:
        return body
    cut = body[:limit].rsplit(" ", 1)[0]
    m = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[:m + 1] if m > 250 else cut + "…")


def format_msg(item, tags):
    e = html.escape
    hashtags = " ".join("#" + t.replace("/", "_") for t in tags)
    parts = [f"<b>{audience(item, tags)}</b>\n<b>{e(item['title'])}</b>"]
    if hashtags:
        parts.append(hashtags)
    ex = excerpt(item["body"])
    if ex:
        parts.append(e(ex))
    parts.append(f"{e(item['source'])} · <a href=\"{e(item['link'], quote=True)}\">Read full article</a>")
    return "\n\n".join(parts)[:4000]


def send(env, text):
    data = urllib.parse.urlencode({
        "chat_id": env["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
    }).encode()
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
    for attempt in range(3):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()
            return True
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            print("Telegram error", ex.code, ex.read().decode()[:200], file=sys.stderr)
            return False
        except Exception as ex:
            print("send failed:", ex, file=sys.stderr)
            time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args()

    seen = set(json.load(open(STATE))) if os.path.exists(STATE) else set()
    now = datetime.now(timezone.utc)
    fresh = []
    for source, url in FEEDS.items():
        try:
            items = parse_feed(source, fetch(url))
        except Exception as ex:
            print(f"[warn] {source}: {ex}", file=sys.stderr)
            continue
        for it in items:
            if not it["link"] or not it["title"] or it["id"] in seen:
                continue
            fresh.append(it)

    if args.seed:
        seen |= {i["id"] for i in fresh}
        json.dump(sorted(seen)[-5000:], open(STATE, "w"))
        print(f"Seeded {len(fresh)} existing articles; nothing sent.")
        return

    queue = []
    for it in fresh:
        if it["date"] and now - it["date"] > MAX_AGE:
            seen.add(it["id"])  # too old, never post
            continue
        tags = tag(it)
        if not tags and it["source"] in BROAD:
            seen.add(it["id"])
            continue
        queue.append((it, tags))
    queue.sort(key=lambda p: p[0]["date"] or now)  # oldest first
    # keep the most recent MAX_PER_RUN; drop (and mark seen) the overflow
    overflow, queue = queue[:-MAX_PER_RUN], queue[-MAX_PER_RUN:]
    seen |= {i["id"] for i, _ in overflow}

    env = load_env()
    if not args.dry_run and not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")

    sent = 0
    for it, tags in queue:
        msg = format_msg(it, tags)
        if args.dry_run:
            print(msg, "\n" + "-" * 60)
        elif send(env, msg):
            seen.add(it["id"])
            sent += 1
            time.sleep(1.2)
    if not args.dry_run:
        json.dump(sorted(seen)[-5000:], open(STATE, "w"))
    print(f"{'Would send' if args.dry_run else 'Sent'} {len(queue) if args.dry_run else sent} articles.")


if __name__ == "__main__":
    main()
