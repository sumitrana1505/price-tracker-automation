"""
Amazon.in mobile-site fetcher (pincode aware, async) - shared by the price tracker engines.

Same logic as primary_fetch.py:
  1. Every product is fetched with the MUMBAI pincode first.
  2. Out of stock there -> Bangalore, Delhi, Hyderabad are checked in parallel
     (first one in that order that has stock wins).
  3. Still out of stock -> all remaining pincodes are checked in parallel.
  Failed pincodes are retried; a product with no stock anywhere is reported as OOS.

Use from other code:
    from amazon_mobile import fetch_many
    results = fetch_many(["B0DZHNQJQW", ...])      # {asin: result dict}

Stand-alone (Excel in / Excel out, like primary_fetch.py):
    python amazon_mobile.py --input excel.xlsx --output primary_output.xlsx
"""
import argparse
import asyncio
import random
import re
import sys
import time
from datetime import datetime

import lxml.html as LH
import openpyxl
from curl_cffi.requests import AsyncSession

# --------------------------------------------------------------------------- config
# Order matters: the first four are the priority pincodes.
PINCODES = [
    ("Mumbai", "400008"),
    ("Bangalore", "560038"),
    ("Delhi", "110031"),
    ("Hyderabad", "500032"),
    ("Pune", "411003"),
    ("Lucknow", "226006"),
    ("Patna", "800012"),
    ("Kolkata", "700032"),
    ("Guwahati", "781011"),
    ("Ahmedabad", "380001"),
    ("Bhopal", "462023"),
    ("Chennai", "600003"),
    ("Coimbatore", "641001"),
    ("Hosur", "635109"),
    ("Hubli", "580020"),
]
PRIMARY = PINCODES[0]          # Mumbai
PRIORITY = PINCODES[1:4]       # Bangalore, Delhi, Hyderabad
REST = PINCODES[4:]            # everything else

BASE = "https://www.amazon.in"
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
IMPERSONATE = "chrome131_android"
CONCURRENCY = 12       # simultaneous page requests (raise for speed, lower if blocked)
TIMEOUT = 25
RETRIES = 3
PRIME_CASHBACK = True  # report Amazon Pay ICICI cashback at the Prime rate (5%) instead of 3%
DEBUG = False         # --debug prints what Amazon answers to the pincode calls

OOS_PAT = re.compile(
    r"currently unavailable|out of stock|temporarily out of stock|"
    r"cannot be delivered|can.t be delivered|not deliverable|does not deliver|"
    r"doesn.t deliver|no featured offers available", re.I)
BLOCK_PAT = re.compile(
    r"enter the characters you see below|api-services-support@amazon|"
    r"validateCaptcha|automated access to amazon", re.I)
TOKEN_PAT = re.compile(r'id="glowValidationToken"[^>]*value="([^"]+)"')
TOKEN_PAT2 = re.compile(r'value="([^"]+)"[^>]*id="glowValidationToken"')
GROUP_RETRIES = 2      # extra rounds for pincodes that failed (timeout / blocked / wrong location)
MAX_LOCATION_TRIES = 6  # per pincode, how often we try to (re)set the delivery location


# --------------------------------------------------------------------------- helpers
def asin_of(url):
    m = re.search(r"/(?:dp|gp/aw/d|gp/product)/([A-Z0-9]{10})", str(url or ""))
    return m.group(1) if m else None


def money(text):
    """'₹19,990.00' -> 19990 ; None if no number."""
    m = re.search(r"(\d[\d,]*)(?:\.\d+)?", text or "")
    return int(m.group(1).replace(",", "")) if m else None


def clean(node):
    return re.sub(r"\s+", " ", node.text_content()).strip()


def max_rupee(text):
    vals = [int(v.replace(",", "")) for v in re.findall(r"₹\s?(\d[\d,]*)", text or "")]
    return max(vals) if vals else None


