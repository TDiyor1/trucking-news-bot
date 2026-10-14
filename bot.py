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
NEWS = os.path.join(HERE, "news.json")  # rolling feed read by the website
NEWS_KEEP = 80
NEWS_MAX_AGE = timedelta(days=7)
SITE_EXCERPT = 300
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


def audience_set(item, tags):
    hay = item["title"] + " " + item["body"][:600]
    hits = {k for k, rx in AUDIENCE_RE.items() if rx.search(hay)}
    if not hits:
        for t in tags:
            hits |= TOPIC_AUDIENCE.get(t, set())
    return hits if len(hits) in (1, 2) else {"Carrier", "Shipper", "Broker"}


def audience(item, tags):
    hits = audience_set(item, tags)
    if len(hits) == 3:
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


def record(item, tags):
    d = item["date"] or datetime.now(timezone.utc)
    return {
        "id": item["id"], "title": item["title"], "url": item["link"], "source": item["source"],
        "audience": [k for k in ("Carrier", "Shipper", "Broker") if k in audience_set(item, tags)],
        "tags": tags, "excerpt": excerpt(item["body"], SITE_EXCERPT),
        "published": d.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def update_feed(records):
    old = json.load(open(NEWS))["items"] if os.path.exists(NEWS) else []
    merged = {r["id"]: r for r in old + records}
    cutoff = (datetime.now(timezone.utc) - NEWS_MAX_AGE).isoformat()
    items = sorted((r for r in merged.values() if r["published"] >= cutoff),
                   key=lambda r: r["published"], reverse=True)[:NEWS_KEEP]
    json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items},
              open(NEWS, "w"), indent=1, ensure_ascii=False)


def send(env, text, preview=True):
    data = urllib.parse.urlencode({
        "chat_id": env["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false" if preview else "true",
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


# ---------------------------------------------------------------- EIA weekly fuel report
EIA_URL = "https://www.eia.gov/petroleum/gasdiesel/"
EIA_STATES = {"California", "Colorado", "Florida", "Massachusetts", "Minnesota", "New York", "Ohio", "Texas", "Washington"}


def parse_eia():
    """Return {'week': 'MM/DD/YY', 'gas_regions': [...], 'gas_places': [...], 'diesel': [...]}.
    Each row is (name, [3 weekly prices oldest->newest], week_change, vs_year_ago, vs_2yr_ago)."""
    raw = fetch(EIA_URL).decode("utf-8", "ignore")
    raw = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S)
    sections, week = [], None
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, flags=re.S):
        c = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", x)).split())
             for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S)]
        if len(c) >= 7 and re.match(r"\d\d/\d\d/\d\d", c[1]):
            sections.append([])
            week = c[3]
        elif sections and len(c) == 7:
            try:
                v = [float(x) for x in c[1:]]
            except ValueError:
                continue
            sections[-1].append((c[0], v[:3], v[5], v[4], v[3]))
    if len(sections) < 3 or not week or sections[0][0][0] != "U.S." or sections[2][0][0] != "U.S.":
        raise ValueError("EIA page layout changed")
    return {"week": week, "gas_regions": sections[0], "gas_places": sections[1], "diesel": sections[2]}


def _short(name):
    name = re.sub(r"\s*\(PADD\s*\w+\)", "", name)
    return {"West Coast less California": "West Coast ex-CA", "Rocky Mountain": "Rocky Mtn"}.get(name, name)


def _chg(x):
    cents = round(x * 100)
    return "flat" if cents == 0 else f"{'▲' if cents > 0 else '▼'}{abs(cents)}¢"


