# AI 协作过程记录

项目：Python 静态网页爬虫实验（Vibe Coding）
协作工具：AI 编程助手（DeepSeek 网页版）
开发方式：结构化 Prompt 驱动 + 分阶段生成 + 每轮运行验证后迭代，全程未一次性生成完整爬虫

---

## 第 1 轮　阶段一：请求层，只获取 HTML

### 【我的 Prompt】

```
你是 Python 后端工程师。我需要编写一个静态网页爬虫。
目标：抓取【目标网站 URL】的列表页，提取标题、时间、作者、链接。
约束：使用 requests 和 BeautifulSoup；必须设置 User-Agent 伪装；
请求间隔不低于 2 秒；遇到网络超时需重试 3 次。
验收：输出 result.csv 文件，并在终端打印成功抓取条数。
现在只生成发送请求并获取 HTML 的代码，不要解析、不要写入 csv。
```

### 【AI 输出】

生成 `fetcher.py` 第一阶段版本，只负责发请求、返回 HTML 文本，不含解析与存储。主要结构：

- UA 池 `UA_POOL`：4 条不同浏览器 UA，`create_session()` 中随机取一条
- `_throttle()`：记录上次请求时间，不足 2 秒则 `sleep` 补足，保证间隔达标
- `fetch_html()`：`Session.get` + `raise_for_status`，对 `Timeout`、`ConnectionError` 最多重试 3 次并按次数递增退避；`HTTPError` 与其它 `RequestException` 直接返回 `None`，不盲目重试
- 编码兜底：`encoding` 为空或为 `iso-8859-1` 时改用 `apparent_encoding`
- `fetch_list_pages()`：生成器，逐页 `yield (page, url, html)`，失败页返回 `None` 由调用方跳过

关键片段：

```python
def _throttle():
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def fetch_html(session, url, timeout=TIMEOUT, max_retries=MAX_RETRIES):
    for attempt in range(1, max_retries + 1):
        _throttle()
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < max_retries:
                time.sleep(REQUEST_INTERVAL * attempt)
        except requests.HTTPError:
            return None
```

### 【运行 / 审查发现问题】

1. PyCharm 静态检查报红：代码里直接使用 `HTTPError`、`RequestException`，但没有从 `requests.exceptions` 导入，运行会抛 `NameError`。
2. 首轮未配置真实目标地址，无法验证请求是否真的成功。

### 【迭代修改动作】

1. 追加一条修复 Prompt，要求"只补导入，不新增解析、不写 CSV"，AI 返回补上 `from requests.exceptions import HTTPError, RequestException` 的完整代码。
2. 将 `LIST_URL_TEMPLATE` 改为 `http://books.toscrape.com/catalogue/page-{page}.html`，`MAX_PAGES` 设为 5 后运行。
3. 运行结果：5 页全部返回，日志形如 `请求成功 [1/3] ... status=200, len=...`，无异常退出。请求层验收通过。

---

## 第 2 轮　阶段二：新增解析，只打印前 3 条

### 【我的 Prompt】

```
基于当前 fetcher.py，新增 BeautifulSoup 解析代码。
目标网站：http://books.toscrape.com/catalogue/page-{page}.html
需要提取：书名（标题）、价格、星级（替代作者）、图书详情链接。
约束：不要删除原有 fetcher 请求逻辑，不要写入 CSV 文件。
验收：拿到 HTML 后解析，只在终端打印每一页的前 3 条解析结果。
```

### 【AI 输出】

在保留全部请求逻辑的前提下新增 `parse_books()`，并在主流程加打印：

- 卡片定位：`soup.select("article.product_pod")` 取出当前页所有图书卡片
- 书名：`h3 > a` 的 `title` 属性，取完整书名；缺失时 `""`
- 链接：`h3 > a` 的 `href` 经 `urljoin(base_url, href)` 转绝对地址
- 价格：`p.price_color` 的文本
- 星级：`p.star-rating` 的 `class` 列表第二值（如 `Three`）
- 主流程：每页只打印 `books[:3]`，末尾统计总解析条数，不写文件

关键片段：

```python
def parse_books(html, base_url):
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
        books.append({"title": title, "price": price,
                      "rating": rating, "link": link})
    return books
```

### 【运行 / 审查发现问题】

1. 运行成功，3 页均正常解析，每页输出前 3 条，字段与页面人工核对一致。
2. 程序正常结束，未生成任何文件，符合"本阶段禁止写 CSV"的约束。
3. 观察到：价格带 `£` 前缀、星级为英文单词（`Three`、`One`），属原样保留，未做清洗，留待后续按需处理。

### 【迭代修改动作】

确认解析稳定后进入下一阶段：只新增持久化能力，不改动请求与解析函数。

---

## 第 3 轮　阶段三：新增持久化，输出 result.csv

### 【我的 Prompt】

```
在当前 fetcher.py 基础上，新增 pandas 持久化存储逻辑。
约束：原有请求、解析代码全部保留，不要改动。
要求：所有解析出来的图书数据汇总，写入 result.csv，编码 utf-8-sig；
终端依旧保留每页前 3 条打印，最后输出总抓取条数。
```

### 【AI 输出】

新增 `import pandas as pd`，主流程增加汇总容器与写盘逻辑，五个已有函数（`_throttle`、`create_session`、`fetch_html`、`fetch_list_pages`、`parse_books`）一字未改：