# --------------------------------------------------------------------------- parser
def parse_product(html, pincode):
    """Extract every field from a mobile product page."""
    d = {"mrp": None, "price": None, "cashback": None, "emi": None, "bank": None,
         "coupon": None, "rating": None, "reviews": None, "in_stock": False,
         "avail": "", "loc_ok": False, "title": "", "bank_card": None, "cashback_card": None,
         "max_offer": None, "max_offer_card": None}
    doc = LH.fromstring(html)

    def first(xp):
        r = doc.xpath(xp)
        return r[0] if r else None

    def text_of(xp):
        n = first(xp)
        return clean(n) if n is not None else ""

    d["title"] = text_of('//*[@id="title"]')

    # which pincode did Amazon actually render this page for?
    loc = text_of('//*[@id="glow-ingress-single-line"]') or text_of('//*[@id="glow-ingress-line2"]') \
        or text_of('//*[@id="contextualIngressPtLabel_deliveryShortLine"]')
    d["loc_ok"] = pincode in loc
    # "Currently unavailable" pages carry an EMPTY location token - such a page cannot be
    # used to set a pincode, so remember whether this one has a usable token
    d["has_token"] = bool(TOKEN_PAT.search(html) or TOKEN_PAT2.search(html))

    # ---- price / MRP
    price = None
    n = first('//input[@id="items[0.base][customerVisiblePrice][amount]"]/@value')
    if n:
        price = money(n)
    if price is None:
        price = money(text_of('//*[@id="corePriceDisplay_mobile_feature_div"]'
                              '//span[contains(@class,"priceToPay")]//span[@class="a-price-whole"]'))
    if price is None:
        price = money(text_of('//*[@id="corePrice_feature_div"]//span[@class="a-offscreen"]'))
    d["price"] = price
    mrp = money(text_of('(//*[@id="corePriceDisplay_mobile_feature_div"]'
                        '//span[contains(@class,"basisPrice")]//span[@class="a-offscreen"])[1]'))
    d["mrp"] = mrp or price

    # ---- rating / reviews
    rt = text_of('//*[@id="averageCustomerReviews_feature_div"]') or text_of('//*[@id="pqv-ratings"]')
    m = re.search(r"([\d.]+)\s*out of 5", rt)
    if m:
        d["rating"] = float(m.group(1))
    m = re.search(r"\(([\d,]+)\)|([\d,]+)\s+(?:global\s+)?ratings?", rt)
    if m:
        d["reviews"] = int((m.group(1) or m.group(2)).replace(",", ""))

    # ---- "Offers" card strip (cashback / No Cost EMI / EMI bank-discount cards).
    #      For each kind the card with the biggest number wins.
    best_bank, best_cb, best_emi, best_coupon = (0, None), (0, None), 0, 0
    for card in doc.xpath('//*[contains(@class,"smx-feature-holder")]'
                          '//*[contains(@class,"cardification-combo-swatch-card")]'):
        head_n = card.xpath('.//*[contains(@class,"a-truncate-full")]') or \
            card.xpath('.//*[contains(@class,"cardification-combo-card-header")]')
        header = clean(head_n[0]) if head_n else ""
        # a card has one line per saving: e.g. a "Coupon -₹1,500" line AND a bank line
        lines = card.xpath('.//*[contains(@class,"cardification-combo-header-type-")]') or [card]
        for line in lines:
            kind = (re.search(r"header-type-(\w+)", line.get("class") or "") or [None, ""])[1]
            name_n = line.xpath('.//*[contains(@class,"cardification-combo-header-body")]//span')
            name = clean(name_n[0]) if name_n else ""
            amt_n = line.xpath('.//*[contains(@class,"cardification-combo-header-money")]')
            m = re.search(r"₹\s?(\d[\d,]*(?:\.\d+)?)", clean(amt_n[0]) if amt_n else "") \
                or re.search(r"₹\s?(\d[\d,]*(?:\.\d+)?)", clean(line) if line is not card else "")
            amount = float(m.group(1).replace(",", "")) if m else 0
            if kind == "COUPON" or name.lower() == "coupon":
                best_coupon = max(best_coupon, amount)
            elif kind == "CASHBACK" or (not kind and "cashback" in header.lower()):
                if "₹" in name or not name:      # "Plus ₹1,132 cashback" line: bank is on the card
                    mb = re.search(r"[A-Za-z ]*Bank Credit Card|[A-Za-z ]+Credit Card", clean(card))
                    name = mb.group(0).strip() if mb else name
                if PRIME_CASHBACK and "amazon pay icici" in name.lower():
                    # logged-out pages show the 3% non-Prime cashback; Prime members get 5%
                    # of the "Buy for ₹X" amount in the card header
                    base = money(header)
                    if base:
                        amount = max(amount, base * 5 // 100)
                if amount > best_cb[0]:
                    best_cb = (amount, name)
            else:
                if amount > best_bank[0]:
                    best_bank = (amount, name)
                if kind == "NO_COST_EMI" or "no cost emi" in header.lower():
                    mm = re.search(r"x\s*(\d+)\s*m", header, re.I)
                    if mm:
                        best_emi = max(best_emi, int(mm.group(1)))
    num = lambda v: int(v) if float(v).is_integer() else v
    if best_coupon:
        d["coupon"] = num(best_coupon)
    if best_bank[0] or best_cb[0]:
        top = max(best_bank, best_cb, key=lambda x: x[0])
        d["max_offer"], d["max_offer_card"] = num(top[0]), top[1]
    if best_bank[0]:
        d["bank"], d["bank_card"] = num(best_bank[0]), best_bank[1]
    if best_cb[0]:
        d["cashback"], d["cashback_card"] = num(best_cb[0]), best_cb[1]
    if best_emi:
        d["emi"] = f"{best_emi}m"

    # ---- older offer layout (Bank Offer / Cashback / No Cost EMI boxes) - only fills gaps
    offers = {}
    for box in doc.xpath('//*[starts-with(@id,"itembox-")]'):
        offers[box.get("id")[8:].lower()] = clean(box)
    if not offers:  # fallback: any offer card carrying a known heading
        for box in doc.xpath('//*[contains(@class,"offers-items") or contains(@class,"vsx__offers")]'
                             '//*[self::li or self::div][.//h6 or .//*[contains(@class,"a-text-bold")]]'):
            t = clean(box)
            for key in ("bank offer", "cashback", "no cost emi"):
                if t.lower().startswith(key) and key not in offers:
                    offers[key.replace(" ", "")] = t
    for key, t in offers.items():
        if "bank" in key and d["bank"] is None:
            d["bank"] = max_rupee(t)
        elif "cashback" in key and d["cashback"] is None:
            d["cashback"] = max_rupee(t)
        elif "emi" in key and d["emi"] is None:
            mm = re.search(r"(\d+)\s*months?", t, re.I)
            d["emi"] = f"{mm.group(1)}m" if mm else "Yes"
    if d["emi"] is None:
        emi_txt = text_of('//*[@id="inEMI_feature_div"]') + " " + text_of('//*[@id="inemi_feature_div"]')
        if re.search(r"no cost emi", emi_txt, re.I):
            mm = re.search(r"(\d+)\s*months?", emi_txt, re.I)
            d["emi"] = f"{mm.group(1)}m" if mm else "Yes"
        elif re.search(r"No Cost EMI available", html):
            d["emi"] = "Yes"

    if d["max_offer"] is None:      # older layout: still report the highest of the two
        vals = [v for v in (d["bank"], d["cashback"]) if v]
        d["max_offer"] = max(vals) if vals else None

    # ---- coupon
    # only this product's own coupon blocks - sponsored/related products on the page carry
    # their own "Save ₹X with coupon" badges and must not be picked up
    ctext = " ".join(clean(n) for n in doc.xpath(
        '//*[@id="promoPriceBlockMessage_feature_div" or @id="couponsInBuybox_feature_div"'
        ' or @id="couponFeature_feature_div" or @id="vpcButton_feature_div"]'))
    own = doc.xpath('//*[@id="coupons-card-sub-heading-before-apply"]')   # "Save ₹5,500 now."
    m = (re.search(r"(₹\s?[\d,]+|\d+\s?%)", clean(own[0])) if own else None) \
        or re.search(r"(?:apply|save|with)\s+(₹\s?[\d,]+|\d+\s?%)\s*(?:off\s+)?(?:with\s+)?coupon", ctext, re.I) \
        or re.search(r"coupon[^₹%\d]{0,40}(₹\s?[\d,]+|\d+\s?%)", ctext, re.I)
    if m and d["coupon"] is None:
        v = m.group(1).replace(" ", "")
        d["coupon"] = v if v.endswith("%") else money(v)

    # ---- availability
    avail = " ".join(filter(None, [
        text_of('//*[@id="availability"]'),
        text_of('//*[@id="outOfStock"]'),
        text_of('//*[@id="availability_feature_div"]'),
        text_of('//*[@id="mir-layout-DELIVERY_BLOCK"]'),
        text_of('//*[@id="deliveryBlock_feature_div"]'),
    ]))
    d["avail"] = avail[:160]
    can_buy = bool(doc.xpath('//*[@id="add-to-cart-button" or @id="buy-now-button"]'))
    d["in_stock"] = bool(can_buy and price and not OOS_PAT.search(avail))
    return d


# --------------------------------------------------------------------------- network
class Blocked(Exception):
    pass


class NotFound(Exception):
    pass


class Fetcher:
    def __init__(self, concurrency):
        self.sem = asyncio.Semaphore(concurrency)
        self.sessions = {}      # pincode -> AsyncSession
        self.locks = {}         # pincode -> asyncio.Lock (location is set once)
        self.ready = {}         # pincode -> bool
        self.gen = {}           # pincode -> how many times the location was (re)set
        self.tries = {}         # pincode -> location attempts so far
        self.good_url = None    # a product page known to carry a usable location token
        self.requests = 0

    async def close(self):
        await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)

    async def _get(self, session, url):
        last = None
        for attempt in range(RETRIES):
            try:
                async with self.sem:
                    self.requests += 1
                    r = await session.get(url, timeout=TIMEOUT)
                if r.status_code == 404:
                    raise NotFound("page not found")
                if r.status_code == 200 and not BLOCK_PAT.search(r.text[:20000]):
                    return r.text
                last = Blocked(f"HTTP {r.status_code} / robot check")
            except NotFound:
                raise
            except Exception as e:           # timeout, connection reset ...
                last = e
            await asyncio.sleep(1.5 * (attempt + 1) + random.random())
        raise last

    async def _set_location(self, session, pincode, html, referer):
        """Tell Amazon which pincode this session delivers to (same call the site's own
        'Update location' sheet makes)."""
        def dbg(*a):
            if DEBUG:
                print("    [debug]", pincode, *a, flush=True)

        m = TOKEN_PAT.search(html) or TOKEN_PAT2.search(html)
        if not m:
            # this page has no usable token (e.g. a "Currently unavailable" product) ->
            # borrow one from a page that has: a known good product page, then the home page
            for alt in (self.good_url, BASE + "/"):
                if not alt or alt == referer:
                    continue
                try:
                    alt_html = await self._get(session, alt)
                except Exception as e:
                    dbg("token page error:", alt, e)
                    continue
                m = TOKEN_PAT.search(alt_html) or TOKEN_PAT2.search(alt_html)
                if m:
                    html, referer = alt_html, alt
                    break
        page_tok = m.group(1) if m else None
        m = re.search(r"storeId=([a-z_\-]+)", html)
        store = m.group(1) if m else "generic"
        ajax = {"x-requested-with": "XMLHttpRequest", "accept": "text/html,*/*", "referer": referer}
        dbg("page token:", bool(page_tok), "| store:", store)

        # 1) open the location sheet exactly like the mobile page does -> it carries the CSRF token
        tokens = []
        try:
            async with self.sem:
                r = await session.get(
                    f"{BASE}/portal-migration/hz/glow/get-rendered-address-selections"
                    f"?selectedLocationType=&selectedLocationValue=&deviceType=mobile&pageType=DetailAW"
                    f"&actionSource=mobile-web-subnav&storeContext={store}",
                    headers={**ajax, **({"anti-csrftoken-a2z": page_tok} if page_tok else {})},
                    timeout=TIMEOUT)
            for pat in (r'CSRF_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)',
                        r'anti-csrftoken-a2z["\']?\s*[:=,]\s*["\']([^"\']+)',
                        r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)',
                        r'name=["\']anti-csrftoken-a2z["\'][^>]*value=["\']([^"\']+)',
                        r'glowValidationToken["\']?[^>]{0,80}value=["\']([^"\']+)'):
                tokens += [t.replace("&#x2F;", "/").replace("&#x3D;", "=").replace("&#x2B;", "+")
                           for t in re.findall(pat, r.text)]
            dbg("sheet:", r.status_code, "len", len(r.text), "| tokens found:", len(tokens))
            if DEBUG and not tokens:
                dbg("sheet body:", re.sub(r"\s+", " ", r.text)[:600])
        except Exception as e:
            dbg("sheet error:", e)
        if page_tok:
            tokens.append(page_tok)
        tokens = list(dict.fromkeys(tokens))          # unique, keep order

        # 2) post the pincode
        hdr = {**ajax, "content-type": "application/json", "origin": BASE}
        bodies = [
            {"locationType": "LOCATION_INPUT", "zipCode": pincode, "deviceType": "mobile",
             "storeContext": store, "pageType": "DetailAW", "actionSource": "glow"},
            {"locationType": "LOCATION_INPUT", "zipCode": pincode, "deviceType": "mobile",
             "storeContext": "generic", "pageType": "Detail", "actionSource": "glow"},
        ]
        for tok in tokens:
            for body in bodies:
                try:
                    async with self.sem:
                        r = await session.post(
                            f"{BASE}/portal-migration/hz/glow/address-change?actionSource=glow",
                            json=body, headers={**hdr, "anti-csrftoken-a2z": tok}, timeout=TIMEOUT)
                except Exception as e:
                    dbg("post error:", e)
                    continue
                dbg("post:", r.status_code, re.sub(r"\s+", " ", r.text)[:300])
                if r.status_code == 200 and re.search(
                        r'"(isValidAddress|successful|isAddressUpdated)"\s*:\s*(1|true)', r.text):
                    return True
        return False

    async def _session_for(self, pincode, url):
        """Return a session whose delivery location is `pincode` (created lazily, once)."""
        lock = self.locks.setdefault(pincode, asyncio.Lock())
        async with lock:
            if pincode not in self.sessions:
                self.sessions[pincode] = AsyncSession(impersonate=IMPERSONATE, headers=HEADERS,
                                                      max_clients=CONCURRENCY)
                await self._locate(pincode, url)
        return self.sessions[pincode]

    async def _locate(self, pincode, url, html=None):
        """(Re)set the pincode on this pincode's session. Caller holds the pincode lock."""
        if self.tries.get(pincode, 0) >= MAX_LOCATION_TRIES:
            return False
        self.tries[pincode] = self.tries.get(pincode, 0) + 1
        s = self.sessions[pincode]
        ok = False
        try:
            start = self.good_url or url         # prefer a page known to carry a token
            if html is None or start != url:
                html = await self._get(s, start)
            ok = await self._set_location(s, pincode, html, start)
        except NotFound:
            ok = await self._set_location(s, pincode, "", url)   # falls back to the home page
        except Exception as e:
            if DEBUG:
                print("    [debug]", pincode, "locate error:", e, flush=True)
        self.ready[pincode] = ok
        self.gen[pincode] = self.gen.get(pincode, 0) + 1
        if not ok:
            print(f"  ! could not set location {pincode} (try {self.tries[pincode]})", flush=True)
        return ok

    async def warm(self, pincodes, url):
        await asyncio.gather(*(self._session_for(p, url) for _, p in pincodes), return_exceptions=True)

    async def fetch(self, asin, city, pincode):
        url = f"{BASE}/gp/aw/d/{asin}?th=1&psc=1"
        try:
            s = await self._session_for(pincode, url)
            gen = self.gen.get(pincode, 0)
            html = await self._get(s, url)
            d = parse_product(html, pincode)
            if d["has_token"] and not self.good_url:
                self.good_url = url
            if not d["loc_ok"]:
                # page was rendered for another pincode -> set the location again (only one
                # task per pincode does it, the others just re-fetch) and retry once
                async with self.locks[pincode]:
                    if self.gen.get(pincode, 0) == gen:
                        await self._locate(pincode, url, html)
                if self.ready.get(pincode):
                    d = parse_product(await self._get(s, url), pincode)
            d["error"] = None if d["loc_ok"] else "location not applied"
        except NotFound:
            d = {"in_stock": False, "error": None, "loc_ok": True, "not_found": True}
        except Exception as e:
            d = {"in_stock": False, "error": f"{type(e).__name__}: {e}"[:80], "loc_ok": False}
        d["city"], d["pincode"] = city, pincode
        return d


