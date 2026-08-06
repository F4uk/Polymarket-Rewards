# Polymarket 自动做市机器人

> **Language / 语言:** [English](README.en.md) · **简体中文**

**官方网站**：[polypulse.wiki](https://polypulse.wiki/)

基于 Polymarket 流动性奖励的自动化做市策略，通过挂单获取奖励并自动管理订单生命周期。

![rewardes](images/rewardes-target.png)

## 快速开始

**无需安装 Python**，下载 **[Releases](../../releases/latest)** 对应平台的压缩包，解压后即可运行。

| 平台 | 下载包 | 内含文件 |
|------|--------|----------|
| Windows | `windows.zip` | `main.exe`、`orderbook.exe`、`.env.example` |
| Linux | `linux.zip` | `main`、`orderbook`、`.env.example` |
| macOS(Intel) | `macos.zip` | `main`、`orderbook`、`.env.example` | 

### 1. 解压

将压缩包解压到任意目录，三个文件放在**同一文件夹**内。程序会在该目录下自动创建 `logs/`、`data/`（SQLite 缓存）。

### 2. 配置

```bash
# 复制配置模板（Windows 可在资源管理器中复制并重命名为 .env）
cp .env.example .env
```

编辑 `.env`，至少填写以下两项：

```bash
POLYMARKET_PRIVATE_KEY=你的私钥
POLYMARKET_PROXY_ADDRESS=你的代理/存款钱包地址
```

其余参数可按策略需要调整，说明见 `.env.example`（中英对照）。

### 3. 启动

**先启动订单簿服务，再启动做市主程序**（两个窗口 / 终端均需保持运行）。

**Windows（命令提示符或 PowerShell）：**

```bat
orderbook.exe
main.exe
```

**Linux / macOS：**

```bash
chmod +x orderbook main    # 首次运行可能需要
./orderbook
./main
```

### 4. 停止

在对应终端按 `Ctrl+C` 即可退出。主程序退出时会尝试取消活跃订单。

---

> 以下为源码安装、自行打包等进阶内容。若已使用上述二进制包，可跳过「环境要求」「安装」章节，直接查阅「配置说明」调参。

## 功能特性

- **自动市场筛选**：扫描所有流动性奖励市场，基于收益比值筛选最优机会
- **订单簿数据服务**：后台定期拉取订单簿并写入本地 SQLite 缓存，主程序无需 Redis
- **CLOB V2 兼容**：使用 `py-clob-client-v2`，支持 POLY_1271 存款钱包（`SIGNATURE_TYPE=3`）
- **智能挂单策略**：在奖励区间边界挂单，最大化奖励获取概率
- **自动订单管理**：
  - 订单成交后自动补单
  - 买单成交后立即对冲卖出
  - 实时调整订单价格以保持在奖励区间边界
- **风险控制**：每市场最大敞口限制，防止过度风险
- **跨平台部署**：支持源码运行，也可打包为 Windows / Linux / macOS 单文件可执行程序

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                   主程序 (main.py / poly-mm)              │
│  - 市场扫描和筛选                                         │
│  - 订单管理（CLOB V2）                                    │
│  - 主循环监控                                             │
└─────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ 市场管理器    │    │ 订单管理器    │    │ 风险管理器    │
│              │    │              │    │              │
│ - 扫描市场    │    │ - 下单/取消   │    │ - 敞口跟踪    │
│ - 筛选机会    │    │ - 补单/对冲   │    │ - 风险限制    │
│ - 计算收益比  │    │ - 价格调整    │    │              │
└──────────────┘    └──────────────┘    └──────────────┘
         │                    │
         ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│     订单簿数据服务 (start_orderbook_service.py /         │
│                      poly-orderbook)                     │
│  - 定期扫描流动性奖励市场                                  │
│  - 通过 HTTP 批量拉取 Polymarket 订单簿                    │
│  - 写入 SQLite 本地缓存                                   │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│   本地缓存 (redis_orderbook_client.py → SQLite)          │
│   默认路径：data/orderbook_cache.sqlite3                  │
└─────────────────────────────────────────────────────────┘
```

## 项目结构

| 文件 | 说明 |
|------|------|
| `main.py` | 做市主程序 |
| `start_orderbook_service.py` | 订单簿数据服务启动脚本 |
| `orderbook_data_service.py` | 订单簿扫描与更新逻辑 |
| `order_manager.py` | 下单、撤单、补单、对冲 |
| `market_manager.py` | 市场扫描与筛选 |
| `market_making_strategy.py` | 做市策略与定价 |
| `api_client.py` | Polymarket REST / CLOB API 客户端 |
| `http_orderbook_client.py` | 通过 HTTP 批量获取订单簿 |
| `redis_orderbook_client.py` | 本地 KV 缓存（SQLite 后端，类名保留兼容） |
| `config.py` | 从 `.env` 加载全部配置 |
| `runtime_paths.py` | 源码 / 打包环境下的路径解析 |
| `.env.example` | 配置模板（中英对照注释） |
| `poly_market_making.spec` | PyInstaller 打包配置 |
| `build.sh` / `build.bat` | Linux/macOS / Windows 打包脚本 |

## 环境要求

- Python 3.10+
- 网络可访问 Polymarket API
- **无需** Redis 或其他外部数据库服务

## 安装

### 1. 克隆项目

```bash
git clone <repository-url>
cd poly_Market-making
```

### 2. 创建虚拟环境

```bash
python3 -m venv venv
source venv/bin/activate   # Linux / macOS
# 或
venv\Scripts\activate      # Windows
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

主要依赖：

- `py-clob-client-v2` — Polymarket CLOB V2 交易 SDK
- `httpx[socks]` — HTTP 客户端（含 SOCKS / HTTP2 支持）
- `python-dotenv` — 读取 `.env` 配置
- `requests` — REST API 请求

### 4. 配置参数

所有配置均集中在 `.env` 文件中，**不再使用** `config.yaml`。复制示例文件并按需修改：

```bash
cp .env.example .env
```

**最小必填项：**

```bash
# 私钥（勿提交到 Git）
POLYMARKET_PRIVATE_KEY=your_private_key_here
# 存款/代理钱包地址（与 polymarket.com/profile 中 proxyWallet 相同）
POLYMARKET_PROXY_ADDRESS=your_proxy_wallet_address_here
```

**常用策略参数：**

```bash
MAX_MARKETS=5
ORDER_SIZE_MULTIPLIER=1.0
MAX_EXPOSURE_PER_MARKET_USDC=20
MIN_REWARD_RATIO=0

# 交易配置（2026 年后新账户通常使用 3）
SIGNATURE_TYPE=3
CHAIN_ID=137

# 本地缓存（SQLite，无需 Redis）
ORDERBOOK_TTL=300
ORDERBOOK_DB_PATH=          # 留空则使用 data/orderbook_cache.sqlite3
```

完整配置项及中英对照说明见 [`.env.example`](.env.example)。参数分为「常调区」和「基本固定区」，方便日常维护。

## 使用方法

### 1. 启动订单簿数据服务（推荐）

订单簿服务负责定期扫描市场并将订单簿写入 SQLite，供主程序读取缓存、减少 API 压力。

**前台运行：**

```bash
python start_orderbook_service.py
```

**后台运行（仅 Linux / macOS）：**

```bash
python start_orderbook_service.py --daemon
```

> Windows 不支持 `--daemon`（依赖 `os.fork`），可使用任务计划程序或 `Start-Process` 等方式后台运行。

### 2. 运行主程序

```bash
python main.py
```

**其他启动方式：**

```bash
python main.py --stop       # 取消所有买单后退出（不进入主循环）
python main.py --daemon     # 后台运行（Linux / macOS）
```

主程序将：

1. 初始化 API 客户端、策略、订单管理等组件
2. 取消现有买单（避免与策略冲突）
3. 扫描并筛选机会市场
4. 为机会市场挂单
5. 进入主循环：检查订单状态、调整价格、定期重新扫描市场

### 3. 优雅关闭

按 `Ctrl+C` 或发送 `SIGTERM` 信号，程序将：

- 取消所有活跃订单
- 显示最终统计信息
- 优雅退出

## 打包为可执行文件

项目支持使用 PyInstaller 打包为单文件可执行程序，便于在无 Python 环境的机器上部署。

### 构建

在**目标平台**上分别执行（PyInstaller 不支持交叉编译）：

```bash
# Linux / macOS
bash build.sh

# Windows
build.bat
```

构建完成后，`dist/` 目录下会生成两个可执行文件：

| 构建产物 | 对应脚本 | 说明 |
|---------|---------|------|
| `poly-orderbook` | `start_orderbook_service.py` | 订单簿数据服务 |
| `poly-mm` | `main.py` | 做市主程序 |

### 发布与运行

对外分发时，建议重命名并打 zip 包（与「快速开始」一致）：

| 平台 | 压缩包 | 重命名 |
|------|--------|--------|
| Windows | `windows.zip` | `poly-orderbook.exe` → `orderbook.exe`，`poly-mm.exe` → `main.exe` |
| Linux | `linux.zip` | `poly-orderbook` → `orderbook`，`poly-mm` → `main` |
| macOS | `macos.zip` | 同上 |

每个 zip 内包含：**订单簿可执行文件 + 主程序可执行文件 + `.env.example`**。用户解压后复制为 `.env` 并填写私钥即可运行。

本地测试步骤：

1. 将可执行文件与 `.env` 放在同一目录
2. 先启动订单簿服务，再启动主程序
3. 日志、`data/`、`.env` 均相对于可执行文件所在目录读写（由 `runtime_paths.py` 处理）

### 构建隐私说明

PyInstaller 打包时可能在二进制中嵌入构建机器的绝对路径或用户名。若需分发，建议在中性目录（如 `/tmp/build`）或隔离环境（如 GitHub Actions）中构建。

## 工作流程

### 市场筛选

1. 获取所有有流动性奖励的市场
2. 按 `.env` 中的交易量、价差、奖励份额等条件过滤
3. 读取订单簿（优先 SQLite 缓存，必要时实时 HTTP 拉取）
4. 计算奖励区间、竞争份额、收益比值
5. 按收益比值降序排序，选取前 `MAX_MARKETS` 个市场

### 订单管理

1. **初始挂单**：在奖励区间边界价格挂买单和卖单
2. **订单监控**：检测成交与取消；成交后补单；买单成交后对冲卖出
3. **价格调整**：订单簿变化导致边界偏移时，撤单并重新挂单
4. **市场更新**：定期重新扫描，为新机会市场挂单，退出市场的撤单

## 配置说明

配置项均以 `.env` 中的**大写环境变量**为准，下表列出常用项。完整列表见 `.env.example`。

### 账户与交易

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `POLYMARKET_PRIVATE_KEY` | 钱包私钥（必填） | — |
| `POLYMARKET_PROXY_ADDRESS` | 存款/代理钱包地址（必填） | `0x...` |
| `SIGNATURE_TYPE` | 签名类型：`3`=POLY_1271 存款钱包/V2 | `3` |
| `CHAIN_ID` | 链 ID | `137`（Polygon 主网） |

### 做市策略

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `MAX_MARKETS` | 最大做市市场数 | `20` |
| `ORDER_SIZE_MULTIPLIER` | 订单份额倍数 | `2.0` |
| `MAX_EXPOSURE_PER_MARKET_USDC` | 每市场最大敞口（USDC） | `100` |
| `MIN_REWARD_RATIO` | 最小奖励比值阈值 | `0.01` |
| `MIN_PROFIT_MARGIN_BPS` | 对冲卖出最小利润（基点） | `5` |

### 主循环间隔

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `UPDATE_INTERVAL_SECONDS` | 市场扫描间隔（秒） | `300` |
| `ORDER_CHECK_INTERVAL_SECONDS` | 订单状态检查间隔（秒） | `30` |
| `ORDERBOOK_UPDATE_INTERVAL_SECONDS` | 订单簿监控 / 调价间隔（秒） | `5` |
| `PRICE_DEVIATION_THRESHOLD_BPS` | 价格偏离阈值（基点） | `1` |

### 订单簿数据服务

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ORDERBOOK_SERVICE_ENABLED` | 是否启用订单簿服务 | `true` |
| `ORDERBOOK_SERVICE_MARKET_SCAN_INTERVAL` | 市场扫描间隔（秒） | `300` |
| `ORDERBOOK_SERVICE_ORDERBOOK_UPDATE_INTERVAL` | 订单簿更新间隔（秒） | `30` |
| `ORDERBOOK_TTL` | 缓存过期时间（秒） | `300` |
| `ORDERBOOK_DB_PATH` | SQLite 路径（留空用默认） | — |

## 日志

日志保存在 `logs/YYYY-MM-DD/` 目录下，按模块分文件，例如：

- `logs/2026-06-22/main.log` — 主程序
- `logs/2026-06-22/order_manager.log` — 订单管理
- `logs/2026-06-22/market_manager.log` — 市场管理
- `logs/2026-06-22/orderbook_data_service.log` — 订单簿服务

## 注意事项

1. **私钥安全**：`.env` 含私钥，切勿提交到 Git；分发二进制时单独交付 `.env`
2. **先启订单簿服务**：建议先运行 `orderbook` / `orderbook.exe`，再启动 `main` / `main.exe`
3. **SIGNATURE_TYPE**：2026 年 Polymarket CLOB V2 新账户需使用 `SIGNATURE_TYPE=3`
4. **风险控制**：根据资金情况合理设置 `MAX_EXPOSURE_PER_MARKET_USDC`
5. **网络稳定性**：确保网络连接稳定，避免下单失败

## 故障排除

### 订单簿缓存为空或数据过旧

- 确认订单簿数据服务已启动并在运行
- 查看 `logs/.../orderbook_data_service.log`
- 检查 `data/orderbook_cache.sqlite3` 是否生成
- 适当调小 `ORDERBOOK_SERVICE_ORDERBOOK_UPDATE_INTERVAL`

### 订单下单失败

- 检查 `POLYMARKET_PRIVATE_KEY` 和 `POLYMARKET_PROXY_ADDRESS` 是否正确
- 确认 `SIGNATURE_TYPE=3`（CLOB V2 存款钱包）
- 检查账户 USDC 余额是否充足
- 查看 `order_manager.log` 中的详细错误

### 市场筛选结果为空

- 检查 `MIN_REWARD_RATIO` 是否过高
- 检查 `SPREAD_RANGE_*`、`VOLUME_24HR_RANGE_*` 等过滤条件是否过严
- 确认订单簿服务已写入有效缓存

### API 503 / 分页错误

- 项目已处理 Polymarket 分页终止游标 `LTE=`；若仍遇 503，多为 API 临时不可用，稍后重试

## 许可证

[根据项目实际情况填写]

## 贡献

欢迎提交 Issue 和 Pull Request。
