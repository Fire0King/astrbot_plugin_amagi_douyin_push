# astrbot_plugin_amagi_douyin_push 🎬

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，用于**监控抖音用户的视频更新和直播状态**，实时推送开播提醒与视频发布通知到指定会话。

数据抓取基于 [amagi](https://github.com/ikenxuan/amagi)（Node.js SDK）。**无需 git 子模块、无需 pnpm、无需手动构建**：
插件首次启动时自动用 npm 安装官方包 `@ikenxuan/amagi`（该包已内置构建产物）到插件目录下的 `.amagi/`，
随后拉起常驻的 amagi HTTP 服务，Python 侧通过本机端口调用抖音网页版接口。

> 📌 **订阅语义**：视频与直播订阅**均锚定主播用户（sec_uid / 用户主页 URL）**。
> 直播监控无法仅凭直播间房间号解析主播身份，请使用 `/dy_sub live <主播主页URL>`。

---

## ✨ 功能特性

- 📹 **视频监控** — 轮询检测订阅用户的抖音视频发布，有新作品时自动推送
- 🔴 **直播监控** — 按用户轮询主播是否开播，开播/下播时自动推送通知
- 👥 **@全体成员** — 支持开播或发视频时 @全体成员（发送前会**实际校验**机器人权限与剩余次数）
- 🔄 **更新订阅** — 重复订阅同一用户可直接更新 @全体 标志
- 📋 **订阅管理** — 列表查看、取消订阅、全局管理查看
- 🔍 **用户查询** — 查看抖音用户信息（昵称、粉丝数等）
- 🖼️ **图片卡片推送** — 可选（`rai`）用模板渲染视频/直播卡片；渲染结果会**校验并重试**，
  失败自动降级为「纯文本 + 抖音封面原图」
- ⚡ **本地渲染（默认）** — `card_engine=local` 时用 **Pillow 自绘卡片**：不依赖任何外部渲染服务、
  不吃内存（~30MB）、单张约 **0.2 秒**（远程文转图要 2~5 秒），小内存机器也能放心开
- 🖨️ **尺寸与清晰度对齐 B 站插件** — 输出倍率 `card_scale`（默认 4）→ 成品约 **1616px 宽**
  （B 站插件约 1440px）；文字按倍率矢量重绘，放大后依然锐利；竖版封面两侧用**封面自身的模糊图**铺底
- 📐 **尺寸自适应** — 卡片超出平台图片限制时自动改以文件（File）发送，不再被平台拒收
- ♻️ **渲染缓存** — 同一视频被多个会话订阅时只渲染一次；发送失败自动降级重发，推送不丢
- 🧩 **amagi 数据桥接** — 插件自动拉起常驻 Node 进程运行 amagi HTTP 服务，本地调用抖音网页版接口

---

## 📦 安装

### 前置要求

| 软件 | 说明 |
|------|------|
| Node.js ≥ 18 | amagi 运行环境（自带 npm，用于自动安装 amagi） |
| Python ≥ 3.9 | AstrBot 环境 |

### 1. 在 AstrBot WebUI 中安装

插件市场搜索安装，或用仓库链接安装：

```
https://github.com/Fire0King/astrbot_plugin_amagi_douyin_push
```

AstrBot 会把插件放进 `AstrBot/data/plugins/astrbot_plugin_amagi_douyin_push/`。

也可以手动克隆（**不需要** `--recurse-submodules`）：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Fire0King/astrbot_plugin_amagi_douyin_push.git
```

### 2. amagi 运行时（自动，无需操作）

插件启动时会自动完成以下流程，**通常不需要你手动做任何事**：

1. 检查插件目录下 `.amagi/` 是否已有 amagi 运行时
2. 没有则执行 `npm install @ikenxuan/amagi@6.6.0`（默认走 npmmirror 源）
3. 定位 `dist/default/index.mjs` 并拉起桥接进程

安装日志与桥接日志位于 AstrBot 数据目录：

```
AstrBot/data/plugin_data/astrbot_plugin_amagi_douyin_push/amagi_bridge/
├── provision_*.log   # npm 安装日志
├── bridge.out.log    # 桥接 stdout
└── bridge.err.log    # 桥接 stderr
```

> 💡 如果自动安装失败（例如网络不通），可手动执行一次，之后插件会直接复用：
> ```bash
> cd AstrBot/data/plugins/astrbot_plugin_amagi_douyin_push
> mkdir .amagi && cd .amagi
> npm install @ikenxuan/amagi@6.6.0 --registry https://registry.npmmirror.com
> ```
> 完成后在 WebUI 里重载插件（或执行 `/dy_bridge_restart`）。

> 💡 如果你已有一份 amagi 源码仓库，也可以打开插件配置里的 `amagi_dir` 指向它，
> 插件会自动识别 `packages/core/dist/default/index.mjs`（需自行 `pnpm build` 过）。

### 3. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 4. 配置 Cookie

在 AstrBot WebUI → 插件设置 → `astrbot_plugin_amagi_douyin_push` 中填写：

| 配置项 | 说明 |
|-------|------|
| `douyin_cookie` | 抖音网页版 Cookie（必需） |
| `douyin_live_cookie` | 备用 Cookie（可选，留空时回退使用 `douyin_cookie`） |
| `poll_interval` | 轮询间隔（秒），默认 60，最小 10 |
| `enable_live_monitor` | 是否开启直播监控，默认开启 |
| `amagi_port` | 桥接监听端口（默认 48211，本机 127.0.0.1） |
| `amagi_dir` | amagi 运行时目录（默认 `.amagi/`，一般无需修改） |
| `amagi_version` | amagi 版本（默认 6.6.0，一般无需修改） |
| `npm_registry` | npm 源（默认 npmmirror，国内建议保持） |
| `node_path` / `npm_path` | node / npm 可执行文件路径（留空自动查找） |
| `rai` | 图片卡片推送开关（渲染引擎见 `card_engine`） |
| `card_engine` | 卡片渲染引擎：`local`=Pillow 本地自绘（默认，不依赖外部服务）；`html`=AstrBot 文转图（远程 t2i，可能不稳定） |
| `card_quality` | 本地卡片 JPEG 质量，默认 88（50-95） |
| `card_scale` | 本地卡片输出倍率，默认 4 → 成品约 1616px 宽（与 B 站插件同级）；嫌大/嫌小可调 1~6 |
| `font_path` | 本地卡片的中文字体路径，留空自动探测（容器内一般无需填写） |
| `render_cache_limit` | 渲染结果缓存条数上限，默认 32 |
| `reconnect_silent` | 重连静默：距上次成功推送超过 6 小时（断电/断网/长期失败）时，恢复后先静默一个轮询周期，避免积压更新一次性刷屏，默认关闭 |

#### 获取 Cookie

1. 用浏览器打开 [www.douyin.com](https://www.douyin.com) 并登录
2. 按 `F12` 打开开发者工具 → `Application` → `Cookies`
3. 全选所有 Cookie 并复制完整字符串
4. 粘贴到插件配置的 `douyin_cookie` 字段
5. **保存配置后重载插件**（或管理员执行 `/dy_bridge_restart`）

### 5. 重载插件

在 WebUI 插件管理处点击「重载插件」。

---

## 📖 命令说明

### 用户命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/dy_sub <URL/sec_uid> [选项]` | 订阅视频**和**直播 | `/dy_sub https://www.douyin.com/user/MS4wLjABAAAA...` |
| `/dy_sub video <URL/sec_uid> [选项]` | 仅订阅视频 | `/dy_sub video MS4wLjABAAAA...` |
| `/dy_sub live <URL/sec_uid> [选项]` | 仅订阅直播（按主播用户） | `/dy_sub live MS4wLjABAAAA... live_atall` |
| `/dy_unsub <ID> [video/live]` | 取消订阅 | `/dy_unsub MS4wLjABAAAA... video` |
| `/dy_sub_list` | 列出当前会话订阅 | `/dy_sub_list` |
| `/dy_info <URL/sec_uid>` | 查询抖音用户信息 | `/dy_info MS4wLjABAAAA...` |
| `/dy_test [live] <URL/sec_uid>` | 测试推送（不保存订阅） | `/dy_test live MS4wLjABAAAA...` |

### @全体 选项

| 选项 | 效果 | 权限 |
|------|------|------|
| `at_all` | 发视频**和**开播时 @全体成员 | 管理员 |
| `live_atall` | **仅**开播时 @全体成员 | 管理员 |
| _(不填)_ | 不 @全体成员 | — |

> **提示**：重复订阅同一用户可直接更新 @全体 标志，不会报“已存在”。

#### 示例

```bash
# 订阅用户（视频+直播），视频/开播时 @全体
/dy_sub https://www.douyin.com/user/MS4wLjABAAAA... at_all

# 仅订阅视频
/dy_sub video MS4wLjABAAAA...

# 仅订阅直播，开播 @全体
/dy_sub live MS4wLjABAAAA... live_atall

# 更新已有订阅（取消 @全体）
/dy_sub MS4wLjABAAAA...

# 查看用户信息
/dy_info MS4wLjABAAAA...
```

### 管理员命令

| 命令 | 说明 |
|------|------|
| `/dy_clear` | 清空当前会话所有订阅 |
| `/dy_global_list` | 查看所有会话的订阅 |
| `/dy_global_unsub <UMO> <UID>` | 删除指定会话指定用户的订阅 |
| `/dy_bridge_restart` | 重启 amagi 桥接（改 Cookie/端口后执行，会重新检查 amagi 运行时） |
| `/dy_status` | 查看插件与桥接运行状态（含推送状态/上次推送成功时间/渲染缓存） |

---

## 🔧 项目结构

```
astrbot_plugin_amagi_douyin_push/
├── main.py                  # 插件入口 & 命令
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置定义
├── requirements.txt         # Python 依赖
├── .amagi/                  # amagi 运行时（自动创建，npm 安装，不入库）
├── amagi_bridge/
│   └── server.mjs           # Node 桥接入口（启动 amagi HTTP 服务）
├── core/
│   ├── douyin.py            # 抖音数据适配（profile/作品/直播快照）
│   ├── models.py            # 数据模型
│   ├── data_manager.py      # 数据持久化
│   └── utils.py             # 工具函数
└── services/
    ├── amagi_service.py     # 桥接进程管理 + HTTP 调用
    ├── listener.py          # 后台轮询监听 + 消息链构建
    ├── dispatcher.py        # 通知发送出口（静默模式/发送结果/成功回调）
    ├── subscription_service.py  # 订阅管理
    ├── renderer.py          # 消息渲染（引擎分派：local / html）
    └── card_renderer.py     # Pillow 本地卡片自绘（不依赖 t2i）
```

---

## 🚨 常见问题

### Q1: 提示“未找到 amagi 运行时 / 未检测到 npm”

**原因**：插件会自动用 npm 安装 amagi；若系统没装 Node.js（或 AstrBot 进程的 PATH 里找不到 node/npm），
自动安装就无法进行。

**解决**：
1. 安装 [Node.js](https://nodejs.org/) ≥ 18（安装包自带 npm），**重启 AstrBot** 使其继承新的 PATH；
2. 若 node 装在非标准位置，可在插件配置里填写 `node_path` 与 `npm_path`；
3. 或手动安装一次，插件之后会直接复用：
   ```bash
   cd AstrBot/data/plugins/astrbot_plugin_amagi_douyin_push
   mkdir .amagi && cd .amagi
   npm install @ikenxuan/amagi@6.6.0 --registry https://registry.npmmirror.com
   ```
   然后在 WebUI 里**重载插件**（或执行 `/dy_bridge_restart`）。

> ✅ 旧版本要求 `git clone --recurse-submodules` 并手动 `pnpm build`，**现已完全不需要**。
> 如果你是从旧版本升级过来的，可以删掉遗留的 `amagi/` 目录，插件会改用 `.amagi/`。

### Q2: npm 安装失败（网络不通 / 超时）

**原因**：访问 npm 源失败。

**解决**：插件默认使用国内可直连的 `https://registry.npmmirror.com`。
若你的环境需要走代理或其他镜像，改插件配置里的 `npm_registry` 即可，例如：

| 场景 | `npm_registry` 建议值 |
|------|----------------------|
| 国内直连（默认） | `https://registry.npmmirror.com` |
| 官方源 / 有代理 | `https://registry.npmjs.org` |
| 私有镜像 | 你的镜像地址 |

改完执行 `/dy_bridge_restart` 重试（该命令会强制重新检查并安装）。

### Q3: 端口被占用（桥接一直未就绪）

在插件设置里修改 `amagi_port`（如 48212）后重载插件。若残留了旧的 node 进程，请先结束它再重载。

### Q4: 为什么不用 git 子模块 / 不需要 pnpm 了？

AstrBot WebUI 通过仓库链接安装插件时不会拉取 git 子模块，旧方案会导致 `amagi/` 目录为空、插件无法启动。
而 amagi 官方 npm 包发布时**已内置构建产物**（`dist/*`），所以现在改为运行时 `npm install @ikenxuan/amagi`，
既不需要子模块，也不需要 pnpm 与本地构建。

### Q5: 为什么直播订阅不能用直播间房间号？

amagi 的直播间接口要求同时提供内部 `room_id` 与 `web_rid`，无法从一串房间号可靠解析出主播身份。
因此直播监控改为按主播用户（sec_uid）轮询，请使用 `/dy_sub live <主播主页URL>`。

### Q6: 订阅了但对方更新了没有推送？

按下面顺序排查（管理员执行 `/dy_status` 可一次看到全部关键状态）：

1. **看「监听服务」是否为 🟢 运行中**
   若为 🔴 已停止，说明后台轮询没在跑 —— 这会导致**日志里几乎没有任何输出**。
   v1.0.2 已修复导致该现象的缺陷（订阅/取消订阅后重启监听的任务竞态），请升级后重载插件。
2. **看「上次视频扫描」是否在持续推进**
   若长时间停在「尚未扫描」或不再更新，说明轮询卡住或桥接不可用。
3. **看「amagi 桥接」与「amagi 运行时」**
   桥接未就绪时插件会每隔 5 分钟打印一条 `amagi 桥接未就绪, 本轮跳过检查 ...` 警告，
   里面带有具体原因（Cookie 未配置 / Node 未安装 / npm 安装失败等）。
4. **首次订阅只记基线，不推送（视频与直播都是）**
   - 视频：订阅后的第一次扫描只记录「当前最新视频」作为基线并写入日志：
     `首次记录用户 xxx 的最新视频: yyy (仅记录基线, 不推送; 之后的更新才会推送)`。
     之后该用户**新发布**的作品才会推送。若你订阅之后对方一直没发新作品，就不会有消息。
   - 直播：第一次扫描只记录当前是否在播，日志为
     `首次记录用户 xxx 的直播状态: 直播中/未开播 (仅记录基线, 不推送; 之后的状态变化才会推送)`。
     所以**订阅时主播正在直播不会立刻收到「开播啦」**（那是误报），要等下一次真正开播。
   - 从旧版本升级后第一次扫描同样只补基线、不推送（防止刷屏）。
5. **确认推送目标会话正常**
   推送失败会在日志中输出 `推送视频消息失败: ...` / `推送直播消息失败: ...`，例如平台侧风控或权限问题。

### Q7: 主播有置顶作品，会不会误推或漏推？

不会。抖音用户作品列表会把**置顶作品排在最前面**，而置顶作品往往是旧作，
因此插件**不依赖列表顺序**判断新旧，而是：

- 以作品自带的 `create_time`（发布时间）为准：只有「发布时间不早于基线」的作品才算新作品；
- 用 `is_top`（1=置顶）识别置顶作品，列表首位是置顶旧作时不会被误当成新作品；
- 置顶作品里如果有**新发布**的（主播发完就置顶），仍然会正常推送 —— 因为它同时满足时间条件。

首次订阅时基线取「发布时间最新」的作品，而不是列表首位（首位可能是置顶旧作）。
旧版本升级后第一次扫描只补基线、不推送。

### Q8: 开播/下播判断是否精确？

直播状态取自抖音用户主页接口，优先级为 `user.live_room.status`（2=直播中/4=未开播）> `user.live_status`（1=直播中）。
判定常量集中在 `core/douyin.py` 顶部并有注释。

- 用管理员命令 `/dy_test live <用户>` 可直接查看**本次判定依据**（用了哪个字段、原始值是多少），
  便于对照你账号实际所见来校准。
- 若某个响应里两个字段都缺失或值无法解析，插件会视为「状态未知」并跳过本轮，
  **不会**误报成「已下播」（避免下播↔开播来回刷屏）。
- 若你实测发现状态值与预期不符（不同账号/时间点抖音可能调整字段），只需调整
  `USER_LIVE_STATUS_ON` / `ROOM_STATUS_LIVE` 两个常量或上面的判定优先级即可。

### Q9: 推送时出现 `rich media transfer failed` (retcode 1200)，或推送后群里什么都没收到？

**原因**：这不是抖音数据获取的问题，而是**图片这一跳**发不出去。开启 `rai`（图片卡片）后，
插件把卡片图交给 AstrBot，AstrBot 以 base64 转给协议端（NapCat），再由 QQ 上传到腾讯富媒体服务器；
上传失败时 OneBot 返回 `retcode 1200`（`rich media transfer failed`）。
由于图片与链接在**同一条消息**里，失败会让**整条推送**一起丢失。

**插件已经内置的三层防护**（一般无需人工干预）：

1. **渲染结果校验 + 重试**：拿到图片路径不等于拿到图片 —— 上游 t2i 服务可能返回空内容或错误页
   （典型表现：`data/temp/io_temp_img_*.jpg` 只有 0 字节）。插件要求文件存在、体积 > 4KB、
   且能被 PIL 完整解码，最多尝试 3 次（间隔 2 秒），日志会打印
   `卡片渲染结果无效 (尝试 1/3)` / `渲染图片失败 (尝试 1/3)`。
2. **降级重发**：渲染彻底失败、或图片发送失败时，自动改用「纯文本 + **抖音封面原图**」重发
   （封面由协议端自行下载，不经过 t2i 渲染服务），保证通知不丢、图也还在。
3. **尺寸自适应**：卡片超出平台图片限制（体积 > 10MB、高度 > 25000；Telegram 另有
   `宽+高 ≤ 10000` 与长宽比 ≤ 20 的限制）时，自动改为**文件**发送，而不是被平台拒收。

**如果推送仍然失败**，按下面排查：

1. 前端日志：日志里若出现 `渲染图片失败: 已尝试 3 次`，说明 AstrBot 的文转图（t2i）服务本身不可用
   （常见于远程 t2i 端点返回空内容）——这是 AstrBot 侧的问题，可检查/更换其文转图端点，
   或[自部署 t2i 服务](https://docs.astrbot.app/others/self-host-t2i.html)；在此之前把插件的 `rai` 关掉即可；
2. 协议端：用一张几十 KB 的小图让机器人发到同一会话做对照。小图也失败说明问题在协议端/账号侧
   （重启 NapCat 重新登录、把 NapCat 与 QQ 升级到匹配版本、换个会话排除群级限制、确认未被风控）；
3. 用 `/dy_status` 查看「推送状态」（是否处于重连静默）与「上次推送成功」时间，
   确认通知是否只是被静默模式丢弃。

### Q10: 日志每 60 秒重复刷 `已启动 amagi 桥接 (pid=...)` / `amagi 桥接就绪`？

**原因**：这两行**只在"重新拉起了一个 node 进程"时打印**，桥接健康时整个生命周期只出现一次。
每 60 秒（= `poll_interval`）成对出现且 **pid 每次都变**，说明插件认定桥接"没在运行"，于是每个
轮询周期都重新拉起一个 —— 而真正在干活的是**上一个进程没退干净留下的残留进程**：

```
残留进程占着 48211
  → 插件新拉起的进程 listen 失败, 但旧版 server.mjs 会**无条件打印 ready** 后静默退出
  → 插件的健康检查被残留进程的响应骗过(握手成功 → 判定"就绪")
  → 下一轮发现子进程已死 → 再拉一个 …… 死循环(全程不报错)
```

**判断方法**（在宿主机执行）：

```bash
nsenter -t $(docker inspect -f '{{.State.Pid}}' astrbot) -n ss -lptnp | grep 48211   # 谁在监听
docker exec astrbot grep -c ready /AstrBot/data/plugin_data/astrbot_plugin_amagi_douyin_push/amagi_bridge/bridge.out.log
docker logs -t astrbot | grep -c "已启动 amagi 桥接"
```

若监听者的 pid **不在**日志里那串 pid 中，就是残留进程。

**解决**：

1. 执行 `/dy_bridge_restart`（v1.1.1 起会自动清理残留进程后重启）；
2. 或手动结束残留进程：`kill $(nsenter -t <容器pid> -n ss -lptnp | awk '/48211/{match($0,/pid=[0-9]+/);print substr($0,RSTART+4,RLENGTH-4)}')`，
   等一个轮询周期，插件会拉起一个能正常绑定的桥接（本次循环随即停止）；
3. v1.1.1 起 `amagi_bridge/server.mjs` 会**先做端口预检**并在监听失败时**大声报错退出**，
   不再出现"假就绪 + 静默自杀"；插件侧也会检测到"端口已有监听但不是我拉起的进程"并**直接复用**，
   不再盲目重复拉起。

### Q11: 图片卡片为什么默认改成"本地渲染"了？想用回 HTML 模板怎么办？

**原因**：AstrBot 的 `html_render` 走的是**远程文转图（t2i）服务**（官方端点部署在国外），
实测有两个问题：

- **会抖**：官方端点曾返回 `502 Bad Gateway` / `503 no available server`，而 AstrBot 的
  `download_image_by_url` **不校验 HTTP 状态码**，会把错误页当图片存下来 —— 表现为"渲染成功了但图是坏的"；
- **会慢**：单张卡片 2.4~4.7 秒（跨洋请求）。

而自部署 t2i 需要跑 Chromium（常驻+峰值 300~600MB），1.7G 内存的小机器**扛不住**（还可能触发
OOM Killer 把 NapCat/AstrBot 干掉）。

因此 v1.2.0 起默认 `card_engine=local`：用 Pillow 直接在插件进程内画卡片 ——
**不依赖任何外部服务、内存 ~30MB、单张约 40ms**。字体自动探测系统中文（容器内已有 `NotoSansCJK`），
找不到时可用 `font_path` 指定。

**想用回 HTML 模板**（例如你已自部署了 t2i 服务）：把 `card_engine` 改成 `html` 即可，
两条渲染路径都保留了校验/重试与失败降级。

**本地渲染的取舍**：版式是代码绘制的（圆角卡片 + 圆形头像 + 封面 contain 居中 + 统计图标），
观感与 HTML 模板接近但不完全相同；统计图标用矢量绘制而非 emoji（避免依赖 emoji 字体）。
**成品尺寸**由 `card_scale` 控制（默认 4 → 约 1616px 宽，和 B 站插件卡片同级；`1` 就是原来的 404px）。

---

## 📄 许可证

本项目基于 MIT 许可证开源（运行期自动安装的 amagi 为 GPL-3.0，仅作本地运行依赖，不随本项目分发）。

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 机器人框架
- [amagi](https://github.com/ikenxuan/amagi) — 抖音等平台 Node.js 数据 SDK
- [astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) — 参考实现的 B站推送插件
