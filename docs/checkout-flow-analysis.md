# Apple 中国大陆 线下取货结账抓包分析

来源：Reqable 会话 `1789290897101519-86`（2026-09-13，Safari 模拟下单）。
当前 Safari 停在：`https://secure6.www.apple.com.cn/shop/checkout/interstitial`（付款说明）。

本次实际走过完整取货链路的商品是 **AirPods 5**（`MKFW4CH/A`）。
同会话购物车里同时出现过 **iPhone 18 Pro Max 512GB 冰川蓝色**（`MJYE4CH/A`），所以 iPhone 切过去时接口不变，只换 part number。

---

## 1. 你要的两家店

| 店名 | storeId | 地址 | 电话 | 搜索定位 |
|---|---|---|---|---|
| Apple 西湖 | **R471** | 杭州市上城区平海路 100 号 | 4006171302 | 浙江 杭州 上城区 |
| Apple 杭州万象城 | **R532** | 杭州市上城区富春路 701 号 | 4006171304 | 浙江 杭州 上城区 |

两家都在上城区。搜索 `浙江 杭州 上城区` 后，返回列表第一名是西湖（2.11 km），第二名是万象城（3.95 km）。

---

## 2. 上海弹窗 / 回弹的根因（这是关键）

**不是门店列表 API 把杭州搜丢了，而是前端 / 定位模块又发了一枪，把 storeLocator 重置成上海。**

默认选中店始终是上海环贸 iapm：

| 店名 | storeId | 角色 |
|---|---|---|
| Apple 上海环贸 iapm | **R401** | 系统默认 `selectStore`，定位失败 / MapKit 失败时的回退店 |
| Apple 静安 | R678 | 上海列表第 2 |
| Apple 香港广场 | R390 | 上海列表第 3 |

### 2.1 证据链

抓包里反复出现这种成对请求：

```
[39]  POST storeLocator.search     selectStore=R401  search=浙江 杭州 上城区
     → 响应列表已经是西湖 R471、万象城 R532 排前，但 d.selectStore 仍是 R401

[41]  POST storeLocator.select/search  selectStore=R401  search=上海 徐汇区
     → 被弹回上海
```

第二次杭州成功选店后同样发生：

```
[109] POST selectStore=R471  search=浙江 杭州 上城区   ← 这是你点的「Apple 西湖」
      响应：selectStore=R471，companionBar 显示 Apple 西湖，可约 09/19 时段

[111] POST selectStore=R401  search=上海 杨浦区         ← 不是你点的，是定位模块自己打的
      响应：又回到上海环贸
```

Echo 错误日志把触发点写死了：

- `MapKit failed to initialize` @ `react-trans/getCoordinates`
- `Error: MapKit failed to initialize` @ `react-trans/StoreList`
- 随后浏览器把 `searchInput` 改回 IP/定位得到的上海区（先是徐汇，后是杨浦）

Safari 被 Reqable 代理后 **MapKit 初始化失败**。checkout.js 的 `StoreList` 拿不到杭州坐标，就走 `locationConsent` + 默认 geo，把 `selectStore` 写回 `R401`。

`checkout.locationConsent.locationConsent = true` 时，页面会主动 POST：

```
POST /shop/checkoutx?_a=location-consent&_m=checkout.locationConsent
```

这会间接触发一次 storeLocator 刷新。刷新用的是定位城市，不是你刚选的城市。

### 2.2 正确的选店请求 vs 回弹请求，怎么分辨

**只认这一个 action 才是「用户选店」：**

```
POST https://secure6.www.apple.com.cn/shop/checkoutx/fulfillment
     ?_a=select
     &_m=checkout.fulfillment.pickupTab.pickup.storeLocator
```

body 必须同时满足：

```
checkout.fulfillment.pickupTab.pickup.storeLocator.selectStore = R471   # 或 R532
checkout.fulfillment.pickupTab.pickup.storeLocator.searchInput = 浙江 杭州 上城区
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.state = 浙江
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.city = 杭州
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.district = 上城区
```

**下列请求全部忽略 / 直接丢掉，它们是上海弹窗：**