async def fetch_group(f, asin, group):
    """Fetch one group of pincodes in parallel; pincodes that failed are retried."""
    results = list(await asyncio.gather(*(f.fetch(asin, c, p) for c, p in group)))
    for attempt in range(GROUP_RETRIES):
        bad = [i for i, r in enumerate(results) if r["error"]]
        if not bad:
            break
        await asyncio.sleep(2 * (attempt + 1) + random.random())
        again = await asyncio.gather(*(f.fetch(asin, *group[i]) for i in bad))
        for i, r in zip(bad, again):
            results[i] = r
    return results


async def check_product(f, asin):
    """Mumbai -> (Bangalore, Delhi, Hyderabad in parallel) -> (all others in parallel)."""
    tried = []
    fails = lambda: [f"{t['city']} ({t['error']})" for t in tried if t["error"]]
    for group in ([PRIMARY], PRIORITY, REST):
        results = await fetch_group(f, asin, group)
        tried += results
        if any(r.get("not_found") for r in results):
            # the product page itself does not exist any more - same for every pincode
            best = dict(results[0])
            best.update(oos_in=[], failed=[], status="OOS - page not found")
            return best
        for r in results:                      # results keep the group's priority order
            if r["in_stock"] and not r["error"]:
                r["oos_in"] = [t["city"] for t in tried if not t["in_stock"] and not t["error"]]
                r["failed"] = fails()
                r["status"] = "In Stock"
                return r
    best = next((t for t in tried if not t["error"]), tried[0])
    best = dict(best)
    best["oos_in"] = [t["city"] for t in tried if not t["error"]]
    best["failed"] = fails()
    if best["oos_in"]:
        best["status"] = "OOS - all pincodes" if not best["failed"] else "OOS (some pincodes failed)"
    else:
        best["status"] = "ERROR: " + str(tried[0].get("error"))
    return best


