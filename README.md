# page-watch

盯着某个网页列表，记录它的变化节奏，并在你关心的东西变了的时候告警。

第一个接入的是 Nordstrom Rack 清仓页，但框架是通用的 —— 任何"列表页"都能
归约成 **总数 + 榜首条目 + 本页条目 ID 集合** 这三样东西。

## 它回答两个问题

1. **这个页面什么时候更新？** —— 每次抓取都落库，`analyze` 用 星期×小时
   热力图把上新节奏画出来，之后你就知道该什么时候手动去刷。
2. **榜首的东西换了吗？** —— 换人或降价立刻弹 macOS 通知（可选邮件），
   带直达链接。

## 零依赖

只用 Python 3.11+ 标准库。没有 requests、没有 BeautifulSoup、没有 Playwright、
没有 venv。这是刻意的：定时任务最怕的就是"手动跑没问题，launchd 里找不到包"。

## 快速开始

```bash
cd ~/PycharmProjects/page-watch
cp config.example.toml config.toml   # 首次使用
vim config.toml                      # 改 URL

python3 run.py poll                  # 抓一次，看看通不通
python3 run.py test-notify           # 测通知渠道
python3 run.py setup-email           # 配邮件（可选，密码存钥匙串）
python3 run.py install-launchd       # 装定时任务，默认每 15 分钟
```

## 邮件告警

密码**不存在配置文件里**。`setup-email` 用 `getpass` 读取（不回显、不进
shell 历史），存进 macOS 钥匙串（服务名 `page-watch-smtp`），
`config.toml` 里只留一个 `use_keychain = true`。

```bash
python3 run.py setup-email
```

Gmail 需要**应用专用密码**（不是登录密码，且账号必须先开两步验证）：
Google 账号 → 安全性 → 两步验证 → 应用专用密码。

`from` 和 `to` 填同一个地址（自己发给自己）完全可行，Gmail 会正常投进收件箱。

密码解析顺序：钥匙串 → `config.toml` 里的 `password` 字段。两处都没有时
只记一条"配置不完整"并跳过邮件，**不会影响抓取和 macOS 通知**。

## 命令

| 命令 | 作用 |
|---|---|
| `poll` | 抓取一次。加 `--dry-run` 则不写库不发通知 |
| `status` | 当前状态、变化分布、最近告警 |
| `analyze` | 星期×小时热力图。`--metric asof/top_change/new_markdown/brand_new/up/new/polls` |
| `calibrate` | 用实测数据推荐告警阈值 |
| `events` | 历史告警列表 |
| `test-notify` | 测试通知渠道 |
| `blocks` | 被挡/被降级情况汇总，判断是否触发限流 |
| `reseed-top` | 改过 `top_n` 后跑一次，避免误报一批新面孔 |
| `setup-email` | 交互式配置邮件，密码存入 macOS 钥匙串 |
| `install-launchd` | 安装/重装定时任务，`--interval 分钟` |

停止定时任务：

```bash
launchctl bootout gui/$(id -u)/com.xwan.pagewatch
```

## 夜里电脑睡着怎么办

Mac 睡眠期间 launchd 不会跑，醒来后补跑**一次**（不是把错过的几十次都补上）。
所以如果你晚上合盖或让机器休眠，`top_change` / `new_markdown` 这些**基于采样
时刻**的指标在夜间就是一片空白。

但这不代表夜里的事就看不到了 —— Rack 的每件商品都带着
`propositions[0].asOf`，也就是**这条数据上次被重新计算的时刻**。实测一页
72 件商品有 72 个互不相同的 asOf，跨度约 19 小时。

```bash
python3 run.py analyze --metric asof
```

这个视图**不受采样时段限制**：白天抓一次，就能回溯十几个小时，包括电脑睡着
的那一整夜。每件商品按 `item` 表去重，不会因为连续抓取而重复累加。

实测昨夜的分布（PercentOff 页）：

```
Wed                   ·:··:@#       40      ← 18-23 点，22-23 点是峰值
Thu  ·· ·  ·· +++:                  32
```

两个指标是互补的，不要混用：

| 指标 | 时间来源 | 夜间 | 用途 |
|---|---|---|---|
| `asof` | 商品自带时间戳 | **可见** | 回答"Rack 什么时候重新定价" |
| `top_change` / `new_markdown` / `brand_new` | 我们的采样时刻 | 空白 | 需要连续采样才有意义的事件 |

真要让它整夜跑的话（MacBook 必须**开着盖**，合盖会强制睡眠）：

