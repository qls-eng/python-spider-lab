# -*- coding: utf-8 -*-
"""
动态渲染站点抓取：同一份数据的两种抓取方案与耗时对比（R8）。

目标站点：http://quotes.toscrape.com/js/ （Zyte 提供的教学练习站，公开免鉴权）
该站的特点：HTML 里没有引文卡片，只有一段 `var data = [...]` 的 JS 数组，
            浏览器执行前端 JS 后才把数组渲染成 <div class="quote"> 卡片。
            因此 requests + BeautifulSoup 直接解析 DOM 只会得到 0 条——
            这是"静态解析对动态页面失效"的最小可复现例子。

两种方案，抓取字段与输出格式完全一致，便于逐行比对：

    方案 B（数据反查，strategy=inline）
        识别页面内嵌的数据源（var data 数组，等价于定位页面背后的 XHR 接口），
        直接把 JS 数组按 JSON 解析。一次请求、零额外依赖。

    方案 A（无头浏览器渲染，strategy=render）
        启动无头浏览器执行页面 JS，等卡片出现后按 DOM 选择器解析。
        通过浏览器自带的 DevTools 协议（CDP）驱动，不需要下载任何浏览器驱动。

运行：
    python dynamic_crawler.py --strategy inline --pages 2
    python dynamic_crawler.py --strategy render --pages 2
    python dynamic_crawler.py --compare --pages 2
"""

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from bs4 import BeautifulSoup

from fetcher import NewsCrawler

# 输出列：与书籍爬虫的 result.csv 分开存放，避免混淆两份数据
QUOTE_COLUMNS = ("author", "text", "tags", "page")

INLINE_MARKER = "var data = "

# 本机浏览器的常见安装位置（Edge / Chrome）
BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser() -> str:
    """返回第一个存在的本机浏览器路径，找不到时抛 RuntimeError。"""
    for path in BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError("未找到可用的 Edge / Chrome，方案 A 无法执行")


def _free_port() -> int:
    """向系统申请一个空闲端口给 CDP 用，避免与用户已开浏览器冲突。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_json(url: str, timeout: float) -> dict:
    """轮询等待 CDP 的 HTTP 接口可用（浏览器启动需要一点时间）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"无头浏览器启动超时：等待 {url} 失败")


