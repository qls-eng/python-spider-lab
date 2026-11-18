# fetcher.py
# 请求获取 HTML（原逻辑不变） + BeautifulSoup 解析（原逻辑不变） + pandas 持久化（新增）
import time
import random
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import pandas as pd                                  # 新增

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ============ 可配置参数 ============
LIST_URL_TEMPLATE = "http://books.toscrape.com/catalogue/page-{page}.html"
MAX_PAGES = 3                # 演示用，books.toscrape.com 实际共 50 页
REQUEST_INTERVAL = 2.0       # 请求间隔下限（秒）
TIMEOUT = 10                 # 单次请求超时（秒）
MAX_RETRIES = 3              # 网络超时 / 连接错误的最大重试次数

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# 记录上一次请求的发起时刻，用于全局节流
_last_request_at = 0.0


def _throttle():
    """保证相邻两次请求的发起时间间隔 >= REQUEST_INTERVAL 秒。"""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def create_session():
    """创建带 User-Agent 伪装的 Session，复用 TCP 连接。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def fetch_html(session, url, timeout=TIMEOUT, max_retries=MAX_RETRIES):
    """
    发送 GET 请求并返回页面 HTML 文本。
    超时 / 连接异常时最多重试 max_retries 次；HTTP 错误码和最终失败返回 None。
    """
    for attempt in range(1, max_retries + 1):
        _throttle()
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()

            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding

            logging.info("请求成功 [%d/%d] %s (status=%s, len=%d)",
                         attempt, max_retries, url, resp.status_code, len(resp.text))
            return resp.text

        except (requests.Timeout, requests.ConnectionError) as exc:
            logging.warning("请求失败 [%d/%d] %s -> %s: %s",
                            attempt, max_retries, url, type(exc).__name__, exc)
            if attempt < max_retries:
                time.sleep(REQUEST_INTERVAL * attempt)

        except requests.HTTPError as exc:
            logging.error("HTTP 状态码异常 %s -> %s", url, exc)
            return None

        except requests.RequestException as exc:
            logging.error("未知请求异常 %s -> %s", url, exc)
            return None

    logging.error("重试 %d 次后仍失败，放弃该页：%s", max_retries, url)
    return None


def fetch_list_pages(page_range=range(1, MAX_PAGES + 1)):
    """
    依次抓取列表页 HTML，以生成器方式逐页返回 (page, url, html)。
    html 为 None 表示该页抓取失败，由调用方决定是否跳过。
    """
    session = create_session()
    try:
        for page in page_range:
            url = LIST_URL_TEMPLATE.format(page=page)
            html = fetch_html(session, url)
            yield page, url, html
    finally:
        session.close()


def parse_books(html, base_url):
    """
    解析单个列表页 HTML，返回书籍信息列表。
    每条形如：{'title': ..., 'price': ..., 'rating': ..., 'link': ...}
    """
    soup = BeautifulSoup(html, "html.parser")
    books = []

    for pod in soup.select("article.product_pod"):
        a = pod.select_one("h3 > a")
        title = a.get("title", "").strip() if a else ""
        href = a.get("href", "") if a else ""
        link = urljoin(base_url, href) if href else ""

        price_el = pod.select_one("p.price_color")
        price = price_el.get_text(strip=True) if price_el else ""

        rating_el = pod.select_one("p.star-rating")
        rating = ""
        if rating_el:
            classes = rating_el.get("class", [])
            if len(classes) > 1:
                rating = classes[1]

        books.append({
            "title": title,
            "price": price,
            "rating": rating,
            "link": link,
        })

    return books


if __name__ == "__main__":
    all_books = []                                   # 新增：汇总容器
    total_books = 0

    for page, url, html in fetch_list_pages():
        if html is None:
            logging.warning("第 %d 页跳过：%s", page, url)
            continue

        books = parse_books(html, url)
        total_books += len(books)
        all_books.extend(books)                      # 新增：逐页累加

        # 原有打印：每页前 3 条，保持不变
        print(f"\n===== 第 {page} 页（共 {len(books)} 条），展示前 3 条 =====")
        for idx, book in enumerate(books[:3], 1):
            print(f"  [{idx}] 书名: {book['title']}")
            print(f"      价格: {book['price']}")
            print(f"      星级: {book['rating']}")
            print(f"      链接: {book['link']}")

    # ========== 新增：pandas 持久化 ==========
    df = pd.DataFrame(all_books, columns=["title", "price", "rating", "link"])
    df.to_csv("result.csv", index=False, encoding="utf-8-sig")

    print(f"\n共抓取图书：{total_books} 条")
    print(f"已写入 result.csv（utf-8-sig），共 {len(df)} 行")