def _summary(label, row):
    _, p, wk, yr, yr2 = row
    trend = "rose" if wk > 0 else "fell" if wk < 0 else "was unchanged"
    amt = f" {abs(round(wk * 100))}¢" if wk else ""
    streak = ""
    if p[2] < p[1] < p[0]:
        streak = ", the second straight weekly drop"
    elif p[2] > p[1] > p[0]:
        streak = ", the second straight weekly gain"
    return (f"{label} {trend}{amt} to ${p[2]:.2f}{streak}; it is "
            f"${abs(yr):.2f} {'above' if yr >= 0 else 'below'} a year ago.")


def _table(rows):
    w = max(len(_short(r[0])) for r in rows)
    return "\n".join(f"{_short(r[0]):<{w}}  ${r[1][2]:.3f}  {_chg(r[2]):>5}" for r in rows)


def format_eia(d):
    e = html.escape
    diesel_us, gas_us = d["diesel"][0], d["gas_regions"][0]
    def trend(row):
        return " → ".join(f"{x:.2f}" for x in row[1])
    parts = [
        f"⛽ <b>EIA Weekly Fuel Report</b>\nPrices as of {e(d['week'])} · US average, per gallon",
        f"🚛 <b>Diesel (on-highway)</b>\n<b>${diesel_us[1][2]:.3f}</b> {_chg(diesel_us[2])} vs last week\n"
        f"Last 3 weeks: {trend(diesel_us)}\n"
        f"vs 1 yr ago {diesel_us[3]:+.2f} · vs 2 yrs ago {diesel_us[4]:+.2f}",
        "<pre>" + e(_table(d["diesel"][1:])) + "</pre>",
        f"🚗 <b>Regular gasoline</b>\n<b>${gas_us[1][2]:.3f}</b> {_chg(gas_us[2])} vs last week\n"
        f"Last 3 weeks: {trend(gas_us)}\n"
        f"vs 1 yr ago {gas_us[3]:+.2f} · vs 2 yrs ago {gas_us[4]:+.2f}",
        "<pre>" + e(_table(d["gas_regions"][1:])) + "</pre>",
        "📍 <b>Gasoline by state</b>\n<pre>" + e(_table([r for r in d["gas_places"] if r[0] in EIA_STATES])) + "</pre>",
        "<b>In short:</b> " + e(_summary("Diesel", diesel_us)) + " " + e(_summary("Regular gas", gas_us)),
        f"Source: <a href=\"{EIA_URL}\">U.S. Energy Information Administration</a>. Regional averages, "
        "not station prices; actual pump and truck-stop prices vary.",
    ]
    return "\n\n".join(parts)[:4000]


def eia_report(env, seen, dry_run=False):
    """Post this week's EIA report once, as soon as EIA publishes a new week."""
    try:
        d = parse_eia()
    except Exception as ex:
        print(f"[warn] EIA: {ex}", file=sys.stderr)
        return
    key = "eia:" + d["week"]
    if key in seen:
        return
    msg = format_eia(d)
    if dry_run:
        print(msg, "\n" + "-" * 60)
    elif send(env, msg, preview=False):
        seen.add(key)
        print(f"Posted EIA weekly report for {d['week']}.")


# ---------------------------------------------------------------- LinkedIn draft feed (optional)
# Separate private chat (TELEGRAM_LINKEDIN_CHAT_ID). Skipped entirely when the secret is unset,
# so the main channel is never affected.
LI_TAKEAWAY = {
    "Rates": "Rate moves change what shippers should budget and what carriers should quote.",
    "Diesel": "Fuel swings hit carrier margins first, then show up in shipper surcharges.",
    "Regulation": "Rule changes affect which carriers can legally haul, and shippers' capacity options.",
    "Tariffs": "Trade policy shifts freight lanes and timing, so shippers should plan routing early.",
    "Capacity": "Capacity shifts decide who gets trucks and at what price.",
    "Fraud": "Fraud and cargo theft are rising risks: vet carriers and verify pickups.",
    "Tech": "New tech can lower costs for carriers and improve visibility for shippers.",
    "Ports/Rail": "Port and rail disruptions ripple into truck demand and delivery times.",
}


