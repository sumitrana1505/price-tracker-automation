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
from typing import Optional, Tuple, List, Dict, Any

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
    """High-reliability scraper for Amazon India product pages"""

    MOBILE_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en-GB;q=0.9,en;q=0.8',
    }

    DESKTOP_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-IN,en;q=0.9,hi;q=0.8',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    def __init__(self, pincode: str = DEFAULT_PINCODE):
        self.pincode = pincode
        if curl_requests:
            self.session = curl_requests.Session(impersonate="safari15_5")
        else:
            self.session = requests.Session()
            self.session.headers.update(self.MOBILE_HEADERS)

    def _set_pincode(self, soup: BeautifulSoup, referer_url: str, pincode: str) -> bool:
        """Sets delivery pincode on Amazon"""
        token_el = soup.find(id='glowValidationToken')
        token = token_el.get('value', '') if token_el else ''

        post_data = {
            'locationType': 'LOCATION_INPUT',
            'zipCode': pincode,
            'storeContext': 'generic',
            'deviceType': 'mobile',
            'pageType': 'Detail',
            'actionSource': 'glow',
        }
        post_headers = {
            'User-Agent': self.MOBILE_HEADERS['User-Agent'],
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Referer': referer_url,
            'x-requested-with': 'XMLHttpRequest',
            'anti-csrftoken-a2z': token,
        }
        try:
            url = 'https://www.amazon.in/portal-migration/hz/glow/address-change?actionSource=glow'
            res = self.session.post(url, data=post_data, headers=post_headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                return data.get('successful') == 1 or data.get('isAddressUpdated') == 1
        except Exception:
            pass
        return False

    def scrape(self, url: str, model: str = "") -> ProductScrapedData:
        data = ProductScrapedData(model=model, channel="Amazon", url=url)
        
        # Jitter delay
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if curl_requests:
                    resp = self.session.get(url, headers=self.MOBILE_HEADERS, timeout=REQUEST_TIMEOUT, impersonate="safari15_5")
                else:
                    resp = self.session.get(url, headers=self.MOBILE_HEADERS, timeout=REQUEST_TIMEOUT)

                if resp.status_code == 200:
                    if "captcha" in resp.text.lower() or "Type the characters" in resp.text:
                        logger.warning(f"[Amazon] CAPTCHA detected on attempt {attempt}")
                        time.sleep(3)
                        continue
                    break
                elif resp.status_code == 404:
                    data.in_stock = False
                    data.error_msg = "Product 404 Not Found"
                    return data
                elif resp.status_code in (429, 503):
                    time.sleep(attempt * 2)
            except Exception as e:
                logger.warning(f"[Amazon] Attempt {attempt} failed: {e}")
                time.sleep(attempt * 1.5)

        if not resp or resp.status_code != 200:
            data.in_stock = False
            data.error_msg = f"HTTP {resp.status_code if resp else 'No response'}"
            return data

        soup = BeautifulSoup(resp.text, 'html.parser')

        # 1. Extract Price
        data.price = self._extract_price(soup)

        # 2. Extract Bank Offers / Cashback
        data.bank_offer = self._extract_bank_offers(soup, url)

        # 3. Extract Coupon
        data.coupon = self._extract_coupon(soup)

        # 4. Extract Rating & Review Count
        self._extract_rating_and_reviews(soup, resp.text, data)

        # Check if item is out of stock or missing price
        is_oos = self._is_out_of_stock(soup)
        if data.price is None or is_oos:
            logger.info(f"  [Amazon] Initial check: {'OOS' if is_oos else 'No price'}. Checking alternate pincodes...")
            found_stock = False

            for pin in AMAZON_PINCODES:
                logger.info(f"    -> Switching to Pincode {pin}...")
                changed = self._set_pincode(soup, url, pin)
                if not changed:
                    continue
                time.sleep(1.0)
                try:
                    if curl_requests:
                        resp_retry = self.session.get(url, headers=self.MOBILE_HEADERS, timeout=REQUEST_TIMEOUT, impersonate="safari15_5")
                    else:
                        resp_retry = self.session.get(url, headers=self.MOBILE_HEADERS, timeout=REQUEST_TIMEOUT)

                    if resp_retry.status_code == 200:
                        soup_retry = BeautifulSoup(resp_retry.text, 'html.parser')
                        p_retry = self._extract_price(soup_retry)
                        oos_retry = self._is_out_of_stock(soup_retry)

                        if p_retry and not oos_retry:
                            logger.info(f"    [Amazon] IN STOCK found at Pincode {pin}! Price: Rs.{p_retry}")
                            data.price = p_retry
                            data.in_stock = True
                            data.bank_offer = self._extract_bank_offers(soup_retry, url)
                            data.coupon = self._extract_coupon(soup_retry)
                            # Update rating if retry has it
                            self._extract_rating_and_reviews(soup_retry, resp_retry.text, data)
                            found_stock = True
                            break
                except Exception as e:
                    logger.debug(f"    Error checking pincode {pin}: {e}")

            if not found_stock:
                logger.info(f"  [Amazon] Out of stock across all {len(AMAZON_PINCODES)} pincodes. Marked as 'OOS'.")
                data.in_stock = False
                data.price = "OOS"
                data.bank_offer = None
                data.coupon = 0

        return data

    def _is_out_of_stock(self, soup: BeautifulSoup) -> bool:
        """Detects whether product is explicitly marked as unavailable/out of stock"""
        avail_el = soup.find(id='availability')
        if avail_el:
            txt = avail_el.get_text().lower()
            if any(term in txt for term in ['currently unavailable', 'out of stock', "don't know when", 'unavailable']):
                return True
        return False

    def _extract_price(self, soup: BeautifulSoup) -> Optional[int]:
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
            m = re.search(r'₹\s*([\d,]+(?:\.\d+)?)', cp.get_text())
            if m:
                return clean_digits(m.group(1))

        ptp = soup.find(class_='priceToPay')
        if ptp:
            whole = ptp.find(class_='a-price-whole')
            if whole:
                return clean_digits(whole.get_text(strip=True))

        return None

    def _extract_bank_offers(self, soup: BeautifulSoup, url: str) -> Optional[int]:
        max_offer = 0

        # Mobile carousel cards
        cards = soup.find_all(class_='cardification-combo-swatch-card')
        for card in cards:
            card_text = card.get_text(separator=' | ', strip=True)
            money_div = card.find(class_='cardification-combo-header-money')
            if money_div:
                amt = clean_digits(money_div.get_text())
                if amt and amt > max_offer and amt < 100000:
                    max_offer = amt
            else:
                match = re.search(r'-₹\s*([\d,]+)', card_text)
                if match:
                    amt = clean_digits(match.group(1))
                    if amt and amt > max_offer and amt < 100000:
                        max_offer = amt

        # Secondary view tab savings
        aop_span = soup.find(lambda el: el.has_attr('data-a-secondary-view') and 'smx-AOP-secondary-view-0' in el['data-a-secondary-view'])
        if aop_span:
            try:
                sec_data = json.loads(aop_span['data-a-secondary-view'])
                if 'url' in sec_data:
                    import html
                    sec_url = 'https://www.amazon.in' + html.unescape(sec_data['url'])
                    headers = {
                        'User-Agent': self.MOBILE_HEADERS['User-Agent'],
                        'Accept': 'text/html,*/*',
                        'Referer': url,
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                    sec_res = self.session.get(sec_url, headers=headers, timeout=15)
                    if sec_res.status_code == 200:
                        sec_soup = BeautifulSoup(sec_res.text, 'html.parser')
                        saving_msgs = sec_soup.find_all(class_='aop-payment-tab-saving-message')
                        for sm in saving_msgs:
                            amt = clean_digits(sm.get_text())
                            if amt and amt > max_offer and amt < 100000:
                                max_offer = amt
            except Exception:
                pass

        return max_offer if max_offer > 0 else None

    def _extract_coupon(self, soup: BeautifulSoup) -> int:
        coupon_div = (soup.find(id='couponBadge') or 
                      soup.find(id='couponBadgeVpc') or 
                      soup.find(class_='vpcButton') or 
                      soup.find(id='unclippedCouponSection'))
        if coupon_div:
            amt = clean_digits(coupon_div.get_text())
            if amt and amt < 20000:
                return amt
        return 0

    def _extract_rating_and_reviews(self, soup: BeautifulSoup, page_text: str, data: ProductScrapedData):
        # 1. Rating
        rating_el = soup.select_one('#acrPopover span.a-icon-alt')
        if rating_el:
            data.rating = clean_float(rating_el.get_text())

        if not data.rating:
            for el in soup.find_all(string=re.compile(r'\d\.\d\s+out of 5')):
                m = re.search(r'(\d\.\d)', el)
                if m:
                    data.rating = clean_float(m.group(1))
                    break

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

        # 2. Reviews Count
        rc_el = soup.select_one('#acrCustomerReviewText')
        if rc_el:
            data.rating_count = clean_digits(rc_el.get_text())

        if not data.rating_count:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    obj = json.loads(script.string or "")
                    if isinstance(obj, dict):
                        rc = (obj.get("aggregateRating") or {}).get("ratingCount")
                        if rc:
                            data.rating_count = clean_digits(rc)
                            break
                except Exception:
                    pass

        if not data.rating_count:
            m = re.search(r'([\d,]+)\s+ratings?\b', page_text, re.I)
            if m:
                data.rating_count = clean_digits(m.group(1))
