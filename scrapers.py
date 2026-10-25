"""
Scraper Module for Flipkart and Amazon India Product Pages
Extracts: Price, Bank Offers / Cashback, Coupons, Ratings, and Ratings Count.
"""

import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any, Union

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

import requests

from config import (
    DEFAULT_PINCODE,
    AMAZON_PINCODES,
    REQUEST_TIMEOUT,
    DELAY_MIN,
    DELAY_MAX,
    MAX_RETRIES,
)

logger = logging.getLogger("PriceTrackerScraper")


@dataclass
class ProductScrapedData:
    """Structured data returned for a product page"""
    model: str = ""
    channel: str = ""
    url: str = ""
    in_stock: bool = True
    price: Optional[Any] = None  # int or "OOS"
    bank_offer: Optional[int] = None
    coupon: Optional[int] = 0
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    error_msg: str = ""


# ─────────────────────────────────────────────────────────────
# Helper Utilities
# ─────────────────────────────────────────────────────────────

def clean_digits(val: Any) -> Optional[int]:
    """Extract clean integer from text or number"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(round(val))
    text = str(val).replace("₹", "").replace(",", "").strip()
    match = re.search(r'\d+', text)
    if match:
        try:
            return int(match.group(0))
        except ValueError:
            return None
    return None


def clean_float(val: Any) -> Optional[float]:
    """Extract float from text (e.g. '4.2 out of 5' -> 4.2)"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val).strip()
    match = re.search(r'\d+\.?\d*', text)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def normalize_amazon_url(url: str) -> str:
    """Cleans and canonicalizes Amazon product URLs, repairing malformed hostnames and extracting ASIN"""
    if not url:
        return ""
    raw_url = str(url).strip()
    if raw_url and "://" not in raw_url:
        raw_url = "https://" + raw_url

    # Fix broken hostnames from Excel replacements (e.g. amazon.iNonecer -> amazon.in)
    if "amazon.in" not in raw_url and "amazon" in raw_url:
        raw_url = re.sub(r'https?://(?:www\.)?amazon\.[a-zA-Z0-9_\.-]+', 'https://www.amazon.in', raw_url)

    # Extract canonical product URL if ASIN is present
    asin_m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', raw_url)
    if asin_m:
        return f"https://www.amazon.in/dp/{asin_m.group(1)}?th=1&psc=1"

    return raw_url


# ─────────────────────────────────────────────────────────────
# Flipkart Scraper
# ─────────────────────────────────────────────────────────────