```bash
sudo pmset -c sleep 0     # 接电源时不休眠，屏幕照常关
sudo pmset -c sleep 1     # 改回去
```

## 为什么用 launchd 不用 cron

Mac 合盖睡眠期间，cron 到点的任务会被**直接丢弃**，不补跑。launchd 的
`StartInterval` 在唤醒后会立刻补一次。对"确定更新时间点"这个目标，
每个漏掉的采样点都是数据空洞 —— `analyze` 里的 `?` 就是在显示这种空洞。

## 为什么盯"前 5 名"而不只盯榜首

实测发现榜首会被**没人要但折扣标得高**的商品长期霸占：一件 Adornia 三件套耳钉，
93% off、571 件可直邮库存，整整一天一件没卖出去，却霸榜数周。而真正的好货
（如 94% off、可直邮仅 1 件的 MAX STUDIO 裙子）进来时往往**夺不了冠**，
却很快被买走。

只盯第 0 名 = 一直盯着一个没人要的常驻选手，真货在它下面进进出出而你收不到通知。

所以默认开启 `top_n_new`（前 5 名出现新面孔就告警）。多件新货合并成**一条**通知，
链接指向其中最便宜的那件。前 5 名内部换位置不告警 —— 只有新面孔才算。

## 自定义规则

排名窗口（`top_n`）有个根本缺陷：**它筛的是名次，不是你要的东西**。实测
10-28 第 1 页 72 件全部是 91-94% off，而一件 90% off、只剩 1 件的
Veronica Beard 裤子排在第 2 页 —— 名次筛选看不见它。

规则在**整个抓取范围**内筛选：

```toml
[[watcher.rule]]
name = "深折扣薄库存"
enabled = true
min_pct_off = 90           # 折扣 ≥ 90%
min_ship_qty = 1           # 至少能寄 1 件（排除只能到店自提的）
max_ship_qty = 3           # 最多 3 件 = 手慢就没
reentry_hours = 24
```

可用条件（都可省略，可自由组合）：

| 条件 | 含义 |
|---|---|
| `min_pct_off` / `max_pct_off` | 折扣区间 |
| `min_ship_qty` / `max_ship_qty` | 可直邮件数区间 |
| `min_price` / `max_price` | 现价区间 |
| `min_compare_at` / `max_compare_at` | 原价（对标价）区间 |
| `brands` / `exclude_brands` | 品牌白名单 / 黑名单（不区分大小写）|
| `require_tags` | 必须带的标签，如 `["NEW_MARKDOWN"]` |

### 折扣到不了 90% 的品类，要单开 watcher

主 watcher 只抓到 90% off 为止，像墨镜这种天生折扣低的品类永远进不来
（$250 → $40 也才 84%）。做法是用站点的筛选器把范围缩小，1 页就能全覆盖：

| 筛选 | 参数 | 结果集 |
|---|---|---|
| 品类 | `&filterByProductType=accessories_sunglasses` | 30 件 |
| 品牌 | `&filterByBrand=save-the-duck` | 3 件 |
| 多品牌 OR | `&filterByBrand=gucci&filterByBrand=save-the-duck` | 18 件 |

页面开销是固定的（85-150KB），跟商品数几乎无关，所以小结果集的 watcher 很便宜。

> 实测 Gucci 的清仓商品**全部是墨镜**，所以墨镜 watcher 已覆盖 Gucci，不必单开。

多条规则各报各的，每条**独立去重** —— 调整一条不会影响其他规则的状态。

### 抓取范围必须跟着规则门槛走

规则门槛 90% 但只抓到 91% 就没有意义。所以加了自动翻页：

```toml
until_pct_off = 90   # 一直翻到本页最低折扣跌破 90%
max_pages = 6        # 安全上限
```

**不能写死页数** —— 折扣分布每天都在变（10-26 第 1 页末尾是 88%，10-28
变成 91%），写死的话规则会悄悄漏掉命中项，而且不会报错。

### 改规则后要播种

```bash
python3 run.py reseed-top
```

它会把当前所有命中项写进"已见过"记忆并打印出来，避免下一轮把存量全报一遍。
顺带也能用它**预览规则会命中什么**，调参数时很方便。

## 不重复报告同一件事

实测暴露的假警报：好货 X 挤进前 5 → 告警（对）；X 被买走后，原来被挤到第 6 名的
条目回到前 5 → **又告警一次**（错，什么都没发生）。榜首也一样：10-24 06:17 报
"榜首换人 96% off"（Halogen $4.29），06:32 又报一次 —— 其实只是换回了原来那个
常驻商品。

