# Adaptive Search

`adaptive-search` 是一个给 Codex 使用的默认外部搜索 Skill：通过本地
Python CLI 直接调用多个搜索供应商，不依赖 MCP，也不依赖常驻服务。

它负责搜索发现、条件式交叉搜索、网页正文提取和上下文压缩。默认不使用
Codex 原生 Web Search；网页抓取优先使用本地 HTTP 下载 + Trafilatura，只有
遇到动态页面或本地提取失败时，才按顺序尝试其他提取器。

## 目录

- `SKILL.md`：Codex Skill 的完整工作流和调用约束。
- `scripts/search.py`：多供应商搜索路由器。
- `scripts/fetch.py`：网页下载、正文提取、相关性压缩和缓存。
- `requirements.txt`：本地正文提取依赖。
- `agents/openai.yaml`：Codex Skill 元数据。
- `config/default-policy.yaml`：当前默认搜索策略，不包含任何密钥。

## 安装

Python 3.11 或更高版本：

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

也可以在虚拟环境中安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

在 Codex 中安装 Skill 时，将本仓库目录复制到：

```text
~/.codex/skills/adaptive-search/
```

如果要关闭 Codex 原生搜索，在 `~/.codex/config.toml` 中设置：

```toml
web_search = "disabled"
```

修改后重启 Codex。

## 密钥配置

密钥只放在用户本机的私有文件或进程环境中，不要提交到 GitHub：

```text
~/.config/searchx/secrets.env
```

支持的变量名：

```text
BRAVE_API_KEY
EXA_API_KEY
TAVILY_API_KEY
BAIDU_QIANFAN_API_KEY 或 BAIDU_API_KEY
NEWS_API_KEY
GITHUB_API_KEY 或 GITHUB_TOKEN
FIRECRAWL_API_KEY
SERPER_API_KEY
SEARXNG_URL（可选）
```

示例文件只写变量名，不写真实值。脚本会跳过未配置的供应商。

## 使用

搜索命令需要同时传入原始任务和搜索词：

```bash
python3 scripts/search.py \
  --task-query "目前最强的国产大模型是哪个" \
  --query "目前最强的国产大模型是哪个" \
  --depth auto
```

抓取选定网页：

```bash
python3 scripts/fetch.py \
  --query "目前最强的国产大模型是哪个" \
  --provider auto \
  --max-pages 3 \
  --max-chars 8000 \
  --context-budget 16000 \
  --url "https://example.com/article"
```

脚本输出 JSON，适合被 Codex Skill 或其他 Agent 调用。

## 默认策略

完整策略见 [`config/default-policy.yaml`](config/default-policy.yaml)。核心规则：

| 深度 | 搜索轮次 | 网页抓取 | 适用场景 |
| --- | ---: | ---: | --- |
| `quick` | 最多 2 个供应商，第二个条件触发 | 通常 0 页，精确措辞最多 1 页 | 简单事实、快速查询 |
| `balanced` | 最多 2 个供应商，证据不足时补充 | 通常最多 2 页 | 普通研究、比较、近期信息 |
| `deep` | 2 个起步，最多 3 个供应商 | 通常 2–3 页，真正比较最多 4 页 | 复杂研究、冲突核验 |

供应商按场景分工：

- 普通搜索：Brave、Exa、Tavily。
- 语义搜索和研究：Exa、Tavily、Brave。
- 中文政策和国内信息：百度、NewsAPI/Tavily、Brave。
- 新闻：NewsAPI/Tavily、Brave、Exa。
- GitHub：GitHub API；不足时再使用 Brave/Exa。
- 明确要求 Google SERP 时才使用 Serper。

搜索历史账本只用于遥测和供应商轮换，不会阻断新搜索；实际限制由当前调用
的深度和轮次控制。

## 网页提取与上下文控制

静态网页的处理路径是：

```text
一次 HTTP 下载 → Trafilatura 正文提取 → 查询相关段落筛选 → 上下文预算限制
```

网页不会把完整 HTML 直接送入模型。若长页面没有关键词命中，抓取器会返回
标题/导语、少量标题和跨正文采样，最多为每页预算的三分之一且不超过 2,400
字符，不会退化为整页返回。

如果页面只返回“加载中...”或“Loading...”等 JavaScript 占位内容，会被标记为
提取失败，不会作为有效证据缓存。`auto` 模式随后才会尝试 Tavily、Exa 或
Firecrawl 等高级提取路径。

搜索结果缓存和网页压缩结果缓存均只保存在本机。当前默认缓存时间为：新闻
5 分钟、中文搜索 2 小时、普通/研究搜索 6 小时、网页压缩结果 6 小时。

## 安全

- 不要把 API key 写进 Skill、配置策略、README、命令行参数或 Git 提交。
- 不要提交 `secrets.env`、缓存数据库、日志和包含真实查询的 benchmark 报告。
- provider 错误和缓存状态会写入 JSON，但不会输出认证头。

## 许可证

本仓库当前未附带额外许可证文件。使用或再发布前，请根据你的需要补充许可证。