| 特征 | 含义 |
|---|---|
| `selectStore=R401` | 默认上海环贸，不是杭州店 |
| `searchInput` 含 `上海` | 定位回退 |
| `state=上海` 且 `city=上海` | 定位回退 |
| `district=徐汇区` 或 `杨浦区` | 两次回弹用过的上海区 |
| `_a=search` 但 `selectStore` 仍是 R401，同时 searchInput 已是杭州 | 只刷新了列表，**没有真正选中杭州店** |
| `_a=location-consent` 之后紧跟的 storeLocator POST | 定位同意后的自动刷新，会覆盖你刚选的店 |
| `_a=Selectstate` / `Selectcity` / `Selectdistrict` | 省市区下拉，只改 options，不锁定门店 |

判断一次选店是否真正生效，看响应 JSON：

```
body.checkout.fulfillment.pickupTab.pickup.storeLocator.d.selectStore == "R471" 或 "R532"
body.checkout.fulfillment.pickupTab.pickup.timeSlot.d.storeId          == 同上
companionBar / itemDelivery.d.pickup.dudeAttributeStore               == "Apple 西湖" 或 "Apple 杭州万象城"
```

只要这三项里任何一项还是 `R401` / `Apple 上海环贸 iapm`，就还在上海弹窗态。

---

## 3. 结账 graviton 接口

Host：`https://secure6.www.apple.com.cn`

所有 checkoutx 都是 `POST application/x-www-form-urlencoded`。
每次响应 `body.meta.h.x-aos-stk` 必须带回下一请求头 `x-aos-stk`。

### 3.1 取货步骤（按顺序）

| 步 | URL | `_a` | `_m` | 作用 |
|---|---|---|---|---|
| 打开结账 | `GET /shop/checkout` | — | — | HTML + `init_data` JSON |
| 选线下取货 | `/shop/checkoutx/fulfillment` | `selectFulfillmentLocationAction` | `checkout.fulfillment.fulfillmentOptions` | body: `selectFulfillmentLocation=RETAIL` |
| 选省 | 同上 | `Selectstate` | `...storeLocator.address.stateCitySelectorForCheckout` | `state=浙江`（city 此时还可能是上海，正常） |
| 选市 | 同上 | `Selectcity` | 同上 | `city=杭州` |
| 选区 | 同上 | `Selectdistrict` | 同上 | `district=上城区`，同时改 `searchInput` / `provinceCityDistrict` |
| 搜索门店 | 同上 | `search` | `checkout.fulfillment.pickupTab.pickup.storeLocator` | 带 8 个 storeLocator 字段 |
| **锁定门店** | 同上 | **`select`** | 同上 | **`selectStore=R471 或 R532`** |
| 选签到时段 | 同上 | `continueFromFulfillmentToPickupContact`（AirPods 路径） | `checkout.fulfillment` | 必须带 timeSlot.* 和 selectStore |
| 取货联系人 | `/shop/checkoutx` | （pickupContact continue） | `checkout.pickupContact` | 姓名 / 邮箱 / 身份证 / 发票 |
| 支付方式 | `/shop/checkoutx/billing` | — | `checkout.billing` | |
| 核对 | `/shop/checkoutx/review` | — | `checkout.review` | |
| 会话续期 | `/shop/checkoutx/session` | `extendSession` | `checkout.session` | ttl 约 20 分钟，交互超时 300s |

iPhone 有货时 `continueFromFulfillmentToPickupContact` 才会出现。本次 iPhone 18 Pro Max 在所有店都是 `目前不可取货` / `storeDisabled=true`，所以走不通取货 continue；AirPods 5 有货后这条 action 才出现。

### 3.2 选店 body 模板（杭州，不要改）

```
checkout.fulfillment.pickupTab.pickup.storeLocator.showAllStores=false
checkout.fulfillment.pickupTab.pickup.storeLocator.selectStore=R471
checkout.fulfillment.pickupTab.pickup.storeLocator.searchInput=浙江 杭州 上城区
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.city=杭州
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.state=浙江
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.provinceCityDistrict=浙江 杭州 上城区
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.countryCode=CN
checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout.district=上城区
```

万象城只把 `selectStore` 换成 `R532`，其它字段不动。