只跟上一次快照比做不到这件事，因为"上一次"本身就是被污染的状态。所以改成
**记住每个条目最近一次出现在前 10 名的时间**（`top_seen` 表）：

- 被挤到第 6-10 名期间，记录一直在刷新 → 回到前 5 时不算新面孔
- 记忆范围自动取 `top_n + 10`，**永远宽于告警窗口** —— 被挤出前 N 名的条目
  在记忆范围内记录仍在刷新，回来时不算新面孔。改 `top_n` 不会破坏这个约束
- 超过 `reentry_hours`（默认 24 小时）没进过前 10 的，重新算新面孔 ——
  它可能是大幅降价才回来的，那确实是新消息
- `primary_changed_only_new = true` 让榜首告警也遵守同一套判据

效果：

```
1) 基线                     告警 无
2) 稳定                     告警 无
3) 好货 X 抢到榜首            告警 PRIMARY_CHANGED + TOP_N_NEW
4) X 还在                   告警 无
5) X 卖掉，一切回到原状        告警 无          ← 修复前这里会误报两条
6) 另一件新货 Y 进来          告警 PRIMARY_CHANGED + TOP_N_NEW
```

## 两个 watcher 的分工

| watcher | 排序 | 职责 | 告警 |
|---|---|---|---|
| `nrack_women_top` | `PercentOff` | 盯折扣最高的那一件 | 榜首换人 / 榜首降价 → 弹窗+声音 |
| `nrack_women_newest` | `Newest` | 测量上新时点 | 默认静音，只攒数据 |

## 为什么不用"总数变多"判断上新

实测总数在分钟级**双向**波动 ±10（随时有人下单、有人退货），而真实上新
常常一次只有 1 件 —— 完全淹没在噪声里。靠阈值既漏报又误报，所以
`count_up` 默认关闭。

取而代之的是 `Newest` 排序：按上架时间倒序，任何一件新商品都会落到第 0 位。
**榜首一变就是上新，不需要任何阈值，1 件也抓得到。**

## 三种"新增"口径，灵敏度递减

| 口径 | 含义 | 噪声 |
|---|---|---|
| `top_change` | Newest watcher 的榜首变了 | **最干净**，推荐用这个看上新时点 |
| `n_brand_new` | 历史从没见过的 ID 数 | 含第 71-72 名边界的排序抖动，实测约 1 件/分钟的虚高 |
| `n_new` | 相对上一次，本页新出现的 ID | 最吵，会把"从第 2 页挤上来"的老货算进来 |

## 关于"仅限门店自提"的商品

Rack 有个**门店模式**：在浏览器里选过 preferred store 之后，同一个 URL 的
结果里会混进"只能到店取、不能寄"的商品，总数也随之变大（实测用户看到过
9824，而匿名抓取同一时刻是 9008 左右）。这些东西网上根本买不了，排在第一位
没有意义。

好消息是这个模式由 **cookie/会话** 驱动，不是 URL 参数：

```
storeMode.selectedStoreIds = []      # 匿名请求下完全是空的
storeMode.postalCode       = ""
shopper.Pickup             = {"pickupcity":"","pickupstore":"","pickupstorename":""}
```

URL 里的 `preferredStore=YOUR_STORE_ID` 被解析了但**不会激活门店模式**。所以这个抓取器
（永远匿名、不带 cookie）永远拿到干净的"可直邮"视图 —— 比你在浏览器里看到的
更稳定。实测横跨整个结果集采样 360 件（第 1/10/40/80/125 页），
`isPickAndNotShip` 全为 false。

**实证**：用户浏览器（门店模式）里排前 4 的商品，拿到匿名抓取的 72 件里去找 ——

| 浏览器排名 | 商品 | 浏览器显示 | 在匿名结果集里 |
|---|---|---|---|
| 0 | LK Bennett Valerie Heart Print Skirt | Pickup ready in 4 hours | ✗ 不存在 |
| 1 | MAX STUDIO Floral Wreath Maxi Skirt | Shipping arrives Oct 28 | ✓ 排名 0 |
| 2 | MAX STUDIO Peak Lapel Blazer | Pickup ready in 4 hours | ✗ 不存在 |
| 3 | SPANX Kick Flare Jeans | Pickup ready in 4 hours | ✗ 不存在 |

三件 pickup-only 的服务端根本不返回，唯一能寄的正好是匿名视图的榜首。
**本工具的榜首 = 浏览器里第一件能直接网购的商品。**

即便如此还是加了两道保险：

