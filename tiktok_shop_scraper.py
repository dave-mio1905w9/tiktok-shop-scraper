#!/usr/bin/env python3
"""
TikTok Shop scraper. Pulls product listings from search/category pages
without using the official API. Outputs JSON or CSV.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HAS_LXML = False
try:
    from lxml import html as lhtml
    HAS_LXML = True
except ImportError:
    pass

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

PROXY_LIST = []

def _load_proxies(path=None):
    global PROXY_LIST
    if PROXY_LIST:
        return PROXY_LIST
    env = os.environ.get('TIKTOK_PROXIES')
    if env:
        PROXY_LIST = [p.strip() for p in env.split(',') if p.strip()]
        return PROXY_LIST
    p = Path(path) if path else Path.home() / '.config' / 'tiktok_shop_scraper' / 'proxies.txt'
    if p.exists():
        PROXY_LIST = [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith('#')]
    return PROXY_LIST

def _pick_proxy():
    proxies = _load_proxies()
    if proxies:
        return random.choice(proxies)
    return None

def _build_opener(proxy=None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    opener = urllib.request.build_opener(*handlers)
    return opener

def _headers():
    uas = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    ]
    return {
        'User-Agent': random.choice(uas),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br' if HAS_BROTLI else 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    }

def _extract_embedded_json(text: str):
    patterns = [
        r'window\._SSR_HYDRATED_DATA_=\s*({.*?});',
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'<script[^>]*>.*?window\.__INIT_DATA__\s*=\s*({.*?});.*?</script>',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None

def _parse_products_from_html(text: str):
    products = []
    if not HAS_LXML:
        return products
    tree = lhtml.fromstring(text)
    selectors = [
        '//div[@data-testid="product-card"]',
        '//div[contains(@class, "product-card")]',
        '//div[contains(@class, "ProductCard")]',
        '//a[contains(@href, "/product/")]/ancestor::div[3]',
    ]
    items = []
    for sel in selectors:
        items = tree.xpath(sel)
        if items:
            break
    for item in items:
        try:
            title_el = item.xpath('.//span[contains(@class, "title") or contains(@class, "product-title")]')
            title = title_el[0].text_content().strip() if title_el else None
            price_el = item.xpath('.//span[contains(text(), "$") or contains(@class, "price")]')
            price = price_el[0].text_content().strip() if price_el else None
            link_el = item.xpath('.//a[contains(@href, "/product/")]/@href')
            link = link_el[0] if link_el else None
            if title:
                products.append({
                    'title': title,
                    'price': price,
                    'link': link,
                })
        except Exception:
            continue
    return products

def _parse_products(data):
    products = []
    if not isinstance(data, dict):
        return products
    for key in ('products', 'items', 'productList', 'searchResults', 'data'):
        if key in data and isinstance(data[key], list):
            for item in data[key]:
                if isinstance(item, dict):
                    products.append(item)
    if not products:
        for v in data.values():
            if isinstance(v, dict):
                products.extend(_parse_products(v))
            elif isinstance(v, list):
                for el in v:
                    if isinstance(el, dict):
                        products.extend(_parse_products(el))
    return products

def _normalize_product(raw: dict):
    out = {
        'id': raw.get('productId') or raw.get('id') or raw.get('itemId'),
        'title': raw.get('title') or raw.get('productTitle') or raw.get('name'),
        'price': None,
        'original_price': None,
        'currency': None,
        'seller': raw.get('seller') or raw.get('shopName') or raw.get('merchantName'),
        'rating': raw.get('rating') or raw.get('avgRating'),
        'sales': raw.get('sales') or raw.get('soldCount') or raw.get('sold'),
        'url': raw.get('url') or raw.get('productUrl') or raw.get('shareUrl'),
        'image': None,
    }
    price_info = raw.get('price') or raw.get('priceInfo') or raw.get('minPrice')
    if isinstance(price_info, dict):
        out['price'] = price_info.get('salePrice') or price_info.get('price') or price_info.get('value')
        out['original_price'] = price_info.get('originalPrice') or price_info.get('marketPrice')
        out['currency'] = price_info.get('currency') or price_info.get('currencyCode')
    elif isinstance(price_info, (int, float, str)):
        out['price'] = price_info
    img = raw.get('image') or raw.get('coverImage') or raw.get('mainImage') or raw.get('imgUrl')
    if isinstance(img, dict):
        out['image'] = img.get('url') or img.get('uri')
    elif isinstance(img, str):
        out['image'] = img
    return out

def fetch_page(url: str, proxy=None, retries=3):
    opener = _build_opener(proxy)
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=_headers(), method='GET')
        try:
            with opener.open(req, timeout=30) as resp:
                raw = resp.read()
                enc = resp.headers.get('Content-Encoding', '')
                if 'br' in enc and HAS_BROTLI:
                    raw = brotli.decompress(raw)
                elif 'gzip' in enc:
                    import gzip
                    raw = gzip.decompress(raw)
                text = raw.decode('utf-8', errors='replace')
                return text
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 503, 502):
                time.sleep(2 ** attempt + random.random())
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(1 + random.random())
    raise last_err

def scrape_search(query: str, pages=1, proxy=None):
    all_products = []
    base = 'https://shop.tiktok.com/search'
    for page in range(1, pages + 1):
        url = f"{base}?q={urllib.parse.quote(query)}&page={page}"
        text = fetch_page(url, proxy=proxy)
        data = _extract_embedded_json(text)
        if data is None:
            prods = _parse_products_from_html(text)
            if not prods:
                print(f"warning: no data found on page {page}", file=sys.stderr)
                break
            all_products.extend(prods)
        else:
            prods = _parse_products(data)
            if not prods:
                print(f"warning: no products on page {page}", file=sys.stderr)
                break
            all_products.extend([_normalize_product(p) for p in prods])
        time.sleep(random.uniform(1.5, 3.0))
    return all_products

def scrape_category(category_id: str, pages=1, proxy=None):
    all_products = []
    base = 'https://shop.tiktok.com/category'
    for page in range(1, pages + 1):
        url = f"{base}/{category_id}?page={page}"
        text = fetch_page(url, proxy=proxy)
        data = _extract_embedded_json(text)
        if data is None:
            prods = _parse_products_from_html(text)
            if not prods:
                print(f"warning: no data found on page {page}", file=sys.stderr)
                break
            all_products.extend(prods)
        else:
            prods = _parse_products(data)
            if not prods:
                print(f"warning: no products on page {page}", file=sys.stderr)
                break
            all_products.extend([_normalize_product(p) for p in prods])
        time.sleep(random.uniform(1.5, 3.0))
    return all_products

def write_json(products, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(products, f, indent=2, ensure_ascii=False)

def write_csv(products, path):
    if not products:
        return
    keys = list(products[0].keys())
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(products)

def main():
    parser = argparse.ArgumentParser(
        description='Scrape TikTok Shop product listings.',
        usage='python tiktok_shop_scraper.py --query "wireless earbuds" --output products.json'
    )
    parser.add_argument('--query', help='Search query')
    parser.add_argument('--category', help='Category ID to scrape')
    parser.add_argument('--pages', type=int, default=1, help='Number of pages to scrape')
    parser.add_argument('--output', '-o', required=True, help='Output file (json or csv)')
    parser.add_argument('--proxy', help='HTTP proxy URL')
    parser.add_argument('--proxy-file', dest='proxy_file', help='File containing proxy list (one per line)')
    args = parser.parse_args()

    if args.proxy_file:
        _load_proxies(args.proxy_file)

    if not args.query and not args.category:
        print("error: specify --query or --category", file=sys.stderr)
        sys.exit(2)

    if args.query and args.category:
        print("error: use --query or --category, not both", file=sys.stderr)
        sys.exit(2)

    proxy = args.proxy or _pick_proxy()

    if args.query:
        products = scrape_search(args.query, pages=args.pages, proxy=proxy)
    else:
        products = scrape_category(args.category, pages=args.pages, proxy=proxy)

    if not products:
        print("no products found", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.output)
    if out_path.suffix.lower() == '.json':
        write_json(products, out_path)
    elif out_path.suffix.lower() == '.csv':
        write_csv(products, out_path)
    else:
        print("output must be .json or .csv", file=sys.stderr)
        sys.exit(1)
    print(f"wrote {len(products)} products to {out_path}")

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
