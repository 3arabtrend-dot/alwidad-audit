#!/usr/bin/env python3
"""
Alwidad Perfumes -> Arab Trend audit only.
Reads WooCommerce products in the Alwidad Perfume brand, scrapes source catalog,
matches products, and writes CSV reports. It NEVER modifies products or media.
"""

import csv
import html
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rapidfuzz import fuzz
from playwright.sync_api import sync_playwright

load_dotenv()

TARGET_SITE = os.getenv("TARGET_SITE", "").rstrip("/")
WC_KEY = os.getenv("WC_CONSUMER_KEY", "").strip()
WC_SECRET = os.getenv("WC_CONSUMER_SECRET", "").strip()

SOURCE_CATALOG = "https://alwidadperfumes.com/index.php?route=product/catalog"
BRAND_SEARCH = "Alwidad"
BRAND_SLUG_HINT = "alwidad-perfume"
OUT_DIR = Path("audit_output")

TIMEOUT = 60
SLEEP = 0.15

session = requests.Session()
session.headers.update({
    "User-Agent": "curl/8.5.0",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
})

ARABIC_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")
HTML_TAGS = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)

STOP_WORDS = {
    "al", "the", "perfume", "perfumes", "spray", "eau", "de", "parfum", "edp",
    "ml", "by", "for", "and", "او", "من", "عطر", "عطور", "برفيوم", "مل",
    "الوداد", "widad", "alwidad"
}


def die(message):
    print(f"ERROR: {message}")
    sys.exit(1)


def clean_html(value):
    value = html.unescape(value or "")
    value = HTML_TAGS.sub(" ", value)
    return SPACE_RE.sub(" ", value).strip()


def normalize(value):
    value = clean_html(value).lower().strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = ARABIC_DIACRITICS.sub("", value)
    value = value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    value = value.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    value = NON_WORD_RE.sub(" ", value)
    return SPACE_RE.sub(" ", value).strip()


def token_set(value):
    return {x for x in normalize(value).split() if len(x) > 1 and x not in STOP_WORDS}


def wc_request(path, params=None):
    url = f"{TARGET_SITE}/wp-json/wc/v3/{path.lstrip('/')}"
    params = dict(params or {})
    # Query-string auth behaves better with some WAF configurations.
    params["consumer_key"] = WC_KEY
    params["consumer_secret"] = WC_SECRET
    response = session.get(url, params=params, timeout=TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}")
    return response


def paginate_wc(path, params=None):
    page = 1
    rows = []
    while True:
        query = dict(params or {})
        query.update({"per_page": 100, "page": page})
        response = wc_request(path, query)
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected WooCommerce response for {path}")
        rows.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
        print(f"  Woo page {page}/{total_pages}: {len(batch)}")
        if page >= total_pages or not batch:
            break
        page += 1
        time.sleep(SLEEP)
    return rows


def find_brand():
    candidates = []
    for endpoint in ("products/brands", "products/brand"):
        try:
            records = paginate_wc(endpoint, {"search": BRAND_SEARCH})
        except Exception:
            continue
        for item in records:
            name = normalize(str(item.get("name", "")))
            slug = normalize(str(item.get("slug", "")))
            if "alwidad" in name or "al widad" in name or "alwidad" in slug:
                candidates.append((endpoint, item))
    if candidates:
        return candidates[0]
    return None, None


def product_has_brand(product):
    for key in ("brands", "brand"):
        value = product.get(key)
        if isinstance(value, list):
            for item in value:
                text = normalize(" ".join(str(item.get(k, "")) for k in ("name", "slug")))
                if "alwidad" in text or "al widad" in text:
                    return True
        elif isinstance(value, dict):
            text = normalize(" ".join(str(value.get(k, "")) for k in ("name", "slug")))
            if "alwidad" in text or "al widad" in text:
                return True

    permalink = normalize(product.get("permalink", ""))
    if BRAND_SLUG_HINT.replace("-", " ") in permalink:
        return True
    return False