### 3.3 时段字段（选店成功后才会有）

```
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.date
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.dayRadio
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.startTime
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.endTime
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotId      # 服务端签发，一次性
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.signKey        # 服务端签发，一次性
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue  # 例如 19-13:15-13:30
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeZone=Asia/Shanghai
checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.isRecommended=false
```

`timeSlotId` / `signKey` 不能复用，必须从当前 `dateTimeSlots.d` 里取。

---

## 4. 库存查询（加购前）

公开接口，不走 checkout session：

```
GET https://www.apple.com.cn/shop/fulfillment-messages
    ?fae=true
    &pl=true
    &mts.0=regular
    &mts.1=compact
    &cppart=UNLOCKED/WW
    &parts.0=MJYE4CH/A
    &searchNearby=true
    &store=R471
```

只看返回 `stores[].storeNumber in {R471, R532}` 且 `partsAvailability[sku].pickupDisplay == available`。
`searchNearby=true` 会带出上海店，那些全部丢掉。

本次抓包里 iPhone 18 Pro Max 在西湖/万象城都是「目前不可取货」；AirPods 5 西湖「星期六 2026/09/19 可取货」，万象城「2026/09/20 可取货」。

直连 `fulfillment-messages` 在本机被 541 拦截（anti-bot），Safari 里走同一接口是通的。助手侧要用浏览器 cookie，不要裸 curl。

---

## 5. 切到 iPhone 18 Pro Max 时接口差异

没有新接口。差异只有：

1. 购物车 part：`MJYE4CH/A`（512GB 冰川蓝，本次已在袋里出现过）。
2. 有货前 `storeDisabled=true`，`selectStore` 即使 POST 了 R471，itemDelivery quote 也不会变成西湖，continue 仍走 shipping 而不是 pickupContact。
3. 有货瞬间：`storeDisabled` 变 false，出现 `timeSlot`，`continueFromFulfillmentToPickupContact` 才可点。
4. 上海回弹逻辑完全一样，必须在选店成功后立刻禁掉 locationConsent 刷新，或忽略一切 `R401`/`上海` 的后续 POST。

建议切 SKU 后重新抓一段「选零售店 → 选浙江 → 选杭州 → 选上城区 → 点西湖」的 10 秒窗口，对照 `docs/shanghai-bounce.md` 验证 action 名有没有改。

---

## 6. Safari 控制（已打开）

「允许来自 Apple 事件的 JavaScript」已打开，可以用 AppleScript `do JavaScript` 读结账页、点西湖/万象城。

当前 live 页（2026-09-13 约 18:00）：

- URL: `https://secure6.www.apple.com.cn/shop/checkout?_s=Fulfillment-init`
- 取货 tab 已选，定位 **浙江 杭州 上城区**
- 文案：你所在的地区无货可取
- 单选框 `input[name=store-locator-result]`：R471 西湖、R532 万象城 排前，**全部 disabled**
- 继续按钮 `data-autom=fulfillment-continue-button` 文案是「继续填写送货地址」→ 这是 **OOS 送货路径**，有货前不要点
- 有货后 continue 才会变成 `continueFromFulfillmentToPickupContact`
- 刷新门店：点 `data-autom=fulfillment-pickup-store-search-button`（按钮文字就是「浙江 杭州 上城区」）

库存探测不要裸 curl `/shop/fulfillment-messages`（本机 HTTP 541）。Safari 结账域跨域 fetch 也会 CORS。可靠信号就是结账页这两个 radio 的 `disabled`。

---

## 7. 加购 + AppleCare+（会话 60 购物袋 / PDP）

17:43 那段 Reqable（`1789292595372636-269`）是微信 cookie JSON + VS Code telemetry，**不是** Apple 加购包。真正的 iPhone + AC+ 状态以 live Safari 和会话 `1789290872442821-60` 购物袋 HTML 为准。

锁定组合：

| 件 | SKU | 价格 |
|---|---|---|
| iPhone 18 Pro Max 512GB 冰川蓝色 | `MJYE4CH/A` | RMB 12,999 |
| AppleCare+（同时加入 iPhone 年年焕新计划） | `SHJL3CH/A` | RMB 1,799 |
| 合计 | | **RMB 14,798** |