```python
all_books = []
for page, url, html in fetch_list_pages():
    if html is None:
        continue
    books = parse_books(html, url)
    all_books.extend(books)
    # 保留每页前 3 条打印 ...

df = pd.DataFrame(all_books, columns=["title", "price", "rating", "link"])
df.to_csv("result.csv", index=False, encoding="utf-8-sig")
print(f"共抓取图书：{total_books} 条")
print(f"已写入 result.csv（utf-8-sig），共 {len(df)} 行")
```

要点：`index=False` 去掉行号列；`utf-8-sig` 带 BOM，Excel 直接双击打开中文不乱码。

### 【运行 / 审查发现问题】

1. 运行成功：3 页共抓取 60 条，控制台保留每页前 3 条预览。
2. 生成 `result.csv`，共 61 行（1 行表头 + 60 行数据），列为 `title, price, rating, link`，Excel 打开无乱码。
3. 抽查首页数据与站点页面一致，字段无错列、无空行。

### 【迭代修改动作】

核心链路（请求 → 解析 → 持久化）全部验证通过，不再改动业务代码。转入交付整理：补 README、需求澄清清单、测试用例、复盘文档，并将工程提交至 GitHub。

---

## 三轮迭代的整体控制点

1. 每轮 Prompt 只声明"本轮新增什么"，并显式要求"不改动已验证代码"，避免 AI 顺手重构导致回归。
2. 每轮都以运行结果为验收门槛，未通过不进下一轮；第 1 轮因导入缺失先行修复再验证。
3. 阶段二刻意要求"只打印前 3 条、不写 CSV"，把"解析是否正确"与"文件是否正确"两个问题分开定位。
4. 阶段三要求保留原有打印，形成"控制台预览 + 文件落盘"双通道，便于人工比对。

---

## 第 4 轮（R7）　实验后补做：步骤 4 面向对象重构（2026-11-18）

### 【背景说明】

指导书步骤 4 明确要求"将 AI 生成的全部代码喂回给 AI，指出潜在性能瓶颈或安全隐患，重构为面向对象的类结构（如 NewsCrawler 类），保持功能不变"，并对比重构前后。

前三轮（R1~R3）完成的是请求、解析、持久化三段主链路，代码为模块级函数式结构，**该步骤在实验进行时未执行**。本轮为实验结束后对照指导书补做，时间与性质如实记录，不修改前三轮的任何内容。

### 【我的 Prompt】

```
请审查当前 fetcher.py，指出潜在的性能瓶颈或安全隐患，
并把全部代码重构为面向对象的类结构（类名 NewsCrawler），保持功能不变。

约束：
1. 不改抓取字段、不改 CSV 列顺序与编码、不改控制台打印内容；
2. 请求间隔 ≥2 秒、UA 池、超时重试 3 次的策略原样保留；
3. 重构后必须能跑出与重构前逐行一致的 result.csv。

验收：同一站点、同一页数运行，CSV 逐行比对一致。
```

### 【AI 审查意见】

| 类别 | 问题 | 建议 |
| --- | --- | --- |
| 性能 | 每页重新建 `Session`，TCP 连接不复用 | 用上下文管理持有连接池，一次抓取共用一个 Session |
| 隐患 | 模块级全局变量 `_last_request_at` 由 `global` 维护，多实例会互相串扰 | 收进实例属性，节流状态跟随实例 |
| 隐患 | `HTTPError` / `RequestException` 依赖隐式引用，曾导致 `NameError` | 重构时显式 `from requests.exceptions import ...` |
| 健壮性 | 页面级失败只打日志，无统一汇总 | 由 `run()` 统一编排，失败页明确跳过 |
| 可读性 | 五个函数 + 主流程平铺，职责边界不清晰 | 按"会话 / 请求 / 解析 / 持久化 / 编排"分层 |

### 【AI 输出】

`fetcher.py` 重构为 `NewsCrawler` 类，方法划分如下：

- 会话层：`__enter__` / `__exit__` / `close` / `_create_session` / `session`（惰性创建）
- 请求层：`_throttle` / `fetch_html` / `fetch_list_pages`
- 解析层：`parse_books`（`staticmethod`，与实例状态无关）
- 持久化层：`save_csv`
- 编排层：`run`

配置项改为构造参数（`max_pages`、`request_interval`、`timeout`、`max_retries`、`output_csv`、`columns`），命令行入口新增 `--pages` 与 `--out`：

```python
with NewsCrawler(max_pages=args.pages, output_csv=args.out) as crawler:
    crawler.run()
```

### 【运行 / 审查发现问题】

1. **重复关闭**：`run()` 的 `finally` 里关闭会话，`with` 退出时 `__exit__` 又关闭一次。`close()` 做了幂等处理（关闭后置 `None`），因此不报错，但属于设计冗余，已在代码注释中说明由上下文管理负责收尾。
2. **回归验证**：重构前先备份 `result.csv` 为 `result_before_refactor.csv`，重构后以 `--pages 3` 重跑，实测：

```
共抓取图书：60 条
已写入 result.csv（utf-8-sig），共 60 行

before rows: 61 | after rows: 61
identical: True
```

逐行（含表头）比对完全一致，字段、顺序、编码、打印内容均未变化。

### 【迭代修改动作】

业务逻辑未再改动；补充 README 的类结构说明，并保留本节作为步骤 4 的过程证据。

### 【本轮判定】

步骤 4 完成，行为零变更，回归验证通过。
