#!/usr/bin/env python3
"""NeeDoh Advent Calendar stock checker. Plain Python, no AI calls.

Usage:
  python check.py               one run, notify on out-of-stock -> in-stock flips
  python check.py --test        send one test notification
  python check.py --loop        keep running, check every 90s
  python check.py --loop 60     keep running, check every 60s

The ntfy topic is read from the NTFY_TOPIC environment variable (kept out of the
repo because the repo is public and anyone who knows the topic can read/post).
"""
import argparse, json, os, random, re, sys, time
from pathlib import Path

import requests

HERE = Path(__file__).parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9",
           "Accept": "text/html,application/json,*/*;q=0.8"}
MAX_PRICE = 45.0
BAD_WORDS = re.compile(r"back[\s-]?order|pre[\s-]?order|bundle|notify me|notify when|email me when", re.I)
NEG_WORDS = re.compile(r"out of stock|sold out|currently unavailable|unavailable|notify me", re.I)
POS_WORDS = re.compile(r"add to (basket|bag|cart)", re.I)

IN, OUT, BLOCKED, WATCHING, ERROR = "in stock", "out", "blocked", "watching", "error"


def get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=25, **kw)


class Blocked(Exception):
    pass


def fetch(url, **kw):
    r = get(url, **kw)
    if r.status_code in (401, 403, 429, 503) or r.status_code >= 500:
        raise Blocked(f"HTTP {r.status_code}")
    if r.status_code == 404:
        raise Blocked("HTTP 404 (no page yet)")
    return r


def is_needoh_advent(title):
    t = title.lower()
    return bool(re.search(r"nee-?\s?doh", t)) and "advent" in t


# ---------- result: (status, price or None, url, note) ----------

def jsonld_nodes(html):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                stack.extend(n)
            elif isinstance(n, dict):
                yield n
                stack.extend(v for v in n.values() if isinstance(v, (list, dict)))


def check_jsonld(r, url):
    html = fetch(url).text
    avail, price = None, None
    for n in jsonld_nodes(html):
        offers = n.get("offers") if n.get("@type") in ("Product", "ProductGroup") or "offers" in n else None
        for o in (offers if isinstance(offers, list) else [offers] if offers else []):
            if not isinstance(o, dict):
                continue
            a = str(o.get("availability", ""))
            if a:
                avail = a.rsplit("/", 1)[-1]
                try:
                    price = float(o.get("price") or o.get("lowPrice"))
                except (TypeError, ValueError):
                    pass
                if avail == "InStock":
                    break
    if avail is None:
        # fall back to button text
        if len(html) < 8000 and not re.search(r"add to|out of stock", html, re.I):
            raise Blocked("page empty or JavaScript-only")
        buttons = " ".join(re.findall(r"<button[^>]*>(.*?)</button>", html, re.S | re.I))
        buttons = re.sub(r"<[^>]+>", " ", buttons)
        if BAD_WORDS.search(buttons) or NEG_WORDS.search(buttons):
            return OUT, None, url, "text marker"
        if POS_WORDS.search(buttons):
            return IN, None, url, "text marker"
        raise Blocked("no stock markers found")
    if avail == "InStock" and BAD_WORDS.search(re.sub(r"<[^>]+>", " ", " ".join(re.findall(r"<button[^>]*>(.*?)</button>", html, re.S | re.I)))):
        return OUT, price, url, "button says pre-order/notify"
    return (IN if avail == "InStock" else OUT), price, url, avail


def shopify_product(r):
    base, handle = r["base"], r["handle"]
    url = f"{base}/products/{handle}"
    j = fetch(url + ".js").json()
    variants = j.get("variants", [])
    avail_vars = [v for v in variants if v.get("available")]
    price = min((v["price"] for v in avail_vars), default=None)
    price = price / 100 if price is not None else None
    if not avail_vars:
        return OUT, (min(v["price"] for v in variants) / 100 if variants else None), url, "sold out"
    # Backorder / pre-order / bundle / notify-me guards
    text = " ".join([j.get("title", ""), " ".join(j.get("tags", []) if isinstance(j.get("tags"), list) else [str(j.get("tags", ""))]),
                     " ".join(v.get("title", "") + " " + str(v.get("name", "")) for v in avail_vars)])
    if BAD_WORDS.search(text):
        return OUT, price, url, "title/variant says pre-order/bundle"
    try:
        html = fetch(url).text
        buttons = re.sub(r"<[^>]+>", " ", " ".join(re.findall(r"<button[^>]*>(.*?)</button>", html, re.S | re.I)))
        if BAD_WORDS.search(buttons):
            return OUT, price, url, "button says pre-order/backorder/notify"
        if re.search(r'"(inventory_policy|continue_selling)"\s*:\s*"?(continue|true)', html) and \
           re.search(r"(only|just)?\s*0 (left|in stock)|backorder", html, re.I):
            return OUT, price, url, "backorder"
    except Blocked:
        pass  # .js said available; keep that
    return IN, price, url, "available"


def shopify_watch(r):
    base = r["base"]
    resp = fetch(f"{base}/search/suggest.json",
                 params={"q": "needoh advent", "resources[type]": "product", "resources[limit]": 10})
    prods = resp.json()["resources"]["results"]["products"]
    hits = [p for p in prods if is_needoh_advent(p["title"])]
    if not hits:
        return WATCHING, None, f"{base}/search?q=needoh+advent", "no listing yet"
    best = None
    for p in hits:
        price = float(p.get("price") or 0) or None
        url = base + p["url"].split("?")[0]
        if p.get("available") and not BAD_WORDS.search(p["title"]):
            return IN, price, url, p["title"]
        best = (OUT, price, url, p["title"])
    return best