- `require_shippable = true` 时，榜首会跳过明确不能直邮的商品。
  字段缺失（`shippable is None`）时不跳过 —— 信息不全不等于买不了，
  宁可多报也不要漏掉真正的好货。
- 每次抓取都记录本页有几件买不到（`n_not_shippable`）、榜首是否可直邮
  （`primary_shippable`）。`status` 里能看到累计情况，用来长期验证上面的结论。

## macOS 通知的两种样式

```toml
[notify.macos]
style = "corner"   # 或 "center"
```

| | `corner`（默认） | `center` |
|---|---|---|
| 实现 | `display notification` | `display alert` 模态窗口 |
| 位置 | 主屏右上角 | 屏幕正中，**无法指定位置或屏幕** |
| 会不会自动消失 | **由 macOS 设置决定** | 一直停着直到点掉 |
| 多屏 | 跟随主屏（系统设置 → 显示器 → 排列，菜单栏在哪块屏就显示在哪） | 跟随当前活跃屏 |
| 同轮多条 | 合并成一条 | 合并进同一个窗口 |
| 被下一条替换 | 否，系统自行堆叠 | 是 |

### corner 样式要常驻，得改系统设置

代码控制不了通知是横幅（几秒消失）还是常驻。要让它停着不走：

**系统设置 → 通知 → 脚本编辑器 → 提醒样式 → 选「持续提醒」**

（osascript 发的通知挂在「脚本编辑器」名下。）

### 想换一块屏显示

系统通知只出现在**主屏**。系统设置 → 显示器 → 排列，把菜单栏拖到你想要的那块屏。

### 试过但放弃的方案

想过用 tkinter 自己画一个可精确定位到任意屏角落、且能被替换的窗口 ——
本机 Homebrew Python 不带 `_tkinter`，装它就破坏了零依赖，不值当。

## center 样式的实现细节

实现是把 `osascript display alert` 放到后台进程里，PID 记在 `.state/alert.pid`；
下次告警先 kill 掉它（窗口随之消失）再弹新的。杀之前会用 `ps` 确认那个 PID
真的是 osascript —— PID 会被系统复用，照着文件里的号码直接杀有可能误伤。

### `start_new_session=True` 是必须的，不是优化

**launchd 任务结束时会回收整个进程组**，普通子进程会被连带杀掉。表现是：
邮件正常送达，但弹窗一闪就没（甚至根本没渲染出来）。实测对照：

```
普通子进程        ✗ launchd 任务退出后被杀
start_new_session ✓ 存活，ppid 变成 1（被 init 收养）
```

从终端手动跑不会出这个问题（shell 不这么回收），所以**只在 launchd 下复现** ——
这类 bug 用手动测试抓不到，必须走真实调度路径验证。

### 不要只看 Popen 有没有抛异常

`Popen` 成功只代表进程起来了，osascript 可能因为没有 GUI 会话、权限不足等
立刻退出。所以发出后会等 0.4 秒确认进程还活着，死了就把 stderr 作为错误返回 ——
否则 `event.notified` 会谎报成功，排查时完全误导。

## 告警正文里的状态提示

榜首告警会把下单前该知道的事直接写在通知里：

```
榜首: MAX STUDIO / Floral Wreath Print Tiered Cotton Blend Maxi Skirt | $7.49 (was $128.00) 94% off
      可直邮仅剩 1 件 · 刚降价 · 库存紧张 · 不可退
```

| 告警类型 | 触发 |
|---|---|
| `PRIMARY_CHANGED` | 榜首换人 |
| `PRIMARY_PRICE_DROP` | 榜首没换但降价 |
| `TOP_N_NEW` | 前 N 名出现新面孔（默认 N=5）|
| `NEW_ARRIVALS` | 出现历史没见过的商品（默认关）|
| `COUNT_UP` | 总数暴涨（默认关，噪声大）|

| 提示 | 来源 | 含义 |
|---|---|---|
| `⚠ 仅门店自提，网上买不了` | `isPickAndNotShip` / `shipQuantity` | 不该出现，出现了说明视图变了 |
| `可直邮仅剩 N 件` | `availability.shipQuantity` ≤ 3 | 手慢就没了 |
| `刚降价` | `enticementTags: NEW_MARKDOWN` | Rack 刚重新定过价 |
| `库存紧张` | `inventoryWarning: low-inventory` | — |
| `不可退` | `enticementTags: FINAL_SALE` | Final sale |

## NEW_MARKDOWN 是最好的"重新定价"探针

