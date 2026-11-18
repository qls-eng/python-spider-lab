# -*- coding: utf-8 -*-
"""
静态网页爬虫（图书列表页）：请求 → 解析 → 持久化

版本说明：
    本文件为「步骤 4：面向对象重构」后的版本（R7，2026-11-18）。
    重构只改变代码组织方式，不改变任何对外行为：
      - 抓取字段与列顺序不变（title, price, rating, link）
      - 请求间隔、UA 池、超时重试策略不变
      - 控制台打印内容、CSV 编码（utf-8-sig）、行数均不变
    验证方式：同一站点、同一页数运行，result.csv 与重构前逐行比对一致。
"""

import random
import time
import logging
from urllib.parse import urljoin

import pandas as pd
import requests
from requests.exceptions import HTTPError, RequestException   # 显式导入，避免 NameError
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class NewsCrawler:
    """图书列表页爬虫：把请求、解析、持久化三段逻辑收敛到一个类中。

    参数:
        list_url_template: 列表页 URL 模板，用 {page} 占位页码
        max_pages:        抓取页数上限
        request_interval: 相邻两次请求的最小间隔（秒）
        timeout:          单次请求超时（秒）
        max_retries:      超时 / 连接错误的最大重试次数
        output_csv:       输出 CSV 路径
        ua_pool:          User-Agent 池，None 时使用内置池
        columns:          CSV 列顺序
    """

    DEFAULT_LIST_URL_TEMPLATE = "http://books.toscrape.com/catalogue/page-{page}.html"
    DEFAULT_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
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

    def __init__(
        self,
        list_url_template: str = DEFAULT_LIST_URL_TEMPLATE,
        max_pages: int = 3,
        request_interval: float = 2.0,
        timeout: int = 10,
        max_retries: int = 3,
        output_csv: str = "result.csv",
        ua_pool: list[str] | None = None,
        columns: tuple[str, ...] = ("title", "price", "rating", "link"),
    ) -> None:
        self.list_url_template = list_url_template
        self.max_pages = max_pages
        self.request_interval = request_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.output_csv = output_csv
        self.columns = columns
        self.ua_pool = ua_pool or self.UA_POOL
        self._last_request_at = 0.0          # 上一次请求的发起时刻，用于节流
        self._session: requests.Session | None = None

    # ---------- 会话管理 ----------

    def __enter__(self) -> "NewsCrawler":
        self._session = self._create_session()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """关闭底层连接池。"""
        if self._session is not None:
            self._session.close()
            self._session = None

    def _create_session(self) -> requests.Session:
        """创建带 User-Agent 伪装的 Session，复用 TCP 连接。"""
        session = requests.Session()
        headers = dict(self.DEFAULT_HEADERS)
        headers["User-Agent"] = random.choice(self.ua_pool)
        session.headers.update(headers)
        return session

    @property
    def session(self) -> requests.Session:
        """惰性创建会话，允许不使用 with 语句也能直接调用。"""
        if self._session is None:
            self._session = self._create_session()
        return self._session

    # ---------- 请求层 ----------

    def _throttle(self) -> None:
        """保证相邻两次请求的发起时间间隔 >= request_interval 秒。"""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def fetch_html(self, url: str) -> str | None:
        """发送 GET 请求并返回页面 HTML；失败返回 None。

        超时 / 连接异常最多重试 max_retries 次并按次数递增退避；
        HTTP 错误码与其它请求异常不重试，直接返回 None。
        """
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()

                # 编码兜底：未声明或声明为 iso-8859-1 时改用推断编码
                if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = resp.apparent_encoding

                logging.info("请求成功 [%d/%d] %s (status=%s, len=%d)",
                             attempt, self.max_retries, url, resp.status_code, len(resp.text))
                return resp.text

            except (requests.Timeout, requests.ConnectionError) as exc:
                logging.warning("请求失败 [%d/%d] %s -> %s: %s",
                                attempt, self.max_retries, url, type(exc).__name__, exc)
                if attempt < self.max_retries:
                    time.sleep(self.request_interval * attempt)

            except HTTPError as exc:
                logging.error("HTTP 状态码异常 %s -> %s", url, exc)
                return None

            except RequestException as exc:
                logging.error("未知请求异常 %s -> %s", url, exc)
                return None

        logging.error("重试 %d 次后仍失败，放弃该页：%s", self.max_retries, url)
        return None

    def fetch_list_pages(self):
        """逐页抓取列表页，生成 (page, url, html)；html 为 None 表示该页失败。"""
        for page in range(1, self.max_pages + 1):
            url = self.list_url_template.format(page=page)
            yield page, url, self.fetch_html(url)

    # ---------- 解析层 ----------

    @staticmethod
    def parse_books(html: str, base_url: str) -> list[dict[str, str]]:
        """解析单个列表页，返回书籍信息列表（字段：title / price / rating / link）。"""
        soup = BeautifulSoup(html, "html.parser")
        books: list[dict[str, str]] = []

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

    # ---------- 持久化层 ----------

    def save_csv(self, books: list[dict[str, str]]) -> pd.DataFrame:
        """汇总数据写入 CSV（utf-8-sig，Excel 直接打开不乱码）。"""
        df = pd.DataFrame(books, columns=list(self.columns))
        df.to_csv(self.output_csv, index=False, encoding="utf-8-sig")
        return df

    # ---------- 编排 ----------

    def run(self) -> pd.DataFrame:
        """执行完整抓取流程，返回写入后的 DataFrame。"""
        all_books: list[dict[str, str]] = []
        total_books = 0

        try:
            for page, url, html in self.fetch_list_pages():
                if html is None:
                    logging.warning("第 %d 页跳过：%s", page, url)
                    continue

                books = self.parse_books(html, url)
                total_books += len(books)
                all_books.extend(books)

                print(f"\n===== 第 {page} 页（共 {len(books)} 条），展示前 3 条 =====")
                for idx, book in enumerate(books[:3], 1):
                    print(f"  [{idx}] 书名: {book['title']}")
                    print(f"      价格: {book['price']}")
                    print(f"      星级: {book['rating']}")
                    print(f"      链接: {book['link']}")
        finally:
            self.close()

        df = self.save_csv(all_books)

        print(f"\n共抓取图书：{total_books} 条")
        print(f"已写入 {self.output_csv}（utf-8-sig），共 {len(df)} 行")
        return df


def main() -> None:
    """命令行入口：可用 --pages 指定页数（默认 3 页演示）。"""
    import argparse

    parser = argparse.ArgumentParser(description="图书列表页爬虫（教程练习站点）")
    parser.add_argument("--pages", type=int, default=3, help="抓取页数，默认 3")
    parser.add_argument("--out", default="result.csv", help="输出 CSV 路径，默认 result.csv")
    args = parser.parse_args()

    with NewsCrawler(max_pages=args.pages, output_csv=args.out) as crawler:
        crawler.run()


if __name__ == "__main__":
    main()
