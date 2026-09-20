# 选店地理：杭州 / 宁波 / 上海

公开库存扫到哪家有货，结账 `_a=select` 就带那家的省市，不再把「上海」当失败。

苏州 R688、无锡 R574 仍丢掉（未要）。

## 放行

杭州（优先）：

```
_a=select  selectStore=R471 或 R532
           state=浙江 city=杭州 district=上城区
           searchInput=浙江 杭州 上城区
```

宁波：

```
_a=select  selectStore=R531
           state=浙江 city=宁波 district=海曙区
           searchInput=浙江 宁波 海曙区
```

上海：

```
_a=select  selectStore ∈ {R678,R401,R359,R389,R390,R683,R705,R581}
           state=上海 city=上海  对应区
```

锁店成功：`selectStore` 等于本次请求的目标店，且 `dude` 对上店名（或 timeSlot.storeId 对上）。

## 仍丢掉

```
selectStore=R688 苏州 / R574 无锡
selectStore 对不上本次请求的目标店（回弹到别家）
OOS 路径「继续填写送货地址」/ continueFromFulfillmentToShipping
```

以前「凡是上海都丢」已经作废。R401 环贸现在是合法取货店，只是优先级在西湖 / 万象城 / 天一 / 静安之后。