PDP `window.PRODUCT_SELECTION_BOOTSTRAP`：

- 三步：`dimensionScreensize=6_9inch` → `dimensionColor=glacier` → `dimensionCapacity=512gb`
- `familyType=iphone18promax`，`part=IPHONE18PRO_MAIN`
- CTA 文案是「继续」，不是「加入购物袋」
- 加购入口：`POST /shop/pdpAddToBag`（完整 form 字段名这次没抓到 body）

AppleCare 挂在 `window.APPLECARE_BOOTSTRAP`，**不是**购物袋里再 addToCart：

```
compatibleWuip = IPHONE18PROMAX
appleCareType  = acp
additionalParams.acpart = SHJL3CH/A
```

Pro 的 AC+ 是 `SHHY3CH/A`，不要加错。`acpart=none` 是不加。

购物袋 graviton（`x-aos-model-page: cart`）：

```
POST /shop/bagx/checkout_now?_a=checkout&_m=shoppingCart.actions
POST /shop/bagx?_a=addToCart&_m=shoppingCart.recommendations.recommendedItem
POST /shop/bagx?_a=location-consent&_m=shoppingCart.locationConsent   ← 会把取货店打回上海环贸，丢掉
```

会话 60 购物袋 delivery 已写着 `dudeAttributeStore=Apple 上海环贸 iapm` / 「不可在 Apple 上海环贸 iapm取货」。进结账后必须重新走杭州省市区 + `_a=select`。

`recommendedForYou` 用 `partsInCart.0=MJYE4CH/A&partsInCart.1=SHJL3CH/A` 可确认袋内两件。

---

## 8. 有货后锁店（Safari）

1. 确认定位按钮仍是「浙江 杭州 上城区」，不是上海
2. 点搜索按钮刷新列表
3. `R471` radio `disabled=false` → 点西湖（优先）
4. 否则 `R532` 可点 → 点万象城
5. **不要点** R401 / R678 / R390 / 任何上海店
6. 响应或页面 `dudeAttributeStore` ∈ {Apple 西湖, Apple 杭州万象城} 才算锁住
7. 出现 timeSlot 后再点继续；仍是「继续填写送货地址」就还没锁上

脚本：`scripts/safari_lock_hangzhou.py`


---

## 9. 全自动下单（`scripts/auto_order.py`）

你只登录。脚本在 Safari 结账同源里发 graviton XHR：

| 步 | `_a` | `_m` |
|---|---|---|
| 取货 tab | `selectFulfillmentLocationAction` | `checkout.fulfillment.fulfillmentOptions` |
| 轮询 | `search` | `checkout.fulfillment.pickupTab.pickup.storeLocator` |
| 锁店 | `select` | 同上，`selectStore` 只允许 R471 / R532 |
| 继续 | `continueFromFulfillmentToPickupContact` | `checkout.fulfillment` + 当前 `timeSlotWindows` 第一个非 restricted 档 |
| 联系人 | `continueFromPickupContactToBilling` | `checkout.pickupContact`（`config/buyer.json`） |
| 支付宝 | `selectBillingOptionAction` | `checkout.billing.billingOptions` `ALIPAY` |
| 核对 | `continueFromBillingToReview` | `checkout.billing` |
| 立即下单 | `continueFromReviewToProcess` | **`checkout.review.placeOrder`**（响应里按钮 action；`checkout.review` 是父级 fallback） |

成功：`head.status=302` → `/shop/checkout/status` → `/shop/checkout/interstitial`。支付宝确认仍要人点。

`timeSlotId` / `signKey` 必须从 **本次 select 响应** 取，不能复用 AirPods 抓包。空窗口（当天 `nslots=0`）跳过。`isRestricted` 跳过。search 响应里的时段不算锁，必须先 `_a=select`。

不要点「继续填写送货地址」（OOS 送货路径 `continueFromFulfillmentToShipping`）。

会话过期停在 `shop/sorry/session_expired`：从购物袋重新结账。脚本在购物袋页会自己 `POST /shop/bagx/checkout_now`。停在取货联系人 / 付款 / 查看订单页会接着填，不再轮询库存。