`enticementTags` 里的 `NEW_MARKDOWN` 标记"刚降过价的商品"。实测第 1 页
72 件里按折扣排有 20 件带这个标、按最新排有 40 件。Rack 每做一轮降价这个数
就会跳，比总数差值直接得多：

```bash
python3 run.py analyze --metric new_markdown
```

## 被挡情况的记录与分析

```bash
python3 run.py blocks
```

每次抓取都记录 HTTP 状态码、实际页数、以及（异常时）判定原因。
**重点是看异常率随时间的变化** —— 10-31 把轮询从 96 次/天提到 168 次/天，
如果异常率跟着涨就说明触发了对方限流，需要退回去。

当前基线（10-22 ~ 10-31，1377 次抓取）：

```
正常      98.77%
残缺结果   1.02%   （14 次，total 恒为 400）
彻底失败   0.22%   （3 次，全是本地 DNS / 连接重置）
最长连续异常 2 次   → 孤立事件，不像被封
```

残缺结果的 `total` **每次都精确等于 400**，说明是服务端的固定兜底集，
不是随机截断。时间分布散（3h 到 22h 都有），也不聚集在高频时段。

## 站点会偶发返回残缺结果集

实测抓到过一次：`resultCount` 从 6950 骤降到 400，商品全换，下一次又恢复正常。
原因不明（疑似后端降级或限流）。如果不拦，它会制造假的"榜首换人"告警，
并往 `item` 表灌几十件假商品，把之后的上新判断全部带歪。

所以每次抓取都会和最近 10 次可信结果的中位数比对，偏离超过 50%
（`anomaly_max_deviation`）就标记为 `anomalous`：

- 不告警
- 不写 `item` 表
- 不作为下一次的 diff 基线
- 但仍然留痕，`status` 里能看到发生了几次

## 阈值校准

`calibrate` 仍可用于 `count_up`（如果你要开它的话）：

```bash
python3 run.py calibrate   # 跑够两三天后执行
```

## 加一个新的被监控页面

1. 在 `pagewatch/watchers/` 下新建一个模块，继承 `Watcher`，实现 `observe()`
   返回 `Observation`。
2. 在 `pagewatch/watchers/__init__.py` 的 `REGISTRY` 里注册。
3. 在 `config.toml` 里加一段 `[[watcher]]`。

存储、diff、告警、调度都不用碰。

## 数据结构

| 表 | 内容 |
|---|---|
| `snapshot` | 每次抓取一行：总数、差值、榜首、ID 集合、耗时、流量。失败也留痕 |
| `item` | 见过的每个条目：首次/最后见到时间、历史最好名次、价格 |
| `event` | 产生过的每条告警 |

## 测试

```bash
python3 -m unittest discover -s tests -t .
```

## Nordstrom Rack 解析原理

页面是服务端渲染的 React 应用，整个状态以 `window.__INITIAL_CONFIG__ = {...}`
内嵌在 HTML 里（约 1.9MB JSON）。抓下来做一次括号配对扫描把 JSON 抠出来即可，
不需要浏览器。

用到的字段：

| 字段 | JSON 路径 |
|---|---|
| 结果总数（= 页面标题上的 "N items"） | `productResults.query.resultCount` |
| 能否直邮 | `...propositions[0].availability.isPickAndNotShip` / `.shipQuantity` |
| 库存提示 | `productsById[id].inventoryWarning` |
| 页面排序 | `productResults.productUi.byId[].sourceIndex` |
| 商品信息 | `productResults.productsById[id]` |
| 站点数据快照时间 | `...propositions[0].asOf` |

筛选参数（都可以直接拼在 URL 上）：

| 作用 | 参数 |
|---|---|
| 只看女装 | `&filterByGenderAge=women`（另有 men / girls / boys / unisex）|
| 按折扣筛 | Discount 分面，见 `query.aggregates` |
| 排序 | `&sort=` 取 `PercentOff` / `Newest` / `PriceLowToHigh` / `PriceHighToLow` / `CustomerRating` / `Featured` |
| 翻页 | `&page=N` |

注意 `postalCodeAvailability` / `preferredStore` / `preferredPostalCode` 实测
**对结果数量没有任何影响**（带不带都是同一个数），留着只是为了和你浏览器里的
URL 保持一致。

开 gzip 后单页传输约 **250KB / 1.2 秒**（不开是 2.1MB）。两个 watcher 各一页、
每 15 分钟一次，一天约 85MB。

> 页面改版时解析会直接抛 `ParseError` 并记入 `snapshot.error`，不会静默返回
> 错误数据。`status` 里的失败计数就是在盯这个。