def linkedin_ok(item, tags):
    return bool(tags) and bool(audience_set(item, tags) & {"Carrier", "Shipper"})


def format_linkedin(item, tags):
    e = html.escape
    who = " & ".join(k.lower() + "s" for k in ("Shipper", "Carrier") if k in audience_set(item, tags))
    take = LI_TAKEAWAY.get(tags[0], "")
    ex = excerpt(item["body"], 350)
    tagline = " ".join("#" + t.replace("/", "") for t in tags[:3]) + " #Freight #Logistics #Trucking"
    draft = (f"{item['title']}\n\n{ex}\n\nWhy it matters for {who}: {take}\n\n"
             f"What are you seeing in your lanes? Message me if you need coverage.\n\n{tagline}")
    return (f"📝 <b>LinkedIn draft · {e(who)}</b>\n\n<pre>{e(draft)}</pre>\n"
            f"{e(item['source'])} · <a href=\"{e(item['link'], quote=True)}\">Source</a>")[:4000]


def linkedin_send(env, item, tags):
    chat = env.get("TELEGRAM_LINKEDIN_CHAT_ID")
    if not chat or not linkedin_ok(item, tags):
        return
    try:
        send(dict(env, TELEGRAM_CHAT_ID=chat), format_linkedin(item, tags), preview=False)
    except Exception as ex:  # never let this break the main channel run
        print("linkedin send failed:", ex, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="rebuild news.json from the last 48h; sends nothing")
    args = ap.parse_args()

    seen = set(json.load(open(STATE))) if os.path.exists(STATE) else set()
    now = datetime.now(timezone.utc)
    fresh, everything = [], []
    for source, url in FEEDS.items():
        try:
            items = parse_feed(source, fetch(url))
        except Exception as ex:
            print(f"[warn] {source}: {ex}", file=sys.stderr)
            continue
        everything += [i for i in items if i["link"] and i["title"]]
        for it in items:
            if not it["link"] or not it["title"] or it["id"] in seen:
                continue
            fresh.append(it)

    if args.seed:
        seen |= {i["id"] for i in fresh}
        json.dump(sorted(seen)[-5000:], open(STATE, "w"))
        print(f"Seeded {len(fresh)} existing articles; nothing sent.")
        return

    if args.backfill:
        recs = []
        for it in everything:
            tags = tag(it)
            if it["date"] and now - it["date"] <= timedelta(hours=48) and (tags or it["source"] not in BROAD):
                recs.append(record(it, tags))
        update_feed(recs)
        print(f"news.json rebuilt with {len(recs)} recent articles.")
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
    if env.get("LINKEDIN_TEST") == "true":  # manual delivery check: one draft from news.json
        for r in json.load(open(NEWS))["items"]:
            it = dict(title=r["title"], body=r["excerpt"], link=r["url"], source=r["source"])
            if r["tags"] and set(r["audience"]) & {"Carrier", "Shipper"}:
                linkedin_send(env, it, r["tags"])
                print("Sent LinkedIn test draft.")
                return
        sys.exit("No suitable article for test")
    if not args.dry_run and not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")

    eia_report(env, seen, args.dry_run)

    sent, posted = 0, []
    for it, tags in queue:
        msg = format_msg(it, tags)
        if args.dry_run:
            print(msg, "\n" + "-" * 60)
            if linkedin_ok(it, tags):
                print(format_linkedin(it, tags), "\n" + "=" * 60)
        elif send(env, msg):
            seen.add(it["id"])
            posted.append(record(it, tags))
            sent += 1
            linkedin_send(env, it, tags)
            time.sleep(1.2)
    if not args.dry_run:
        update_feed(posted)
        json.dump(sorted(seen)[-5000:], open(STATE, "w"))
    print(f"{'Would send' if args.dry_run else 'Sent'} {len(queue) if args.dry_run else sent} articles.")


if __name__ == "__main__":
    main()
