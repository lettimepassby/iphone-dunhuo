# iPhone 蹲货 · iphone-dunhuo

Apple 中国官网（apple.com.cn）**线下门店取货**自动盯库存 + 有货即下单的助手。

思路很简单：**库存靠公开接口扫，下单靠你自己已登录的 Safari。** 脚本不碰你的密码、不代持 cookie —— 结账走的是你本机 Safari 里那份已登录会话（AppleScript `do JavaScript`）。库存轮询走匿名的公开接口，不带 cookie。你只负责登录 Apple ID 和最后在支付宝点确认，中间的「盯货 → 锁店 → 填取货联系人 → 立即下单」全自动。

> ⚠️ 仅供学习研究与个人自用。请遵守 Apple 网站条款与当地法律，自行承担使用风险；不要用于黄牛囤货或任何商用抢购。频繁请求可能触发风控（HTTP 541 按出口 IP 限流）。

## 它能干嘛

- 公开 `GET /shop/retail/pickup-message` 匿名轮询多门店库存（不登录、不带 cookie）
- 多代理并行扫描 + 每代理常驻 keep-alive，主循环不等最慢的节点
- 有货瞬间在结账页 **一次 JS 连发** graviton POST：锁店 → 取货联系人 → 支付宝 → 立即下单
- 支持多锚点门店覆盖（例：杭州优先，宁波 / 上海有货也下单）
- 结账会话自动 `extendSession` / 掉线自愈；下单进付款页持续响铃提醒你去支付宝确认

## 先配好自己的资料

仓库里只有 `*.example.json` 模板，真实资料/密钥不入库（见 `.gitignore`）。第一次用先复制成自己的：

```bash
cp config/buyer.example.json   config/buyer.json     # 你的取货联系人 / 发票 / 支付
cp config/proxies.example.json config/proxies.json   # 代理池（可只留 direct 本机直连）
```

- `config/buyer.json` — 取货联系人：姓、名、邮箱、身份证后四位、发票类型、支付方式。手机号用 Apple ID 里已存的，不在 POST 里传。
- `config/proxies.json` — 库存扫描用的代理节点。**只扫库存用**，结账永远走本机 Safari。只想直连就保留 `{"name":"direct"}` 一项即可。青果等付费代理把 key 填进去或用环境变量，别提交。

## 怎么跑

1. Safari 登录 Apple ID，购物袋里放好你要的型号（数量确认无误）
2. 点结账，停在 `https://secure6.www.apple.com.cn/shop/checkout...`（别停在 `session_expired`）
3. Safari 开发菜单 →「允许来自 Apple 事件的 JavaScript」打开
4. 开抢（在项目根目录）：

```bash
python3 scripts/auto_order.py          # 全自动：扫库存，有货立刻锁店填资料下单
python3 scripts/auto_order.py 1        # 慢一点，1 秒一轮
python3 scripts/auto_order.py --no-place   # 停在「查看订单」，不点立即下单（演练用）
python3 scripts/auto_order.py --once   # 只打一枪看库存
```

只盯库存、不登录、不碰结账页：

```bash
python3 scripts/watch_stock.py         # 多代理并行扫，每次打印各门店状态
python3 scripts/watch_stock.py 5       # 慢一点
```

刷新免费代理池（只扫库存，结账仍走本机 Safari）：

```bash
python3 scripts/fetch_proxies.py                 # 快代理 + scdn + 89ip + ProxyScrape/Geonode/GitHub 等
python3 scripts/fetch_proxies.py --kuaidaili 15  # 快代理翻 15 页
python3 scripts/fetch_proxies.py --no-extra      # 跳过 GitHub 聚合列表
python3 scripts/fetch_proxies.py --keep 80       # 最多留 80 条 live
```

## 一点原理

**库存轮询**走公开 `GET /shop/retail/pickup-message`（不登录、不带 cookie），`pickupDisplay==available` 才算有货。老接口 `/shop/fulfillment-messages` 恒定 HTTP 541，不要用。每个代理一条常驻线程 + keep-alive，结果随到随用。HTTP 541 是 Apple 按出口 IP 限流，只冷却该节点（12→24→40 秒）不踢；吞吐靠轮转多个 IP，不靠打爆同一个。连续硬失败的静态节点会从池里踢掉。

**下单**在结账页一次异步 `fetch` 连发同源 graviton POST：`selectFulfillmentLocation=RETAIL` →（换城市才 `Selectdistrict`）→ `_a=select` → 没带出时段再 `select` 一次 → `continueFromFulfillmentToPickupContact` → 联系人 → ALIPAY → `continueFromReviewToProcess`。时段 `timeSlotId`/`signKey` 从 select 响应现取。用页面里的异步 fetch（躲开 Safari 同步 XHR ~10 秒超时），中间不点 UI 按钮。结账必须走已登录 Safari（cookie + `x-aos-stk`），不能裸 curl。

**会话**：结账会话约 20 分钟（不是 Apple ID 本身）。脚本定时 `extendSession`；ttl 过低 / 「你还在购物吗」overlay / 页面变 `session_expired` / Safari JS 空返回，都会自己从购物袋再点结账。只有 Safari 跳到 Apple ID 登录页才需要你重新登录。别关结账标签、电脑别睡。

> 结构性限制：storeLocator 的 `select` 服务端要现生成并签名时段，约 10 秒，任何客户端都一样；库存窗口有时不到 1 秒。所以真正的胜负手是**检测新鲜度**（代理越快越好），而不是把下单再压快几毫秒。

## 文件

- `scripts/auto_order.py` — **全自动下单主入口**：公开库存轮询 + 有货锁店下单
- `scripts/watch_stock.py` — 只查库存，不登录、不碰 Safari
- `scripts/pickup_stock.py` — 公开 `pickup-message` 客户端 / 多代理调度
- `scripts/fetch_proxies.py` — 免费代理抓取 + 探活
- `scripts/snipe_hangzhou.py` — 只锁店、不填资料
- `scripts/safari_lock_hangzhou.py` — DOM 点选备用
- `scripts/nudge.swift` — 非抢占式输入注入（CGEventPostToPid，缓解 Apple ID idle 登出），首次用会现编成 `scripts/nudge`
- `config/*.example.json` — 配置模板（复制成同名 `.json` 使用）
- `config/product.json` / `skus.json` / `stores.json` / `endpoints.json` — 商品 / SKU / 门店 / graviton 端点（公开数据）
- `docs/checkout-flow-analysis.md`、`docs/shanghai-bounce.md` — 抓包分析与选店地理

## License

MIT