def get_target_products():
    endpoint, brand = find_brand()

    if brand:
        print(f"Brand found: id={brand.get('id')} name={brand.get('name')} slug={brand.get('slug')}")
        attempts = [
            {"brand": brand.get("id")},
            {"brand": brand.get("slug")},
        ]
        for params in attempts:
            try:
                products = paginate_wc("products", params)
                products = [p for p in products if product_has_brand(p)] or products
                if products:
                    return products, brand
            except Exception as exc:
                print(f"  Brand filter fallback: {exc}")

    print("Brand filter unavailable; fetching products then filtering brand metadata.")
    all_products = paginate_wc("products", {"status": "any"})
    filtered = [p for p in all_products if product_has_brand(p)]
    return filtered, brand


def with_query(url, **updates):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key, value in updates.items():
        query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))



class SourceBrowser:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        self.context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(TIMEOUT * 1000)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def get_html(self, url):
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
        status = response.status if response else 0
        if status >= 400:
            raise RuntimeError(f"Source HTTP {status}: {url}")
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        return self.page.content()


def discover_source_links(browser):
    links = set()
    empty_pages = 0

    for page in range(1, 31):
        url = with_query(SOURCE_CATALOG, page=page)
        soup = BeautifulSoup(browser.get_html(url), "lxml")
        page_links = set()

        for a in soup.select("a[href*='route=product/product']"):
            href = html.unescape(a.get("href", "")).strip()
            if href and "product_id=" in href:
                page_links.add(urljoin(url, href))

        new_links = page_links - links
        links.update(page_links)
        print(f"  Source page {page}: {len(page_links)} links, {len(new_links)} new")

        if not new_links:
            empty_pages += 1
        else:
            empty_pages = 0

        if empty_pages >= 2:
            break
        time.sleep(SLEEP)

    return sorted(links)


def first_attr(soup, selectors, attr):
    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.get(attr):
            return urljoin(SOURCE_CATALOG, html.unescape(node.get(attr)).strip())
    return ""


def scrape_source_product(browser, url):
    soup = BeautifulSoup(browser.get_html(url), "lxml")

    title = ""
    for selector in ("h1", ".product-info h1", "meta[property='og:title']"):
        node = soup.select_one(selector)
        if node:
            title = node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
            if title:
                break

    image = first_attr(soup, [
        "meta[property='og:image']",
    ], "content")

    if not image:
        image = first_attr(soup, [
            ".thumbnails > li:first-child a",
            ".product-info .image a",
        ], "href")

    if not image:
        image = first_attr(soup, [
            "#image",
            ".product-image img",
            ".product-info .image img",
            "img.img-responsive",
        ], "src")

    description = ""
    for selector in ("#tab-description", ".tab-content #description", ".product-description"):
        node = soup.select_one(selector)
        if node:
            description = node.get_text(" ", strip=True)
            if description:
                break

    return {
        "source_title": clean_html(title),
        "source_url": url,
        "source_image": image,
        "source_description": clean_html(description),
    }


