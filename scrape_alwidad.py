#!/usr/bin/env python3
import csv
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CATALOG_URL = "https://alwidadperfumes.com/index.php?route=product/catalog"
OUT_JSON = Path("source_products.json")
OUT_CSV = Path("source_products.csv")

def with_query(url, **updates):
    p = urlparse(url)
    q = parse_qs(p.query)
    for k, v in updates.items():
        q[k] = [str(v)]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

def clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()

def product_links_from_html(page_html, base_url):
    soup = BeautifulSoup(page_html, "lxml")
    links = set()
    for a in soup.select("a[href*='route=product/product']"):
        href = html.unescape(a.get("href", "")).strip()
        if href and "product_id=" in href:
            links.add(urljoin(base_url, href))
    return links

def parse_product(page_html, url):
    soup = BeautifulSoup(page_html, "lxml")

    title = ""
    for selector in ("h1", ".product-info h1", "meta[property='og:title']"):
        node = soup.select_one(selector)
        if node:
            title = node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
            if title:
                break

    image = ""
    meta = soup.select_one("meta[property='og:image']")
    if meta and meta.get("content"):
        image = urljoin(url, html.unescape(meta["content"]).strip())

    if not image:
        for selector, attr in (
            (".thumbnails > li:first-child a", "href"),
            (".product-info .image a", "href"),
            ("#image", "src"),
            (".product-info .image img", "src"),
            ("img.img-responsive", "src"),
        ):
            node = soup.select_one(selector)
            if node and node.get(attr):
                image = urljoin(url, html.unescape(node[attr]).strip())
                break

    description = ""
    for selector in ("#tab-description", ".tab-content #description", ".product-description"):
        node = soup.select_one(selector)
        if node:
            description = node.get_text(" ", strip=True)
            if description:
                break

    return {
        "title": clean(title),
        "url": url,
        "main_image": image,
        "description": clean(description),
    }

def main():
    products = []
    all_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Muscat",
            viewport={"width": 1440, "height": 1200},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = context.new_page()

        empty_pages = 0
        for page_no in range(1, 31):
            url = with_query(CATALOG_URL, page=page_no)
            response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            status = response.status if response else 0
            print(f"Catalog page {page_no}: HTTP {status}")

            if status == 403:
                page.wait_for_timeout(5000)

            page_html = page.content()
            found = product_links_from_html(page_html, url)
            new_links = found - all_links
            all_links.update(found)
            print(f"  found={len(found)} new={len(new_links)} total={len(all_links)}")

            if not new_links:
                empty_pages += 1
            else:
                empty_pages = 0

            if empty_pages >= 2:
                break

            time.sleep(1)

        if not all_links:
            raise RuntimeError("No product links found. Source may still be blocking GitHub Actions.")

        print(f"Total source links: {len(all_links)}")

        for i, url in enumerate(sorted(all_links), 1):
            response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            status = response.status if response else 0
            if status == 403:
                page.wait_for_timeout(5000)

            item = parse_product(page.content(), url)
            if item["title"]:
                products.append(item)

            print(f"[{i}/{len(all_links)}] HTTP {status} | {item['title'][:80]}")
            time.sleep(0.5)

        browser.close()

    OUT_JSON.write_text(
        json.dumps(products, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["title", "url", "main_image", "description"],
        )
        writer.writeheader()
        writer.writerows(products)

    print(f"Saved {len(products)} products to {OUT_JSON} and {OUT_CSV}")

if __name__ == "__main__":
    main()