class FlipkartScraper:
    """High-reliability scraper for Flipkart product pages"""

    HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-IN,en;q=0.9',
    }

    def __init__(self, pincode: str = DEFAULT_PINCODE):
        self.pincode = pincode
        if curl_requests:
            self.session = curl_requests.Session(impersonate="chrome120")
        else:
            self.session = requests.Session()
            self.session.headers.update(self.HEADERS)

    def scrape(self, url: str, model: str = "") -> ProductScrapedData:
        data = ProductScrapedData(model=model, channel="Flipkart", url=url)
        
        # Jitter delay before request
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if curl_requests:
                    resp = self.session.get(url, headers=self.HEADERS, timeout=REQUEST_TIMEOUT, impersonate="chrome120")
                else:
                    resp = self.session.get(url, headers=self.HEADERS, timeout=REQUEST_TIMEOUT)
                
                if resp.status_code == 200:
                    break
                elif resp.status_code == 404:
                    data.in_stock = False
                    data.error_msg = "Product 404 Not Found"
                    return data
                elif resp.status_code in (429, 503):
                    time.sleep(attempt * 2)
            except Exception as e:
                logger.warning(f"[Flipkart] Attempt {attempt} failed for {url}: {e}")
                time.sleep(attempt * 1.5)

        if not resp or resp.status_code != 200:
            data.in_stock = False
            data.error_msg = f"HTTP {resp.status_code if resp else 'No response'}"
            return data

        page_text = resp.text

        # Extract __INITIAL_STATE__
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*(?:</script>|\n)', page_text)
        if not m:
            m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', page_text)
            
        if not m:
            logger.warning(f"[Flipkart] No __INITIAL_STATE__ found for {url}")
            data.error_msg = "Initial state not found"
            # Fallback to HTML parsing
            self._parse_flipkart_html_fallback(page_text, data)
            return data

        try:
            state_json = re.sub(r'\bundefined\b', 'null', m.group(1))
            state = json.loads(state_json)
        except Exception as e:
            logger.error(f"[Flipkart] JSON decode error: {e}")
            self._parse_flipkart_html_fallback(page_text, data)
            return data

        # 1. Price
        try:
            p_val = state['multiWidgetState']['pageDataResponse']['pageContext']['fdpEventTracking']['events']['psi']['ppd']['finalPrice']
            data.price = clean_digits(p_val)
        except Exception:
            pass

        slots = state.get('multiWidgetState', {}).get('widgetsData', {}).get('slots', [])
        
        # Check ATLAS_NEP_V2 for price fallback
        if data.price is None:
            for slot in slots:
                w = slot.get('slotData', {}).get('widget', {})
                if w.get('type') == 'ATLAS_NEP_V2':
                    nep_ctx_str = w.get('data', {}).get('nepContext')
                    if nep_ctx_str:
                        try:
                            nep_ctx = json.loads(nep_ctx_str) if isinstance(nep_ctx_str, str) else nep_ctx_str
                            if 'fktp' in nep_ctx and nep_ctx['fktp']:
                                data.price = clean_digits(nep_ctx['fktp'])
                                break
                            elif 'lp' in nep_ctx and nep_ctx['lp']:
                                data.price = clean_digits(nep_ctx['lp'])
                                break
                        except Exception:
                            pass

        # 2. Bank Offers
        max_bank_offer = 0
        for slot in slots:
            w = slot.get('slotData', {}).get('widget', {})
            if w.get('type') == 'ATLAS_NEP_V2':
                offers = w.get('data', {}).get('offers', [])
                for offer in offers:
                    for sum_rc in offer.get('value', {}).get('offerSummariesRC', []):
                        tracking = sum_rc.get('tracking', {})
                        label_key = tracking.get('metaInfoLabelKey')
                        content_type = tracking.get('contentType')
                        content_title = tracking.get('contentTitle')

                        # Target bank offer items
                        if (label_key == 'Bank offers' or 
                            content_type in ['OfferDetails:PBO', 'OfferDetails:Bank offers'] or 
                            content_title == 'Bank offers'):

                            grid = sum_rc.get('value', {}).get('bankOfferGrid', [])
                            for g in grid:
                                g_val = g.get('value', {})
                                d_text = g_val.get('discountedPriceText', '')
                                if 'off' in d_text.lower():
                                    amt = clean_digits(d_text)
                                    if amt and amt > max_bank_offer and amt < 100000:
                                        max_bank_offer = amt

                            meta_val = tracking.get('metaInfoLabelValue', '')
                            if 'off' in meta_val.lower():
                                meta_amt = clean_digits(meta_val)
                                if meta_amt and meta_amt > max_bank_offer and meta_amt < 100000:
                                    max_bank_offer = meta_amt

        if max_bank_offer > 0:
            data.bank_offer = max_bank_offer
        else:
            data.bank_offer = None

        # 3. Rating & Rating Count
        try:
            page_data = state.get("multiWidgetState", {}).get("pageDataResponse", {})
            pr = (page_data.get("pageContext", {})
                           .get("fdpEventTracking", {})
                           .get("events", {})
                           .get("psi", {})
                           .get("pr", {}))
            
            # Overall rating
            for key in ("overallRating", "rating", "averageRating"):
                val = pr.get(key)
                if val is not None:
                    data.rating = clean_float(val)
                    break

            # Ratings count
            for key in ("ratingsCount", "ratingCount"):
                val = pr.get(key)
                if val is not None:
                    data.rating_count = clean_digits(val)
                    break
        except Exception:
            pass

        # Check stock status
        try:
            availability = state.get('multiWidgetState', {}).get('pageDataResponse', {}).get('pageContext', {}).get('trackingDataV2', {}).get('productAvailability')
            if availability and 'out_of_stock' in str(availability).lower():
                data.in_stock = False
        except Exception:
            pass

        return data

    def _parse_flipkart_html_fallback(self, html_text: str, data: ProductScrapedData):
        """HTML regex / BeautifulSoup fallback if JSON is obfuscated"""
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # Price fallback
        for price_el in soup.find_all(class_=re.compile(r'_30jeq3|_16Jk6d|Nx9bqj')):
            txt = price_el.get_text()
            val = clean_digits(txt)
            if val and val > 1000:
                data.price = val
                break

        # Rating fallback
        for rating_el in soup.find_all(class_=re.compile(r'_3LWZlK|XQDdHH')):
            val = clean_float(rating_el.get_text())
            if val and 1.0 <= val <= 5.0:
                data.rating = val
                break

        # Rating count fallback
        rc_match = re.search(r'([\d,]+)\s+Ratings\b', html_text)
        if rc_match:
            data.rating_count = clean_digits(rc_match.group(1))