def title_score(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0

    scores = [
        fuzz.ratio(na, nb),
        fuzz.token_set_ratio(na, nb),
        fuzz.token_sort_ratio(na, nb),
        fuzz.partial_ratio(na, nb),
    ]

    ta, tb = token_set(a), token_set(b)
    overlap = 0.0
    if ta and tb:
        overlap = 100.0 * len(ta & tb) / max(1, min(len(ta), len(tb)))

    exact_bonus = 8 if na == nb else 0
    containment_bonus = 5 if na in nb or nb in na else 0

    return min(100.0, 0.55 * max(scores) + 0.25 * scores[1] + 0.20 * overlap + exact_bonus + containment_bonus)


def description_score(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return float(fuzz.token_set_ratio(na[:3000], nb[:3000]))


def match_one(target, sources):
    ranked = []
    target_title = target.get("name", "")
    target_desc = " ".join([
        clean_html(target.get("short_description", "")),
        clean_html(target.get("description", "")),
    ])

    for source in sources:
        ts = title_score(target_title, source["source_title"])
        ds = description_score(target_desc, source["source_description"])
        final = 0.88 * ts + 0.12 * ds if ds else ts
        ranked.append((round(final, 2), round(ts, 2), round(ds, 2), source))

    ranked.sort(key=lambda x: x[0], reverse=True)
    best = ranked[0] if ranked else (0, 0, 0, {})
    second = ranked[1] if len(ranked) > 1 else (0, 0, 0, {})

    margin = round(best[0] - second[0], 2)
    if best[0] >= 90 and margin >= 8:
        status = "CONFIDENT"
    elif best[0] >= 80 and margin >= 5:
        status = "LIKELY"
    else:
        status = "REVIEW"

    return best, second, margin, status


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not TARGET_SITE or not WC_KEY or not WC_SECRET:
        die("Missing TARGET_SITE / WC_CONSUMER_KEY / WC_CONSUMER_SECRET in .env")

    OUT_DIR.mkdir(exist_ok=True)

    print("1) Fetching Arab Trend products in Alwidad brand...")
    targets, brand = get_target_products()
    if not targets:
        die("No Arab Trend products were found for Alwidad Perfume.")
    print(f"Target products: {len(targets)}")

    print("\n2) Discovering source catalog products...")
    with SourceBrowser() as browser:
        source_links = discover_source_links(browser)
        if not source_links:
            die("No source product links were discovered.")
        print(f"Source product links: {len(source_links)}")

        print("\n3) Reading source product details...")
        sources = []
        for index, url in enumerate(source_links, 1):
            try:
                item = scrape_source_product(browser, url)
                if item["source_title"]:
                    sources.append(item)
                print(f"  [{index}/{len(source_links)}] {item['source_title'][:70]}")
            except Exception as exc:
                print(f"  [{index}/{len(source_links)}] ERROR {url}: {exc}")
            time.sleep(SLEEP)

    print(f"Source products parsed: {len(sources)}")

    print("\n4) Matching...")
    rows = []
    for target in targets:
        best, second, margin, status = match_one(target, sources)
        current_images = target.get("images") or []
        best_source = best[3]
        second_source = second[3]

        row = {
            "status": status,
            "target_id": target.get("id"),
            "target_title": target.get("name", ""),
            "target_url": target.get("permalink", ""),
            "current_main_image": current_images[0].get("src", "") if current_images else "",
            "current_image_count": len(current_images),
            "best_score": best[0],
            "title_score": best[1],
            "description_score": best[2],
            "margin_vs_second": margin,
            "source_title": best_source.get("source_title", ""),
            "source_url": best_source.get("source_url", ""),
            "source_main_image": best_source.get("source_image", ""),
            "second_score": second[0],
            "second_source_title": second_source.get("source_title", ""),
            "second_source_url": second_source.get("source_url", ""),
        }
        rows.append(row)
        print(f"  {target.get('id')} | {status:9} | {best[0]:6.2f} | {target.get('name')} -> {best_source.get('source_title', '')}")

    fields = list(rows[0].keys())
    write_csv(OUT_DIR / "alwidad_audit_all.csv", rows, fields)
    write_csv(OUT_DIR / "alwidad_audit_review.csv",
              [r for r in rows if r["status"] == "REVIEW"], fields)

    counts = {name: sum(1 for r in rows if r["status"] == name)
              for name in ("CONFIDENT", "LIKELY", "REVIEW")}

    summary = [
        f"Target products: {len(targets)}",
        f"Source products: {len(sources)}",
        f"CONFIDENT: {counts['CONFIDENT']}",
        f"LIKELY: {counts['LIKELY']}",
        f"REVIEW: {counts['REVIEW']}",
        "",
        "AUDIT ONLY: no product or media was modified.",
    ]
    (OUT_DIR / "summary.txt").write_text("\n".join(summary), encoding="utf-8")

    print("\n" + "=" * 60)
    print("\n".join(summary))
    print(f"\nFiles: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