# --------------------------------------------------------------------------- excel
FIELD_COLS = {"MRP": "mrp", "Price": "price", "Cashback": "cashback", "No Cost EMI(Max)": "emi",
              "Bank Offer": "bank", "Coupon": "coupon", "Rating": "rating", "Reviews": "reviews",
              "Bank Offer Card": "max_offer_card", "Cashback Card": "cashback_card"}
FIELD_COLS["Bank Offer"] = "max_offer"   # Bank Offer = highest of bank discount and cashback
EXTRA_COLS = ["Bank Offer Card", "Cashback Card", "Status", "City", "Pincode", "OOS In", "Failed Pincodes", "Fetched At"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="excel.xlsx")
    ap.add_argument("--output", default="primary_output.xlsx")
    ap.add_argument("--sheet", default="Amazon")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--limit", type=int, default=0, help="only first N products (for testing)")
    ap.add_argument("--debug", action="store_true", help="print Amazon's replies to the pincode calls")
    a = ap.parse_args()
    global DEBUG
    DEBUG = a.debug

    wb = openpyxl.load_workbook(a.input)
    ws = wb[a.sheet] if a.sheet in wb.sheetnames else wb.active
    head = {str(c.value).strip(): c.column for c in ws[1] if c.value is not None}
    if "Link" not in head:
        sys.exit("No 'Link' column found in row 1")
    col = ws.max_column
    for name in EXTRA_COLS:
        if name not in head:
            col += 1
            ws.cell(1, col, name)
            head[name] = col

    rows = [(r, asin_of(ws.cell(r, head["Link"]).value)) for r in range(2, ws.max_row + 1)]
    rows = [(r, x) for r, x in rows if x]
    if a.limit:
        rows = rows[:a.limit]
    if not rows:
        sys.exit("No Amazon product links found")
    print(f"{len(rows)} products | {len(PINCODES)} pincodes | concurrency {a.concurrency}", flush=True)

    t0 = time.time()
    f = Fetcher(a.concurrency)
    done = 0
    try:
        # Mumbai session first (always needed); the others are created only if something is OOS.
        await f.warm([PRIMARY], f"{BASE}/gp/aw/d/{rows[0][1]}")

        async def work(row, asin):
            nonlocal done
            res = await check_product(f, asin)
            done += 1
            print(f"[{done}/{len(rows)}] {asin} {res['status']:<10} {res.get('city', ''):<10} "
                  f"price={res.get('price')} mrp={res.get('mrp')}"
                  + (f" | OOS in: {', '.join(res['oos_in'])}" if res.get("oos_in") else ""), flush=True)
            return row, res

        results = await asyncio.gather(*(work(r, x) for r, x in rows))
    finally:
        await f.close()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for row, res in results:
        if res["status"].startswith("ERROR"):
            ws.cell(row, head["Status"], res["status"])
            ws.cell(row, head["Fetched At"], now)
            continue                              # keep the old values, never overwrite with blanks
        for name, key in FIELD_COLS.items():
            if name in head:
                ws.cell(row, head[name], res.get(key))
        if res["status"].startswith("OOS") and "Price" in head:
            ws.cell(row, head["Price"], "OOS")     # not available at any pincode / page gone
        ws.cell(row, head["Status"], res["status"])
        ws.cell(row, head["City"], res["city"] if res["status"] == "In Stock" else None)
        ws.cell(row, head["Pincode"], res["pincode"] if res["status"] == "In Stock" else None)
        ws.cell(row, head["OOS In"], ", ".join(res.get("oos_in") or []))
        ws.cell(row, head["Failed Pincodes"], ", ".join(res.get("failed") or []))
        ws.cell(row, head["Fetched At"], now)
    try:
        wb.save(a.output)
        out = a.output
    except PermissionError:                       # file is open in Excel
        out = a.output.replace(".xlsx", f"_{datetime.now():%H%M%S}.xlsx")
        wb.save(out)

    n_ok = sum(r["status"] == "In Stock" for _, r in results)
    n_err = sum(r["status"].startswith("ERROR") for _, r in results)
    print(f"\nDone in {time.time() - t0:.1f}s | {f.requests} page requests | "
          f"in stock {n_ok} | OOS {len(results) - n_ok - n_err} | errors {n_err} | saved -> {out}")