# ─────────────────────────────────────────────────────────────
# Amazon India Scraper
# ─────────────────────────────────────────────────────────────

class AmazonScraper:
    """High-reliability scraper for Amazon India product pages with dynamic pincode switching"""

    HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-IN,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    }

    MOBILE_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en-GB;q=0.9,en;q=0.8',
    }

    def __init__(self, pincode: str = DEFAULT_PINCODE):
        self.default_pincode = str(pincode or DEFAULT_PINCODE)
        self.current_pincode = None
        self.csrf_token = ""
        self._init_session()

    def _init_session(self):
        """Initializes a browser-impersonated session with default Delhi pincode set"""
        if curl_requests:
            self.session = curl_requests.Session(impersonate="chrome124")
        else:
            self.session = requests.Session()
            self.session.headers.update(self.HEADERS)
        self.current_pincode = None
        self.csrf_token = ""
        self._ensure_pincode(self.default_pincode)

    def _extract_csrf_token(self, soup: BeautifulSoup) -> str:
        """Extracts CSRF / glow validation token from page DOM or embedded scripts"""
        for inp in soup.find_all('input'):
            n = inp.get('name', '').lower()
            i = inp.get('id', '').lower()
            if any(k in n or k in i for k in ['anti-csrftoken-a2z', 'glow-validation-token', 'glowvalidationtoken', 'anti-csrf']):
                val = inp.get('value', '').strip()
                if val:
                    return val
        for s in soup.find_all('script'):
            if s.string:
                m = re.search(r'anti-csrftoken-a2z["\']?\s*[:=]\s*["\']([^"\']+)["\']', s.string)
                if m:
                    return m.group(1)
        return ""

    def _set_pincode(self, soup: Optional[BeautifulSoup], referer_url: str, pincode: str) -> bool:
        """Sets delivery pincode on Amazon via glow endpoint (with token caching for high speed)"""
        if soup and not self.csrf_token:
            self.csrf_token = self._extract_csrf_token(soup)

        # If token was not found on product page, harvest from search page with desktop headers
        if not self.csrf_token:
            try:
                r0 = self.session.get("https://www.amazon.in/s?k=tv", headers=self.HEADERS, timeout=12)
                if r0.status_code == 200:
                    soup0 = BeautifulSoup(r0.text, 'html.parser')
                    self.csrf_token = self._extract_csrf_token(soup0)
            except Exception:
                pass

        token = self.csrf_token

        post_data = {
            'locationType': 'LOCATION_INPUT',
            'zipCode': str(pincode),
            'storeContext': 'generic',
            'deviceType': 'desktop',
            'pageType': 'Detail',
            'actionSource': 'glow',
        }
        post_headers = {
            'User-Agent': self.HEADERS['User-Agent'],
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Referer': referer_url or 'https://www.amazon.in/',
            'x-requested-with': 'XMLHttpRequest',
            'anti-csrftoken-a2z': token,
        }
        try:
            url = 'https://www.amazon.in/portal-migration/hz/glow/address-change?actionSource=glow'
            res = self.session.post(url, data=post_data, headers=post_headers, timeout=12)
            if res.status_code == 200:
                data = res.json()
                if data.get('successful') == 1 or data.get('isAddressUpdated') == 1:
                    self.current_pincode = str(pincode)
                    logger.debug(f"[Amazon] Successfully set delivery pincode to {pincode}")
                    return True
        except Exception as e:
            logger.debug(f"[Amazon] Pincode change failed for {pincode}: {e}")
        return False

    def _ensure_pincode(self, pincode: str) -> bool:
        """Ensures the session has the requested pincode active"""
        if self.current_pincode == str(pincode):
            return True
        return self._set_pincode(None, 'https://www.amazon.in/', str(pincode))

    def _get_current_location_text(self, soup: BeautifulSoup) -> str:
        """Extracts current delivery location label displayed on page"""
        loc_el = soup.select_one(
            '#glow-ingress-line2, .glow-toaster-sub-title, #glow-ingress-block, '
            '#glow-ingress-single-line, #contextFreeAddress, #nav-global-location-slot'
        )
        return loc_el.get_text(' ', strip=True) if loc_el else ""

    def _fetch_page(self, url: str) -> Optional[Any]:
        """Fetches product page with retries and CAPTCHA handling"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if curl_requests:
                    resp = self.session.get(url, headers=self.HEADERS, timeout=REQUEST_TIMEOUT, impersonate="chrome124")
                else:
                    resp = self.session.get(url, headers=self.HEADERS, timeout=REQUEST_TIMEOUT)

                if resp.status_code == 200:
                    if "captcha" in resp.text.lower() or "type the characters" in resp.text.lower():
                        logger.warning(f"[Amazon] CAPTCHA detected on attempt {attempt}")
                        self._init_session()
                        time.sleep(attempt * 2)
                        continue
                    return resp
                elif resp.status_code == 404:
                    return resp
                elif resp.status_code in (429, 503):
                    time.sleep(attempt * 2)
            except Exception as e:
                logger.warning(f"[Amazon] Attempt {attempt} failed: {e}")
                time.sleep(attempt * 1.5)
        return None

    def scrape(self, url: str, model: str = "") -> ProductScrapedData:
        clean_url = normalize_amazon_url(url)
        data = ProductScrapedData(model=model, channel="Amazon", url=clean_url)

        # 1. Start with Default Delhi Pincode (110001)
        if self.current_pincode != self.default_pincode:
            self._ensure_pincode(self.default_pincode)

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        resp = self._fetch_page(clean_url)
        if not resp or resp.status_code != 200:
            if resp and resp.status_code == 404:
                data.in_stock = False
                data.price = "OOS"
                data.error_msg = "Product 404 Not Found"
                return data
            data.in_stock = False
            data.price = "OOS"
            data.error_msg = f"HTTP {resp.status_code if resp else 'No response'} (Blocked/CAPTCHA)"
            return data

        soup = BeautifulSoup(resp.text, 'html.parser')

        # 2. Check current delivery location on page
        # If page does not reflect Delhi delivery, re-apply Delhi pincode and reload
        loc_text = self._get_current_location_text(soup)
        if self.default_pincode not in loc_text and "delhi" not in loc_text.lower():
            logger.info(f"  [Amazon] Initial page location was '{loc_text}'. Setting Delhi ({self.default_pincode})...")
            self._set_pincode(soup, clean_url, self.default_pincode)
            time.sleep(0.4)
            resp_delhi = self._fetch_page(clean_url)
            if resp_delhi and resp_delhi.status_code == 200:
                resp = resp_delhi
                soup = BeautifulSoup(resp.text, 'html.parser')

        # 3. Always extract rating and review count from available page
        self._extract_rating_and_reviews(soup, resp.text, data)

        # 4. Check Pincode 1/20 (Delhi): Is it in stock?
        price_delhi = self._extract_price(soup)
        oos_delhi = self._is_out_of_stock(soup)

        if price_delhi is not None and not oos_delhi:
            logger.info(f"  [Amazon] IN STOCK at Delhi ({self.default_pincode}) [1/20] | Price: Rs.{price_delhi:,}")
            data.price = price_delhi
            data.in_stock = True
            data.bank_offer = self._extract_bank_offers(soup, clean_url, price=data.price)
            data.coupon = self._extract_coupon(soup, price=data.price, page_text=resp.text)
            return data

        # 5. OOS at Delhi -> Check across ALL remaining 19 pincodes (Total 20 pincodes)
        alternate_pins = [p for p in AMAZON_PINCODES if str(p) != str(self.default_pincode)]
        logger.info(
            f"  [Amazon] Unavailable in Delhi ({self.default_pincode}) [1/20]. "
            f"Checking across all {len(alternate_pins)} remaining pincodes (Total: 20)..."
        )
        found_stock = False

        checked_count = 1
        for pin in alternate_pins:
            checked_count += 1
            changed = self._set_pincode(soup, clean_url, str(pin))
            if not changed:
                continue
            time.sleep(0.35)
            resp_retry = self._fetch_page(clean_url)
            if not resp_retry or resp_retry.status_code != 200:
                continue

            soup_retry = BeautifulSoup(resp_retry.text, 'html.parser')
            soup = soup_retry  # update soup for next token
            if not data.rating or not data.rating_count:
                self._extract_rating_and_reviews(soup_retry, resp_retry.text, data)
            p_retry = self._extract_price(soup_retry)
            oos_retry = self._is_out_of_stock(soup_retry)

            if p_retry and not oos_retry:
                logger.info(f"    [Amazon] IN STOCK found at Pincode {pin} ({checked_count}/20 checked)! Price: Rs.{p_retry:,}")
                data.price = p_retry
                data.in_stock = True
                data.bank_offer = self._extract_bank_offers(soup_retry, clean_url, price=p_retry)
                data.coupon = self._extract_coupon(soup_retry, price=p_retry, page_text=resp_retry.text)
                self._extract_rating_and_reviews(soup_retry, resp_retry.text, data)
                found_stock = True
                break
            else:
                logger.debug(f"    [Amazon] Checked {checked_count}/20 (Pincode {pin}): Unavailable/OOS")

        # 6. Only write 'OOS' if all 20 pincodes were checked and product was unavailable across all of them
        if not found_stock:
            logger.info(f"  [Amazon] Confirmed UNAVAILABLE across all {checked_count}/20 checked pincodes. Marked as 'OOS'.")
            data.in_stock = False
            data.price = "OOS"
            data.bank_offer = None
            data.coupon = 0

        # Switch session back to default Delhi pincode for subsequent products
        if self.current_pincode != self.default_pincode:
            self._set_pincode(soup, clean_url, self.default_pincode)

        return data

    def _is_out_of_stock(self, soup: BeautifulSoup) -> bool:
        """Detects whether product is explicitly marked as unavailable/out of stock for current location"""
        # 1. Direct out of stock container
        oos_el = soup.find(id='outOfStock')
        if oos_el:
            txt = oos_el.get_text().lower()
            if any(term in txt for term in ['currently unavailable', 'out of stock', "don't know when", 'temporarily unavailable', 'temporarily out of stock']):
                return True

        # 2. Availability element
        avail_el = soup.find(id='availability')
        if avail_el:
            txt = avail_el.get_text().lower()
            if any(term in txt for term in [
                'currently unavailable', 'out of stock', "don't know when",
                'cannot be dispatched', 'temporarily unavailable', 'temporarily out of stock',
                'cannot be shipped', 'not deliverable'
            ]):
                return True
            if any(term in txt for term in ['in stock', 'usually dispatched', 'only', 'order soon', 'left in stock']):
                return False

        # 3. Delivery block restrictions / location delivery message
        delivery_selectors = [
            '#deliveryBlockMessage',
            '#mir-layout-DELIVERY_BLOCK',
            '.delivery-message',
            '#delivery-block',
            '#desktop_buybox .a-alert-error',
            '#desktop_buybox .a-alert-warning',
            '#buybox .a-alert-error',
            '#buybox .a-alert-warning',
        ]
        for sel in delivery_selectors:
            for alert in soup.select(sel):
                t = alert.get_text().lower()
                if any(term in t for term in [
                    'cannot be dispatched to your selected delivery location',
                    'cannot be delivered',
                    'cannot be shipped',
                    'not deliverable',
                    'select a different delivery location',
                    'choose a different delivery location',
                    'no featured offers available',
                    "can't be delivered",
                    'delivery is not available',
                    'delivery not available',
                ]):
                    return True

        return False

    def _extract_price(self, soup: BeautifulSoup) -> Optional[int]:
        # 1. Standard corePrice containers
        cp = (soup.find(id='corePriceDisplay_mobile_feature_div') or 
              soup.find(id='corePrice_desktop') or 
              soup.find(id='corePriceDisplay_desktop_feature_div'))
        if cp:
            ptp = cp.find(class_='priceToPay') or cp.find(class_='apex-pricetopay-value') or cp
            whole = ptp.find(class_='a-price-whole')
            if whole:
                w_text = whole.get_text(strip=True).replace(',', '').rstrip('.')
                val = clean_digits(w_text)
                if val:
                    return val
            off = ptp.select_one('.a-price .a-offscreen') or cp.select_one('.a-offscreen')
            if off:
                val = clean_digits(off.get_text())
                if val:
                    return val
            m = re.search(r'₹\s*([\d,]+(?:\.\d+)?)', cp.get_text())
            if m:
                return clean_digits(m.group(1))

        # 2. priceToPay anywhere in DOM
        ptp = soup.find(class_='priceToPay')
        if ptp:
            whole = ptp.find(class_='a-price-whole')
            if whole:
                val = clean_digits(whole.get_text(strip=True))
                if val:
                    return val
            off = ptp.select_one('.a-offscreen')
            if off:
                val = clean_digits(off.get_text())
                if val:
                    return val

        # 3. apex desktop / mobile
        apex = soup.find(id='apex_desktop') or soup.find(id='apex_mobile')
        if apex:
            off = apex.select_one('.a-price .a-offscreen')
            if off:
                val = clean_digits(off.get_text())
                if val:
                    return val

        return None

    def _extract_bank_offers(self, soup: BeautifulSoup, url: str = "", price: Optional[int] = None) -> Optional[Union[int, float]]:
        """
        Extracts authentic maximum bank offer discount applicable to this product.
        - Parses the official PDP headline bank offer ('Upto ₹X discount') displayed by Amazon.
        - Parses modal offers with strict per-bank grouping (Axis, ICICI, HDFC, SBI, AllBanks).
        - Discards any offer where product price is less than required minimum purchase value.
        - Excludes No Cost EMI interest savings, cashbacks, coupons, and partner offers.
        """
        # 1. Extract official PDP Headline Bank Offer
        pdp_headline_offer = 0.0
        box = soup.find(id='itembox-InstantBankDiscount')
        if not box:
            for title_el in soup.find_all(class_=re.compile(r'offers-items-title|title')):
                if title_el.get_text().strip().lower() == 'bank offer':
                    box = title_el.find_parent(class_=re.compile(r'offers-items|itembox'))
                    if box:
                        break

        if box:
            card_text = box.get_text(' ', strip=True)
            m_pdp = re.search(r'(?:upto|flat|discount of|off)\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)', card_text, re.I)
            if m_pdp:
                try:
                    val = float(m_pdp.group(1).replace(',', ''))
                    if 0 < val < 100000:
                        pdp_headline_offer = val
                except ValueError:
                    pass

        # 2. Extract detailed vsxOffersSecondaryView modal offers
        modal_offers = []
        if box:
            # Extract ASIN from URL or page
            asin = ""
            if url:
                asin_m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
                if asin_m:
                    asin = asin_m.group(1)
            if not asin:
                asin_inp = soup.find('input', id='ASIN')
                if asin_inp and asin_inp.get('value'):
                    asin = asin_inp.get('value').strip()

            span = box.find(attrs={'data-side-sheet': True})
            if span and asin:
                try:
                    side_sheet_data = json.loads(span['data-side-sheet'])
                    enc_merchant = side_sheet_data.get('encryptedMerchantId', '')
                    buying_idx = side_sheet_data.get('buyingOptionIndex', 0)
                    sr = side_sheet_data.get('sr', '')
                    smid = side_sheet_data.get('smid', '')

                    ajax_url = (
                        f'https://www.amazon.in/gp/product/ajax/vsxOffersSecondaryView?'
                        f'asin={asin}&deviceType=web&offerType=InstantBankDiscount&buyingOptionIndex={buying_idx}&'
                        f'additionalParams=merchantId%3A{enc_merchant}&smid={smid}&encryptedMerchantId={enc_merchant}&'
                        f'sr={sr}&experienceId=vsxOffersSecondaryView&showFeatures=vsxoffers&'
                        f'featureParams=OfferType%3AInstantBankDiscount%2CDeviceType%3Aweb'
                    )
                    headers = dict(self.HEADERS)
                    headers['x-requested-with'] = 'XMLHttpRequest'
                    headers['Referer'] = f'https://www.amazon.in/dp/{asin}'
                    resp = self.session.get(ajax_url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        sub_soup = BeautifulSoup(resp.text, 'html.parser')
                        for el in sub_soup.find_all(['h4', 'div', 'p', 'span']):
                            t = el.get_text(' ', strip=True)
                            if any(k in t.lower() for k in ['instant discount', 'flat inr', 'additional inr', 'additional flat inr', 'discount of inr', 'upto inr']):
                                if len(t) < 300 and not any(t in o for o in modal_offers):
                                    modal_offers.append(t)
                except Exception as e:
                    logger.debug(f"[Amazon] Error fetching bank offer modal: {e}")

        # 3. Calculate per-bank discounts enforcing minimum transaction amounts
        bank_totals = {}
        for o in modal_offers:
            o_clean = ' '.join(o.split())

            # Check minimum purchase requirement
            m_min = re.search(r'(?:min(?:imum)?\s*(?:purchase|trxn)?\s*(?:value|is)?\s*(?:inr|rs\.?|₹)?)\s*([\d,]+(?:\.\d+)?)', o_clean, re.I)
            if m_min and price:
                min_val = float(m_min.group(1).replace(',', ''))
                if price < min_val:
                    # Ineligible: product price does not satisfy minimum order threshold
                    continue

            # Identify target bank
            b_name = 'Other'
            o_lower = o_clean.lower()
            if 'axis' in o_lower:
                b_name = 'Axis'
            elif 'icici' in o_lower:
                b_name = 'ICICI'
            elif 'hdfc' in o_lower:
                b_name = 'HDFC'
            elif 'sbi' in o_lower:
                b_name = 'SBI'
            elif 'all banks' in o_lower or 'all bank' in o_lower:
                b_name = 'AllBanks'

            is_add = bool(re.search(r'additional\s*(?:flat\s*)?inr', o_clean, re.I))
            is_emi = bool(re.search(r'\bemi\b', o_clean, re.I) and not re.search(r'\bnon[- ]?emi\b', o_clean, re.I))

            # Extract discount amount
            amt = 0.0
            m_amt = re.search(r'(?:additional\s*(?:flat\s*)?inr|flat\s*inr|up\s*to\s*inr|discount\s*of\s*inr)\s*([\d,]+(?:\.\d+)?)', o_clean, re.I)
            if m_amt:
                amt = float(m_amt.group(1).replace(',', ''))
            else:
                m_num = re.search(r'inr\s*([\d,]+(?:\.\d+)?)', o_clean, re.I)
                if m_num:
                    amt = float(m_num.group(1).replace(',', ''))

            # Percentage discount (e.g. 10% instant discount up to 1750)
            m_pct = re.search(r'(\d+(?:\.\d+)?)\s*%\s*instant discount', o_clean, re.I)
            if m_pct and price:
                pct_val = (float(m_pct.group(1)) / 100.0) * price
                amt = min(amt, pct_val) if amt > 0 else pct_val

            if amt <= 0:
                continue

            key = (b_name, 'EMI' if is_emi else 'NonEMI')
            if key not in bank_totals:
                bank_totals[key] = {'base': 0.0, 'add': 0.0}

            if is_add:
                bank_totals[key]['add'] = max(bank_totals[key]['add'], amt)
            else:
                bank_totals[key]['base'] = max(bank_totals[key]['base'], amt)

        best_bank_offer = pdp_headline_offer
        for key, vals in bank_totals.items():
            tot = vals['base'] + vals['add']
            # Cap: Bank discount shouldn't exceed 40% of item price or headline+1500
            if tot > best_bank_offer:
                if price is None or tot < (price * 0.40):
                    best_bank_offer = tot

        # 4. Fallback: check mobile swatch cards if no offer found yet
        if best_bank_offer <= 0:
            cards = soup.find_all(class_='cardification-combo-swatch-card')
            for card in cards:
                c_text = card.get_text(' ', strip=True).lower()
                if any(term in c_text for term in ['coupon', 'cashback', 'no cost emi', 'interest savings', 'partner offer', 'gst invoice']):
                    continue
                if 'bank' not in c_text and 'card' not in c_text:
                    continue
                for m in re.findall(r'(?:discount of|upto|flat|off).*?₹\s*([\d,]+(?:\.\d+)?)', c_text, re.IGNORECASE):
                    try:
                        val = float(m.replace(',', ''))
                        if 0 < val < 100000:
                            best_bank_offer = max(best_bank_offer, val)
                    except ValueError:
                        pass

        if best_bank_offer > 0:
            return int(best_bank_offer) if best_bank_offer == int(best_bank_offer) else round(best_bank_offer, 2)

        return None

    def _extract_coupon(self, soup: BeautifulSoup, price: Optional[int] = None, page_text: Optional[str] = None) -> int:
        """
        Extracts authentic coupon discount explicitly applicable to this product.
        - Strictly scoped to current buybox and coupon feature containers.
        - Strictly prevents leakage from sibling product variations (e.g. other TV sizes).
        - Excludes 'You pay' summary labels.
        """
        detected_coupons = []

        target_selectors = [
            '#promoPriceBlockMessage_feature_div',
            '#couponsInBuybox_feature_div',
            '#couponBadge',
            '#couponBadgeVpc',
            '.vpcButton',
            '#unclippedCouponSection',
            '.couponLabelText',
            '#applicable_coupons',
            '#coupons-card-sub-heading-before-apply',
            '#coupons-card-sub-heading-after-apply'
        ]

        candidate_texts = []
        for selector in target_selectors:
            for el in soup.select(selector):
                txt = el.get_text(' ', strip=True)
                if txt:
                    candidate_texts.append(txt)

        for el in soup.find_all('span', id=re.compile(r'^couponText', re.I)):
            txt = el.get_text(' ', strip=True)
            if txt:
                candidate_texts.append(txt)

        for text in candidate_texts:
            t_lower = text.lower()
            if 'coupon' not in t_lower or 'you pay' in t_lower:
                continue

            # Percentage coupon e.g. 'Apply 5% coupon' or 'Save 10% with coupon'
            m_pct = re.search(r'(?:apply|save)?\s*(\d+)\s*%\s*(?:off|with)?\s*coupon', text, re.I)
            if m_pct and price:
                pct = int(m_pct.group(1))
                val = int(round(price * pct / 100.0))
                if 0 < val < price:
                    detected_coupons.append(val)

            # Rupee coupon e.g. 'Apply ₹2250 coupon', 'Save ₹2,000 with coupon'
            m_amt = re.search(r'(?:apply|save)\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)\s*(?:off|with|now)?\s*coupon', text, re.I)
            if m_amt:
                amt_str = m_amt.group(1).split('.')[0]
                val = clean_digits(amt_str)
                if val and (price is None or 0 < val < price):
                    detected_coupons.append(val)

            m_amt2 = re.search(r'coupon\s*(?:for|of|worth)?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)', text, re.I)
            if m_amt2:
                amt_str = m_amt2.group(1).split('.')[0]
                val = clean_digits(amt_str)
                if val and (price is None or 0 < val < price):
                    detected_coupons.append(val)

        if detected_coupons:
            return max(detected_coupons)

        return 0

    def _extract_rating_and_reviews(self, soup: BeautifulSoup, page_text: str, data: ProductScrapedData):
        # 1. Rating
        for sel in [
            '#acrPopover span.a-icon-alt',
            '#acrCustomerReviewLink span.a-icon-alt',
            '[data-hook="rating-out-of-text"]',
            '#pqv-ratings'
        ]:
            el = soup.select_one(sel)
            if el:
                r = clean_float(el.get_text())
                if r and 1.0 <= r <= 5.0:
                    data.rating = r
                    break

        if not data.rating:
            m = re.search(r'([1-5]\.\d)\s+out of\s+5', page_text)
            if m:
                data.rating = clean_float(m.group(1))

        if not data.rating:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    obj = json.loads(script.string or "")
                    if isinstance(obj, dict):
                        rv = (obj.get("aggregateRating") or {}).get("ratingValue")
                        if rv:
                            data.rating = clean_float(rv)
                            break
                except Exception:
                    pass

        # 2. Rating Count / Reviews Count
        # (a) Check data-hook="total-rating-count" (standard across Amazon product reviews header)
        el_hook = soup.select_one('[data-hook="total-rating-count"]')
        if el_hook:
            rc = clean_digits(el_hook.get_text())
            if rc and rc > 0:
                data.rating_count = rc

        # (b) Check #acrCustomerReviewLink (mobile header rating link e.g. "(1,050)")
        if not data.rating_count:
            acr_link = soup.select_one('#acrCustomerReviewLink')
            if acr_link:
                for span in acr_link.find_all('span'):
                    aria = span.get('aria-label', '')
                    if 'review' in aria.lower() or 'rating' in aria.lower():
                        rc = clean_digits(aria)
                        if rc and rc > 0:
                            data.rating_count = rc
                            break
                if not data.rating_count:
                    m = re.search(r'\(([\d,]+)\)', acr_link.get_text())
                    if m:
                        rc = clean_digits(m.group(1))
                        if rc and rc > 0:
                            data.rating_count = rc

        # (c) Check #acrCustomerReviewText (desktop review text)
        if not data.rating_count:
            rc_el = soup.select_one('#acrCustomerReviewText')
            if rc_el:
                rc = clean_digits(rc_el.get_text())
                if rc and rc > 0:
                    data.rating_count = rc

        # (d) Check #pqv-ratings e.g. '4.1 out of 5 stars, 1,318 ratings'
        if not data.rating_count:
            pqv = soup.select_one('#pqv-ratings')
            if pqv:
                m = re.search(r'out of 5 stars,?\s*([\d,]+)\s+ratings?', pqv.get_text(), re.I)
                if m:
                    rc = clean_digits(m.group(1))
                    if rc and rc > 0:
                        data.rating_count = rc

        # (e) Product Details table row: Customer Reviews
        if not data.rating_count:
            for th in soup.find_all(class_=re.compile(r'prodDetSectionEntry')):
                if 'customer reviews' in th.get_text().lower():
                    td = th.find_next_sibling('td')
                    if td:
                        a_tag = td.find('a')
                        if a_tag:
                            rc = clean_digits(a_tag.get_text())
                            if rc and rc > 0:
                                data.rating_count = rc
                                break

        # (f) Regex on raw HTML for global ratings: e.g. '1,318 global ratings'
        if not data.rating_count:
            m = re.search(r'([\d,]+)\s+global\s+ratings?', page_text, re.I)
            if m:
                rc = clean_digits(m.group(1))
                if rc and rc > 0:
                    data.rating_count = rc

        # (g) Regex for aria-label="X,XXX Reviews"
        if not data.rating_count:
            m = re.search(r'aria-label=["\']([\d,]+)\s+Reviews?["\']', page_text, re.I)
            if m:
                rc = clean_digits(m.group(1))
                if rc and rc > 0:
                    data.rating_count = rc