def amazon_watch(r):
    html = fetch(r["url"]).text
    if re.search(r"captcha|robot check|automated access", html, re.I):
        raise Blocked("captcha")
    asins = []
    for m in re.finditer(r'data-asin="([A-Z0-9]{10})"(.*?)(?=data-asin="|$)', html, re.S):
        chunk = re.sub(r"<[^>]+>", " ", m.group(2))[:600]
        if is_needoh_advent(chunk):
            asins.append(m.group(1))
    if not asins:
        return WATCHING, None, r["url"], "no listing yet"
    limit = r.get("max_price", 40)
    for asin in asins[:3]:
        time.sleep(random.uniform(2, 4))
        url = f"https://www.amazon.co.uk/dp/{asin}"
        page = fetch(url).text
        text = re.sub(r"<[^>]+>", " ", page)
        m = re.search(r'a-offscreen">\s*£([\d.,]+)', page)
        price = float(m.group(1).replace(",", "")) if m else None
        sold_by_amazon = bool(re.search(r"(Ships from|Dispatches from)\s*Amazon", text, re.I)) and \
                         bool(re.search(r"Sold by\s*Amazon", text, re.I))
        in_cart = bool(re.search(r'id="add-to-cart-button"', page)) and not re.search(r"currently unavailable", text, re.I)
        if in_cart and sold_by_amazon and price is not None and price < limit:
            return IN, price, url, "sold by Amazon"
    return OUT, None, r["url"], "not Amazon-sold / over price / unavailable"


def check_retailer(r):
    mode = r.get("mode", "product")
    try:
        if r["type"] == "amazon":
            return amazon_watch(r)
        if r["type"] == "shopify":
            return shopify_watch(r) if mode == "watch" else shopify_product(r)
        if r["type"] == "jsonld":
            if mode == "watch":
                return watch_html(r)
            return check_jsonld(r, r["url"])[0:4]
    except Blocked as e:
        return BLOCKED, None, r.get("url") or r.get("base"), str(e)
    except (requests.RequestException, ValueError, KeyError) as e:
        return ERROR, None, r.get("url") or r.get("base"), f"{type(e).__name__}: {e}"[:120]


def watch_html(r):
    html = fetch(r["url"]).text
    links = re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I)
    for href, label in links:
        if is_needoh_advent(re.sub(r"<[^>]+>", " ", label) + " " + href):
            full = requests.compat.urljoin(r["url"], href)
            time.sleep(random.uniform(2, 4))
            return check_jsonld(r, full)
    return WATCHING, None, r["url"], "no listing yet"


# ---------- state + notify ----------

def load_state():
    try:
        return json.loads((HERE / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(s):
    (HERE / "state.json").write_text(json.dumps(s, indent=2, sort_keys=True) + "\n")


def ntfy(title, body, click=None, priority="high"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("  ! NTFY_TOPIC is not set, so no notification was sent.")
        return False
    h = {"Title": title, "Priority": priority, "Tags": "tada"}
    if click:
        h["Click"] = click
    try:
        resp = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=h, timeout=20)
        print(f"  ntfy -> HTTP {resp.status_code}")
        return resp.ok
    except requests.RequestException as e:
        print(f"  ntfy failed: {e}")
        return False


def run_once(retailers, state):
    rows = []
    for i, r in enumerate(retailers):
        if i:
            time.sleep(random.uniform(2, 6))  # polite random gap between sites
        status, price, url, note = check_retailer(r)
        sym = r.get("currency", "£")
        limit = r.get("max_price", MAX_PRICE)
        name = r["name"]
        prev = state.get(name, {}).get("status")
        if status in (BLOCKED, ERROR):
            # keep the last good status so a blip can't cause a false "flip"
            pass
        else:
            state[name] = {"status": status, "price": price, "url": url,
                           "checked": time.strftime("%Y-%m-%d %H:%M:%S")}
        if status == IN and prev != IN:
            if price is None or price < limit:
                p = f"{sym}{price:.2f}" if price is not None else "price unknown"
                ntfy("NeeDoh calendar IN STOCK", f"{name}: {p}", click=url)
            else:
                print(f"  {name} in stock but {sym}{price:.2f} is over the limit, not notifying")
        rows.append((name, status, f"{sym}{price:.2f}" if price is not None else "-", note))
    return rows


def print_table(rows):
    w = max(len(r[0]) for r in rows)
    print(f"\n{'Retailer'.ljust(w)}  {'Status'.ljust(9)}  {'Price'.ljust(8)}  Note")
    for n, s, p, note in rows:
        print(f"{n.ljust(w)}  {s.ljust(9)}  {p.ljust(8)}  {note}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send one test notification and exit")
    ap.add_argument("--loop", nargs="?", const=90, type=int, metavar="SECONDS",
                    help="keep running, checking every SECONDS (default 90)")
    a = ap.parse_args()

    if a.test:
        ok = ntfy("NeeDoh checker TEST", "If you can read this, notifications work.", click="https://github.com")
        sys.exit(0 if ok else 1)

    retailers = json.loads((HERE / "retailers.json").read_text())
    while True:
        state = load_state()
        rows = run_once(retailers, state)
        save_state(state)
        print_table(rows)
        if a.loop is None:
            break
        gap = max(a.loop, 30)
        print(f"Next check in about {gap}s (Ctrl+C to stop)")
        try:
            time.sleep(gap + random.uniform(0, 10))
        except KeyboardInterrupt:
            print("Stopped.")
            break


if __name__ == "__main__":
    main()