def run_async(coro):
    """Run a coroutine to completion (curl_cffi needs the selector event loop on Windows)."""
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)


async def _fetch_many(asins, concurrency, log):
    f = Fetcher(concurrency)
    out, done = {}, 0
    try:
        await f.warm([PRIMARY], f"{BASE}/gp/aw/d/{asins[0]}")

        async def work(asin):
            nonlocal done
            try:
                res = await check_product(f, asin)
            except Exception as e:                      # never let one product kill the run
                res = {"status": f"ERROR: {type(e).__name__}: {e}"[:120], "in_stock": False,
                       "oos_in": [], "failed": []}
            done += 1
            if log:
                log(f"[Amazon {done}/{len(asins)}] {asin} {res['status']} "
                    f"{res.get('city', '') if res['status'] == 'In Stock' else ''} "
                    f"price={res.get('price')} offer={res.get('max_offer')} coupon={res.get('coupon')}"
                    + (f" | failed: {', '.join(res['failed'])}" if res.get("failed") else ""))
            out[asin] = res

        await asyncio.gather(*(work(a) for a in asins))
    finally:
        await f.close()
    return out


def fetch_many(asins, concurrency=CONCURRENCY, log=print):
    """Fetch many ASINs concurrently. Returns {asin: result}; result['status'] is
    'In Stock', 'OOS - ...' or 'ERROR: ...'."""
    asins = list(dict.fromkeys(a for a in asins if a))
    if not asins:
        return {}
    return run_async(_fetch_many(asins, concurrency, log))


if __name__ == "__main__":
    run_async(main())
