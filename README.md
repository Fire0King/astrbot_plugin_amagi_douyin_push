# astrbot_plugin_amagi_douyin_push 🎬

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，用于**监控抖音用户的视频更新和直播状态**，实时推送开播提醒与视频发布通知到指定会话。

数据抓取基于 [amagi](https://github.com/ikenxuan/amagi)（Node.js SDK，以 git 子模块形式随插件部署）：
插件启动时自动拉起常驻的 amagi HTTP 服务，Python 侧通过本机端口调用抖音网页版接口，**无需单独部署数据服务**。

> 📌 **订阅语义**：视频与直播订阅**均锚定主播用户（sec_uid / 用户主页 URL）**。
> 直播监控无法仅凭直播间房间号解析主播身份，请使用 `/dy_sub live <主播主页URL>`。

---

## ✨ 功能特性

- 📹 **视频监控** — 轮询检测订阅用户的抖音视频发布，有新作品时自动推送
- 🔴 **直播监控** — 按用户轮询主播是否开播，开播/下播时自动推送通知
- 👥 **@全体成员** — 支持开播或发视频时 @全体成员（需管理员权限）
- 🔄 **更新订阅** — 重复订阅同一用户可直接更新 @全体 标志
- 📋 **订阅管理** — 列表查看、取消订阅、全局管理查看
- 🔍 **用户查询** — 查看抖音用户信息（昵称、粉丝数等）
- 🧩 **amagi 数据桥接** — 插件自动拉起常驻 Node 进程运行 amagi HTTP 服务，本地调用抖音网页版接口

---

## 📦 安装

### 前置要求

| 软件 | 说明 |
|------|------|
| Node.js ≥ 18 | amagi 运行环境 |
| pnpm | amagi 依赖安装/构建（`npm i -g pnpm` 或 `corepack enable` 后自带） |
| Python ≥ 3.9 | AstrBot 环境 |

### 1. 克隆插件

```bash
cd AstrBot/data/plugins
git clone --recurse-submodules https://github.com/Fire0King/astrbot_plugin_amagi_douyin_push.git
cd astrbot_plugin_amagi_douyin_push
```

> ⚠️ 必须带 `--recurse-submodules`。若已克隆但 `amagi/` 是空目录，补执行：
> ```bash
> git submodule update --init --recursive
> ```

### 2. 构建 amagi（一次性）

插件首次加载会自动尝试执行以下命令，也可以手动先执行以免插件启动等待：

```bash
cd amagi
pnpm install
pnpm --filter @ikenxuan/amagi run build
cd ..
```

构建产物位于 `amagi/packages/core/dist`，插件启动时会检查该目录，缺失即自动构建。

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
| `amagi_dir` | amagi 目录（默认插件目录下 `amagi/`，一般无需修改） |
| `node_path` | node 可执行文件路径（留空自动从 PATH 查找） |
| `pnpm_path` | pnpm 可执行文件路径（留空自动从 PATH 查找） |
| `rai` | 图片卡片渲染开关（需 AstrBot HTML 渲染支持） |

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
| `/dy_bridge_restart` | 重启 amagi 桥接（改 Cookie/端口后执行） |
| `/dy_status` | 查看插件与桥接运行状态 |

---

## 🔧 项目结构

```
astrbot_plugin_amagi_douyin_push/
├── main.py                  # 插件入口 & 命令
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置定义
├── requirements.txt         # Python 依赖
├── amagi/                   # amagi 子模块 (Node.js SDK)
├── amagi_bridge/
│   └── server.mjs           # Node 桥接入口（启动 amagi HTTP 服务）
├── core/
│   ├── douyin.py            # 抖音数据适配（profile/作品/直播快照）
│   ├── models.py            # 数据模型
│   ├── data_manager.py      # 数据持久化
│   └── utils.py             # 工具函数
└── services/
    ├── amagi_service.py     # 桥接进程管理 + HTTP 调用
    ├── listener.py          # 后台轮询监听
    ├── subscription_service.py  # 订阅管理
    └── renderer.py          # 消息渲染
```

---

## 🚨 常见问题

### Q1: 提示 `amagi 目录无效 (缺少 package.json)` 或“未找到 amagi 目录”

**原因**：克隆时没加 `--recurse-submodules`，`amagi/` 只是个空目录。

**解决**：
```bash
cd AstrBot/data/plugins/astrbot_plugin_amagi_douyin_push
git submodule update --init --recursive
```
若提示 `amagi` 不是已登记的子模块，则手动添加：
```bash
git submodule add https://github.com/ikenxuan/amagi.git amagi
git submodule update --init --recursive
```

### Q2: 提示 amagi 未构建 / 启动失败，日志里有 pnpm 报错

**原因**：amagi 子模块未安装依赖或未构建。

**解决**：
```bash
cd AstrBot/data/plugins/astrbot_plugin_amagi_douyin_push/amagi
pnpm install
pnpm --filter @ikenxuan/amagi run build
cd ..
```
然后在 WebUI 里**重载插件**（或执行 `/dy_bridge_restart`）。

### Q3: 端口被占用（桥接一直未就绪）

在插件设置里修改 `amagi_port`（如 48212）后重载插件。若残留了旧的 node 进程，请先结束它再重载。

### Q4: 提示“需要 Node.js/pnpm”

请安装 [Node.js](https://nodejs.org/) ≥ 18，并执行 `npm i -g pnpm`（或 `corepack enable && corepack prepare pnpm@latest --activate`）后重载插件。

### Q5: 为什么直播订阅不能用直播间房间号？

amagi 的直播间接口要求同时提供内部 `room_id` 与 `web_rid`，无法从一串房间号可靠解析出主播身份。
因此直播监控改为按主播用户（sec_uid）轮询，请使用 `/dy_sub live <主播主页URL>`。

### Q6: 开播/下播判断是否精确？

直播状态取自抖音用户主页接口（`user.live_status`，或个别账号附带的 `live_room`），判定常量集中在 `core/douyin.py` 顶部并有注释。
若你实测发现状态值与预期不符（不同账号/时间点抖音可能调整字段），只需调整 `USER_LIVE_STATUS_ON` / `ROOM_STATUS_LIVE` 两个常量或判定顺序即可。

---

## 📄 许可证

本项目基于 MIT 许可证开源（amagi 子模块为 GPL-3.0，仅作本地运行依赖）。

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 机器人框架
- [amagi](https://github.com/ikenxuan/amagi) — 抖音等平台 Node.js 数据 SDK
- [astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) — 参考实现的 B站推送插件
