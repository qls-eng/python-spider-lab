# Python 静态网页爬虫实验（Vibe Coding）

使用 `requests + BeautifulSoup + pandas` 实现一个静态网页爬虫，按 Vibe Coding 工作流分阶段迭代开发：先请求、再解析、最后持久化，每一阶段单独运行验证，不做一次性生成。

## 目标站点

`http://books.toscrape.com/catalogue/page-{page}.html`

Books to Scrape 是公开的爬虫练习沙盒站点，页面全部为静态 HTML，无 JavaScript 渲染，适合作为静态爬虫实验对象。

字段映射（对应实验要求的标题、时间、作者、链接四个字段）：

| 实验要求字段 | 本项目实际字段 | 来源 |
| --- | --- | --- |
| 标题 | `title`（书名） | `h3 > a` 的 `title` 属性 |
| 时间 | `price`（价格） | `p.price_color` 文本 |
| 作者 | `rating`（星级） | `p.star-rating` 的 class 第二个值 |
| 链接 | `link`（详情页绝对地址） | `h3 > a` 的 `href` + `urljoin` |

说明：真实新闻站点普遍存在反爬策略与版权限制，本实验选用练习沙盒站点，用书名、价格、星级、详情链接完成四字段提取的等效训练。

## 技术栈

- Python 3.9+（开发环境 3.14）
- `requests`：HTTP 请求，Session 复用连接
- `beautifulsoup4`：HTML 解析
- `pandas`：数据汇总与 CSV 导出

## 功能特性

1. UA 池随机切换 User-Agent，降低被识别为脚本请求的概率
2. 请求节流：相邻两次请求发起时间间隔不小于 2 秒
3. 失败重试：超时与连接错误最多重试 3 次，并按次数递增退避
4. 状态码异常不盲目重试：HTTP 错误直接放弃该页
5. 编码修正：非 UTF-8 页面用 `apparent_encoding` 兜底，避免中文乱码
6. 分页抓取：页数由 `MAX_PAGES` 控制，失败页自动跳过不中断
7. 字段缺失兜底：任一字段取不到时填空字符串，程序不崩溃
8. 结果输出：写入 `result.csv`，`utf-8-sig` 编码，Excel 直接打开无乱码

## 项目结构

```
spider/
├── fetcher.py                  # 爬虫主程序（请求 + 解析 + 持久化）
├── result.csv                  # 运行产物：抓取结果
├── requirements.txt            # 依赖清单
├── README.md                   # 项目说明
├── .gitignore                  # 忽略虚拟环境与 IDE 配置
└── docs/
    ├── 需求澄清清单.md          # 结构化 Prompt 与需求边界
    ├── ai对话记录.md            # 分轮次 AI 协作过程记录
    ├── 测试用例.md              # 功能与异常场景验证记录
    └── 复盘.md                  # 人工审查点、风险识别与方案对比
```

## 运行方式

安装依赖：

```bash
pip install -r requirements.txt
```

执行：

```bash
python fetcher.py
```

预期输出：

1. 每页打印前 3 条解析结果（书名、价格、星级、链接）
2. 运行结束打印总抓取条数与写入行数
3. 项目目录生成 `result.csv`

## 可配置参数

在 `fetcher.py` 顶部修改：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `LIST_URL_TEMPLATE` | books.toscrape 列表页模板 | 目标列表页地址模板 |
| `MAX_PAGES` | 3 | 抓取页数 |
| `REQUEST_INTERVAL` | 2.0 | 请求间隔下限（秒） |
| `TIMEOUT` | 10 | 单次请求超时（秒） |
| `MAX_RETRIES` | 3 | 超时与连接错误最大重试次数 |

## 合规说明

- 仅抓取公开的爬虫练习站点，不涉及需要登录、付费或个人隐私的数据
- 请求间隔不低于 2 秒，单次运行页数受控，不对目标站点造成压力
- 未绕过任何访问限制，未使用代理池、验证码识别等手段

## 已知限制

- 仅适用于静态 HTML 页面；Vue、React 等 JavaScript 渲染的页面，`requests` 拿到的是空壳 HTML，需改用浏览器自动化方案
- 解析逻辑依赖站点当前的 DOM 结构，目标站点改版会导致 CSS 选择器失效

进阶方案与对比见 `docs/复盘.md`。