class HeadlessBrowser:
    """通过 DevTools 协议（CDP）驱动本机无头浏览器，用于渲染动态页面。

    为什么不用 Selenium / Playwright：
        Selenium 4 需要 Selenium Manager 联网下载 msedgedriver，本项目环境下载失败；
        Playwright 需要较大轮子 + 浏览器运行时，镜像下载同样不可靠。
        而浏览器自带 --remote-debugging-port，零驱动、零额外二进制即可完成渲染。
    代价：需要 websocket-client（一条 WebSocket 连接）与本机已安装的浏览器。
    """

    def __init__(self, browser_path: str | None = None, page_timeout: float = 45.0) -> None:
        self.browser_path = browser_path or find_browser()
        self.page_timeout = page_timeout
        self._process: subprocess.Popen | None = None
        self._ws = None
        self._profile_dir: str | None = None
        self._msg_id = 0

    # ---------- 生命周期 ----------

    def __enter__(self) -> "HeadlessBrowser":
        import websocket   # websocket-client，仅本方案需要

        port = _free_port()
        self._profile_dir = tempfile.mkdtemp(prefix="cdp-profile-")
        self._process = subprocess.Popen(
            [
                self.browser_path,
                "--headless=new",              # 无窗口渲染
                "--disable-gpu",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self._profile_dir}",   # 独立配置目录，不碰用户配置
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        _wait_json(f"http://127.0.0.1:{port}/json/version", timeout=15)
        targets = _wait_json(f"http://127.0.0.1:{port}/json/list", timeout=10)
        page_target = next(t for t in targets if t.get("type") == "page")
        self._ws = websocket.create_connection(page_target["webSocketDebuggerUrl"],
                                               timeout=self.page_timeout)
        self._request("Page.enable")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    # ---------- CDP 调用 ----------

    def _request(self, method: str, params: dict | None = None) -> dict:
        """发送一条 CDP 命令并等待同 id 的响应，期间收到的事件直接丢弃。"""
        self._msg_id += 1
        msg_id = self._msg_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") != msg_id:
                continue                       # 事件通知，忽略
            if "error" in message:
                raise RuntimeError(f"CDP {method} 失败：{message['error']}")
            return message.get("result", {})

    def _evaluate(self, expression: str):
        """在页面上下文里执行 JS 并取回值。"""
        result = self._request("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        })
        return result.get("result", {}).get("value")

    def render(self, url: str, selector: str = "div.quote") -> str:
        """导航到 url，等选择器命中后返回渲染完成的整页 HTML。"""
        self._request("Page.navigate", {"url": url})

        deadline = time.time() + self.page_timeout
        count = 0
        while time.time() < deadline:
            # 轮询命中数量：既覆盖"页面未加载完"，也覆盖"加载完但 JS 还没渲染完"
            count = self._evaluate(
                f"document.querySelectorAll({json.dumps(selector)}).length"
            ) or 0
            if count > 0:
                break
            time.sleep(0.2)

        if count == 0:
            raise RuntimeError(f"等待 {selector} 超时（{self.page_timeout}s），页面可能未渲染")

        return self._evaluate("document.documentElement.outerHTML")


class DynamicQuoteCrawler(NewsCrawler):
    """引文站爬虫：复用父类的请求层（UA 池 / 节流 / 超时重试），只替换解析层。"""

    DEFAULT_LIST_URL_TEMPLATE = "http://quotes.toscrape.com/js/page/{page}/"

    def __init__(self, **kwargs) -> None:
        # 注意：父类 __init__ 的 list_url_template 默认值在函数定义时已绑定到
        # NewsCrawler.DEFAULT_LIST_URL_TEMPLATE，改子类类属性不会生效。
        # 因此这里必须显式传参，否则会继续抓父类的图书站（本项目踩过一次）。
        kwargs.setdefault("list_url_template", self.DEFAULT_LIST_URL_TEMPLATE)
        kwargs.setdefault("columns", QUOTE_COLUMNS)
        kwargs.setdefault("output_csv", "quotes.csv")
        super().__init__(**kwargs)

    # ---------- 方案 B：内嵌数据反查 ----------

    @staticmethod
    def extract_inline_data(html: str) -> list[dict]:
        """从 HTML 里定位 `var data = [...]` 并按 JSON 解析出原始记录。

        用 JSONDecoder.raw_decode 而不是正则匹配括号：
        数组里含嵌套对象、字符串里含方括号时，正则切分会截错位置，
        raw_decode 从起始 '[' 解析到匹配结尾，天然处理嵌套。
        """
        marker_at = html.find(INLINE_MARKER)
        if marker_at == -1:
            raise ValueError("页面中未找到内嵌数据标记 var data，站点结构可能已变更")

        array_at = html.find("[", marker_at)
        if array_at == -1:
            raise ValueError("内嵌数据标记后未找到数组起始符 '['")

        payload, _end = json.JSONDecoder().raw_decode(html[array_at:])
        if not isinstance(payload, list):
            raise ValueError("内嵌数据结构不是数组，解析假设不成立")
        return payload

    @staticmethod
    def normalize_inline(payload: list[dict], page: int) -> list[dict]:
        """把原始 JSON 记录映射成统一字段（author / text / tags / page）。"""
        rows = []
        for item in payload:
            author = item.get("author") or {}
            tags = item.get("tags") or []
            rows.append({
                "author": str(author.get("name", "")).strip(),
                "text": str(item.get("text", "")).strip(),
                "tags": "|".join(str(tag).strip() for tag in tags),
                "page": page,
            })
        return rows

    def crawl_inline(self) -> list[dict]:
        """方案 B 主流程：逐页取 HTML → 解析内嵌数组 → 归一化字段。"""
        rows: list[dict] = []
        for page in range(1, self.max_pages + 1):
            url = self.list_url_template.format(page=page)
            html = self.fetch_html(url)
            if html is None:
                logging.warning("第 %d 页跳过：%s", page, url)
                continue
            payload = self.extract_inline_data(html)
            page_rows = self.normalize_inline(payload, page)
            rows.extend(page_rows)
            print(f"[inline] 第 {page} 页解析出 {len(page_rows)} 条")
        return rows

    # ---------- 方案 A：无头浏览器渲染 ----------

    @staticmethod
    def parse_rendered_html(html: str, page: int) -> list[dict]:
        """渲染后的 HTML 走选择器解析——与静态页解析同一套选择器。

        单独抽成静态方法，一是便于脱离浏览器做单元测试，
        二是保证两方案解析出的字段口径一致。
        """
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for quote in soup.select("div.quote"):
            text_el = quote.select_one("span.text")
            author_el = quote.select_one("small.author")
            tag_els = quote.select("div.tags a.tag")
            rows.append({
                "author": author_el.get_text(strip=True) if author_el else "",
                "text": text_el.get_text(strip=True) if text_el else "",
                "tags": "|".join(tag.get_text(strip=True) for tag in tag_els),
                "page": page,
            })
        return rows

    def crawl_render(self) -> list[dict]:
        """方案 A 主流程：一个浏览器实例渲染多页，每页渲染后交给统一解析函数。

        每页允许一次重试：首个页面要等 DNS / TLS / 外部 JS 加载，
        网络抖动时容易第一次超时，重试一次即可正常渲染。
        """
        rows: list[dict] = []
        with HeadlessBrowser() as browser:
            for page in range(1, self.max_pages + 1):
                url = self.list_url_template.format(page=page)
                html = None
                for attempt in (1, 2):
                    try:
                        html = browser.render(url)
                        break
                    except RuntimeError as exc:
                        logging.warning("第 %d 页渲染失败（第 %d 次）：%s -> %s",
                                        page, attempt, url, exc)
                if html is None:
                    logging.error("第 %d 页连续两次渲染失败，跳过：%s", page, url)
                    continue
                page_rows = self.parse_rendered_html(html, page)
                rows.extend(page_rows)
                print(f"[render] 第 {page} 页渲染后解析出 {len(page_rows)} 条")
        return rows

    # ---------- 对比 ----------

    def compare(self) -> None:
        """依次跑两种方案，打印实测耗时 / 条数 / 一致性 / 环境成本。"""
        results: dict[str, dict] = {}

        start = time.perf_counter()
        inline_rows = self.crawl_inline()
        results["B 内嵌数据反查"] = {
            "rows": inline_rows,
            "seconds": time.perf_counter() - start,
            "extra_dep": "无（仅 requests + BeautifulSoup）",
            "setup": "无",
        }

        start = time.perf_counter()
        try:
            render_rows = self.crawl_render()
            results["A 无头浏览器渲染"] = {
                "rows": render_rows,
                "seconds": time.perf_counter() - start,
                "extra_dep": "websocket-client + 本机浏览器",
            "setup": "无需下载驱动；需本机已装 Edge/Chrome",
            }
        except RuntimeError as exc:
            results["A 无头浏览器渲染"] = {
                "rows": None,
                "seconds": time.perf_counter() - start,
                "extra_dep": "websocket-client + 本机浏览器",
                "setup": "未执行：" + str(exc),
            }

        print("\n" + "=" * 92)
        print(f"两种方案实测对比（同一站点、{self.max_pages} 页、同样字段）")
        print("=" * 92)
        print(f"{'方案':<16}{'耗时(s)':<10}{'条数':<8}{'额外依赖':<34}{'环境准备'}")
        for name, info in results.items():
            count = "未执行" if info["rows"] is None else str(len(info["rows"]))
            print(f"{name:<16}{info['seconds']:<10.2f}{count:<8}{info['extra_dep']:<34}{info['setup']}")

        inline_rows = results["B 内嵌数据反查"]["rows"]
        render_rows = results["A 无头浏览器渲染"]["rows"]
        if render_rows is None:
            print("\n一致性核对：方案 A 未执行，仅校验方案 B 自身的字段完整率。")
            empty = sum(1 for row in inline_rows if not row["author"] or not row["text"])
            print(f"  方案 B 共 {len(inline_rows)} 条，作者或正文为空的记录 {empty} 条")
            return

        # 逐行比对：作者、正文、标签、页码四项都要一致
        fields = ("author", "text", "tags", "page")
        mismatches = sum(
            1 for a, b in zip(inline_rows, render_rows)
            if any(str(a[f]) != str(b[f]) for f in fields)
        )
        same = mismatches == 0 and len(inline_rows) == len(render_rows)
        print(f"\n一致性核对：条数 B={len(inline_rows)} / A={len(render_rows)}，"
              f"逐行字段（作者/正文/标签/页码）{'完全一致' if same else f'有 {mismatches} 行不一致'}")

    # ---------- 入库 ----------

    def save_quotes(self, rows: list[dict]) -> None:
        """复用父类 save_csv 写盘，保证 utf-8-sig 与列顺序一致。"""
        self.save_csv(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="动态渲染站点抓取（引文练习站）")
    parser.add_argument("--strategy", choices=("inline", "render"), default="inline",
                        help="inline=内嵌数据反查（方案 B），render=无头渲染（方案 A）")
    parser.add_argument("--compare", action="store_true", help="依次跑两种方案并打印实测对比")
    parser.add_argument("--pages", type=int, default=2, help="抓取页数，默认 2")
    parser.add_argument("--out", default="quotes.csv", help="输出 CSV 路径，默认 quotes.csv")
    args = parser.parse_args()

    crawler = DynamicQuoteCrawler(max_pages=args.pages, output_csv=args.out,
                                  request_interval=1.0)
    try:
        with crawler:
            if args.compare:
                crawler.compare()
                return 0

            if args.strategy == "inline":
                rows = crawler.crawl_inline()
            else:
                rows = crawler.crawl_render()

            if not rows:
                print("未抓到任何记录，请检查站点可达性或解析规则。", file=sys.stderr)
                return 1
            crawler.save_quotes(rows)
            print(f"\n共抓取 {len(rows)} 条，已写入 {args.out}（utf-8-sig）")
            return 0
    except RuntimeError as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
