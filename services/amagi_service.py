"""
amagi 桥接服务管理 (Python 侧)

职责:
  1. 定位 amagi 运行时; 缺失时用 npm 自动安装官方包 @ikenxuan/amagi
  2. 以常驻 Node 子进程方式启动 amagi_bridge/server.mjs (amagi 官方 HTTP 服务)
  3. 提供健康检查 / HTTP JSON 请求 / 重启 / 停止 / 状态查询

关于 amagi 运行时的获取方式 (不再使用 git 子模块):
  官方包 @ikenxuan/amagi 发布到 npm 时已内置构建产物 (dist/*),
  因此只需 `npm install` 即可, 无需 pnpm / 无需本地 build。
  AstrBot 通过 WebUI 安装插件时不会拉取 git 子模块, 这正是旧方案
  `amagi/` 目录为空的原因, 现改为运行时自动安装, 从根上避免该问题。

约定:
  - amagi 运行时目录: <plugin_dir>/.amagi  (可用 amagi_dir 配置覆盖)
  - 识别三种目录布局 (按优先级):
      1) 完整仓库:      <dir>/packages/core/dist/default/index.mjs
      2) npm 安装:      <dir>/node_modules/@ikenxuan/amagi/dist/default/index.mjs
      3) 裸包目录:      <dir>/dist/default/index.mjs
  - 桥接脚本:          <plugin_dir>/amagi_bridge/server.mjs
  - 桥接日志:          写入 AstrBot 数据目录下的 amagi_bridge/ 文件夹
  - HTTP 请求走 amagi 自带的 /api/douyin/* 路由, 响应包裹格式:
      {"success": true,  "code": 200, "message": "...", "data": {...}, ...}
      {"success": false, "code": 400, "message": "...", "error": {...}, ...}
"""

import asyncio
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from astrbot.api import logger
from astrbot.api.star import StarTools

# amagi 构建产物入口候选 (相对 amagi_dir, 按优先级排列)
_ENTRY_REL_CANDIDATES = (
    # 1) 完整仓库布局 (git clone 源码后本地构建)
    Path("packages", "core", "dist", "default", "index.mjs"),
    Path("packages", "core", "dist", "default", "index.cjs"),
    # 2) npm 安装布局 (npm install @ikenxuan/amagi)
    Path("node_modules", "@ikenxuan", "amagi", "dist", "default", "index.mjs"),
    Path("node_modules", "@ikenxuan", "amagi", "dist", "default", "index.cjs"),
    # 3) 裸包目录布局 (解压官方 tarball 到该目录)
    Path("dist", "default", "index.mjs"),
    Path("dist", "default", "index.cjs"),
)

# npm 包名与默认版本 (版本与 amagi 6.x 的 dist 入口结构对应)
_AMAGI_PKG = "@ikenxuan/amagi"
_DEFAULT_AMAGI_VERSION = "6.6.0"
# 默认 registry: 国内可直连的 npmmirror (官方源在国内常被阻断)
_DEFAULT_REGISTRY = "https://registry.npmmirror.com"

# 运行时目录下自动生成的 package.json (npm install 需要一个工程根)
_RUNTIME_PKG_JSON = {
    "name": "amagi-runtime",
    "private": True,
    "version": "1.0.0",
    "description": "该目录由 astrbot_plugin_amagi_douyin_push 自动生成, 用于存放 amagi 运行时",
}

# 桥接进程就绪后 stdout 输出的固定行, 用于校验
_READY_PREFIX = "[amagi-bridge] ready"
_ERROR_PREFIX = "[amagi-bridge] error"


class AmagiError(Exception):
    """amagi 调用相关错误 (桥接不可用 / HTTP 错误 / 数据异常)"""


class AmagiNotReady(AmagiError):
    """桥接尚未就绪或未配置 Cookie"""


class AmagiAPIError(AmagiError):
    """amagi 返回的业务错误 (含 code/message)"""


def _candidate_bin_dirs() -> list:
    """常见 Node.js 安装目录 (PATH 未生效时兜底, 例如 AstrBot 由服务方式启动)"""
    if os.name != "nt":
        return []
    dirs = []
    for env_key, tail in (
        ("ProgramFiles", ("nodejs",)),
        ("ProgramFiles(x86)", ("nodejs",)),
        ("LOCALAPPDATA", ("Programs", "nodejs")),
        ("APPDATA", ("npm",)),
        ("PROGRAMDATA", ("nvm",)),
    ):
        base = os.environ.get(env_key)
        if base:
            dirs.append(os.path.join(base, *tail))
    return [d for d in dirs if os.path.isdir(d)]


def _find_executable(name: str) -> str:
    """查找可执行文件: 支持绝对路径 / PATH / Windows 常见安装目录 (优先 .cmd/.exe)"""
    name = (name or "").strip()
    if not name:
        return ""
    if os.path.dirname(name):  # 调用方已给出路径
        return name

    exts = (".cmd", ".exe", ".bat") if os.name == "nt" else ("",)
    for directory in _candidate_bin_dirs():
        for ext in exts:
            path = os.path.join(directory, name + ext)
            if os.path.isfile(path):
                return path
    for ext in exts:
        found = shutil.which(name + ext)
        if found:
            return found
    return shutil.which(name) or name


class AmagiService:
    """管理 amagi 运行时 (npm 自动安装) 与常驻 HTTP 桥接进程"""

    def __init__(self, plugin_dir: Path, cfg: Dict[str, Any]):
        self.plugin_dir = Path(plugin_dir)
        self.cfg = cfg

        # amagi 运行时目录: 默认 <plugin_dir>/.amagi, 允许配置 amagi_dir 覆盖
        # (若你已有一份完整 amagi 源码仓库, 把 amagi_dir 指向它即可直接使用)
        configured = str(cfg.get("amagi_dir") or "").strip()
        if configured:
            self.amagi_dir = Path(configured).expanduser()
        else:
            self.amagi_dir = self.plugin_dir / ".amagi"
        self.server_script = self.plugin_dir / "amagi_bridge" / "server.mjs"

        self.host = "127.0.0.1"
        self.port = int(cfg.get("amagi_port", 48211) or 48211)

        # amagi 版本 (留空用默认) 与 npm 源 (留空用 npmmirror)
        self.amagi_version = str(cfg.get("amagi_version") or "").strip() or _DEFAULT_AMAGI_VERSION
        self.npm_registry = str(cfg.get("npm_registry") or "").strip() or _DEFAULT_REGISTRY

        # 数据/日志目录 (AstrBot 标准数据目录)
        data_dir = StarTools.get_data_dir(plugin_name="astrbot_plugin_amagi_douyin_push")
        self.log_dir = Path(data_dir) / "amagi_bridge"
        os.makedirs(self.log_dir, exist_ok=True)

        self.node_bin = _find_executable(str(cfg.get("node_path") or "node"))
        self.npm_bin = _find_executable(str(cfg.get("npm_path") or "npm"))
        self.amagi_entry: Optional[Path] = None   # 已定位到的 dist 入口文件

        self.cookie: str = ""
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._started: bool = False      # 是否已成功监听端口
        self._build_done: bool = False   # 本轮是否已检查/安装过 amagi
        self._build_ok: bool = False
        self._build_msg: str = "尚未检查 amagi 运行时"
        self._last_err: str = ""
        self._ready_attempted_at: float = 0.0
        self._warn_cookie: bool = False
        # 拉起桥接必须串行: 视频循环与直播循环是并发任务, 靠"3 秒时间窗"防重入并不可靠
        self._start_lock = asyncio.Lock()

    # ---------------- 状态 ----------------

    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.returncode is None)

    @property
    def started(self) -> bool:
        """桥接已成功监听端口并可通过 HTTP 访问"""
        return self._started

    @property
    def dist_ready(self) -> bool:
        """amagi 运行时入口是否已就绪"""
        return self._locate_entry() is not None

    @property
    def cookie_configured(self) -> bool:
        return bool(self.cookie)

    def set_cookie(self, cookie: str):
        self.cookie = (cookie or "").strip()

    def status_info(self) -> Dict[str, Any]:
        return {
            "built": self._build_ok,
            "build_msg": self._build_msg,
            "running": self.running,
            "started": self._started,
            "host": self.host,
            "port": self.port,
            "cookie_configured": self.cookie_configured,
            "node": self.node_bin,
            "npm": self.npm_bin,
            "amagi_dir": str(self.amagi_dir),
            "amagi_version": self.amagi_version,
            "entry": str(self.amagi_entry) if self.amagi_entry else "",
            "last_error": self._last_err,
        }

    # ---------------- 定位 / 安装 amagi 运行时 ----------------

    def _locate_entry(self) -> Optional[Path]:
        """在 amagi_dir 中按候选布局定位 amagi dist 入口"""
        for rel in _ENTRY_REL_CANDIDATES:
            path = self.amagi_dir / rel
            if path.is_file():
                return path
        return None

    def _npm_available(self) -> bool:
        return bool(self.npm_bin) and (
            os.path.isfile(self.npm_bin) or bool(shutil.which(self.npm_bin))
        )

    def _write_runtime_pkg(self):
        """写入 npm 安装所需的最小 package.json (已存在则不覆盖)"""
        pkg = self.amagi_dir / "package.json"
        if pkg.exists():
            return
        try:
            with open(pkg, "w", encoding="utf-8") as fp:
                json.dump(_RUNTIME_PKG_JSON, fp, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"写入运行时 package.json 失败: {e}")

    def _install_error_hint(self) -> str:
        return (
            f"npm 安装失败, 请检查网络与 npm 源 (当前: {self.npm_registry})。\n"
            f"也可手动执行: cd {self.amagi_dir} && npm install {_AMAGI_PKG}@{self.amagi_version} "
            f"--registry {self.npm_registry}"
        )

    def _prepare_sync(self) -> bool:
        """
        同步确保 amagi 运行时可用 (供线程中调用):

          1. amagi_dir 中已存在 dist 入口 (完整仓库 / npm 目录 / 裸包) → 直接使用
          2. 否则用 npm 安装官方包 @ikenxuan/amagi

        官方 npm 包发布时已内置构建产物 (dist/*), 因此无需 pnpm、无需本地 build,
        也不需要 git 子模块 —— 这正是 WebUI 安装插件时的可靠路径。
        """
        entry = self._locate_entry()
        if entry:
            self.amagi_entry = entry
            self._build_ok = True
            if not self._build_msg.startswith("amagi 已安装"):
                self._build_msg = "amagi 运行时已就绪"
            logger.info(f"amagi 运行时已就绪: {entry}")
            return True

        logger.info(
            f"未找到 amagi 运行时 ({self.amagi_dir}), 将执行 "
            f"npm install {_AMAGI_PKG}@{self.amagi_version} ..."
        )
        if not self._npm_available():
            self._build_msg = (
                "未检测到 npm, 无法自动安装 amagi。\n"
                "请安装 Node.js ≥ 18 (自带 npm) 后重载插件, "
                "或在插件配置中填写 npm_path / node_path。"
            )
            logger.error(self._build_msg)
            return False

        try:
            os.makedirs(self.amagi_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self._build_msg = f"无法创建 amagi 运行时目录 {self.amagi_dir}: {e}"
            logger.error(self._build_msg)
            return False
        self._write_runtime_pkg()

        args = [
            "install", f"{_AMAGI_PKG}@{self.amagi_version}",
            "--registry", self.npm_registry,
            "--no-audit", "--no-fund", "--no-package-lock",
            "--loglevel", "error",
        ]
        if not self._run_cmd(self.npm_bin, args, self.amagi_dir, step="npm install amagi"):
            self._build_msg = self._install_error_hint()
            return False

        entry = self._locate_entry()
        if not entry:
            self._build_msg = f"npm 安装结束, 但未找到 amagi 产物: {self.amagi_dir}"
            logger.error(self._build_msg)
            return False

        self.amagi_entry = entry
        self._build_ok = True
        self._build_msg = f"amagi 已自动安装 ({_AMAGI_PKG}@{self.amagi_version})"
        logger.info(f"amagi 安装完成: {entry}")
        return True

    def _run_cmd(self, cmd: str, args: list, cwd: Path, step: str,
                 timeout: int = 1800) -> bool:
        """运行命令, 输出重定向到日志文件 (避免管道限制)"""
        log_file = self.log_dir / f"provision_{int(time.time())}.log"

        argv = list(args)
        if os.name == "nt" and cmd.lower().endswith((".cmd", ".bat")):
            # .cmd / .bat 需经由 cmd.exe 执行
            argv = ["/c", cmd, *args]
            cmd = os.environ.get("COMSPEC", "cmd.exe")

        env = dict(os.environ)
        node_dir = os.path.dirname(self.node_bin or "")
        if node_dir and os.path.isdir(node_dir):
            # 保证 npm 能找到同目录下的 node (PATH 未包含 Node 目录时必需)
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")

        try:
            with open(log_file, "w", encoding="utf-8") as fp:
                proc = subprocess.run(
                    [cmd, *argv], cwd=str(cwd), env=env,
                    stdout=fp, stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
            if proc.returncode == 0:
                logger.info(f"{step} 成功 ({cmd})")
                return True
            tail = self._tail_log(log_file, 30)
            self._last_err = f"{step} 失败: {tail}"
            logger.error(self._last_err)
            return False
        except subprocess.TimeoutExpired:
            self._last_err = f"{step} 超时 ({timeout}s)"
            logger.error(self._last_err)
            return False
        except Exception as e:  # noqa: BLE001
            self._last_err = f"{step} 出错: {e}"
            logger.error(self._last_err)
            return False

    @staticmethod
    def _tail_log(path: Path, lines: int = 30) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return "".join(f.readlines()[-lines:]).strip()
        except Exception:  # noqa: BLE001
            return ""

    async def prepare(self, force: bool = False):
        """
        异步准备: 定位/安装 amagi 运行时在线程中执行, 避免阻塞事件循环。

        force=True 时忽略上一次的结果重新检查/安装
        (例如用户刚装好 Node.js 或网络恢复后执行 /dy_bridge_restart)。
        """
        if self._build_done and not force:
            return self._build_ok
        self._build_done = True
        loop = asyncio.get_running_loop()
        try:
            self._build_ok = await loop.run_in_executor(None, self._prepare_sync)
        except Exception as e:  # noqa: BLE001
            self._build_msg = f"amagi 运行时检查异常: {e}"
            logger.error(self._build_msg)
            self._build_ok = False
        return self._build_ok

    # ---------------- 进程管理 ----------------

    async def ensure_started(self):
        """确保桥接进程已启动; 未配置 Cookie / amagi 未就绪则记录原因并返回"""
        async with self._start_lock:
            if self.running and self._started:
                return True

            if not self.cookie_configured:
                if not self._warn_cookie:
                    logger.warning("抖音 Cookie 未配置 (douyin_cookie), 跳过启动 amagi 桥接")
                    self._warn_cookie = True
                return False

            await self.prepare()
            if not self._build_ok:
                logger.warning("amagi 未就绪, 无法启动桥接: " + self._build_msg)
                return False
            if not self.server_script.exists():
                logger.error(f"缺少桥接脚本: {self.server_script}")
                return False

            # 端口已有监听者: 先弄清是不是"我们自己的子进程"
            #
            # 为什么必须先探端口: 桥接是被 reload/terminate 时可能没被杀干净的。
            # 残留进程占着端口时, 再拉起的新进程必然 EADDRINUSE 并立刻退出, 但
            # 健康检查会被**残留进程的响应**骗过(握手成功), 于是插件每个轮询周期
            # 都白拉一个进程、日志成对刷屏、pid 一直变 —— 复用即可根治。
            if await asyncio.to_thread(self._probe, 1.0):
                owner = await asyncio.to_thread(self._port_listener_pid)
                if self._proc and self._proc.returncode is None and (
                        owner is None or owner == self._proc.pid):
                    self._started = True
                    return True
                # 不是本次拉起的进程在监听 → 直接复用, 不再拉起注定失败的进程
                self._started = True
                self._last_err = ""
                logger.warning(
                    f"端口 {self.port} 已有桥接在监听 (pid={owner if owner else '未知'}, "
                    f"不是当前子进程) —— 直接复用, 不再重复拉起。"
                    f"这通常是上次重载/退出时没清理干净的残留进程; "
                    f"需要彻底重启请执行 /dy_bridge_restart"
                )
                return True

            # 距上次尝试 3 秒内不重复尝试, 防止多个协程同时拉起
            now = time.monotonic()
            if now - self._ready_attempted_at < 3:
                return self.running and self._started
            self._ready_attempted_at = now

            # 旧进程清理
            await self._terminate_proc()

        env = dict(os.environ)
        env["DOUYIN_COOKIE"] = self.cookie
        env["AMAGI_PORT"] = str(self.port)
        env["AMAGI_DIR"] = str(self.amagi_dir)
        # 直接把定位到的入口文件传给桥接脚本, 避免 JS 侧重复猜测目录布局
        if self.amagi_entry:
            env["AMAGI_ENTRY"] = str(self.amagi_entry)

        stdout_path = self.log_dir / "bridge.out.log"
        stderr_path = self.log_dir / "bridge.err.log"
        try:
            out_f = open(stdout_path, "a", encoding="utf-8", errors="ignore")
            err_f = open(stderr_path, "a", encoding="utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            self._last_err = f"无法打开桥接日志文件: {e}"
            logger.error(self._last_err)
            return False

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.node_bin, str(self.server_script),
                env=env, stdout=out_f, stderr=err_f,
                cwd=str(self.plugin_dir),
            )
            logger.info(
                f"已启动 amagi 桥接 (pid={self._proc.pid}, port={self.port}) "
                f"cookie={'已配置' if self.cookie_configured else '空'}"
            )
        except Exception as e:  # noqa: BLE001
            self._last_err = f"启动 amagi 桥接进程失败: {e}"
            logger.error(self._last_err)
            self._proc = None
            return False
        finally:
            # 子进程已获得句柄副本, 父进程关闭自身副本 (避免句柄泄漏)
            out_f.close()
            err_f.close()

        # 健康检查轮询
        ok = await self._wait_ready(timeout=20)
        if not ok:
            tail = self._tail_log(stderr_path, 15) + "\n" + self._tail_log(stdout_path, 15)
            self._last_err = f"桥接启动后健康检查未通过:\n{tail}"
            logger.error(self._last_err)
            await self._terminate_proc()
            return False
        self._started = True
        self._last_err = ""
        pid = self._proc.pid if self._proc else "?"
        logger.info(f"amagi 桥接就绪: http://{self.host}:{self.port} (pid={pid})")
        return True

    def _probe(self, timeout: float = 1) -> bool:
        """端口探测: 能建立 HTTP 连接即视为就绪 (不要求 JSON/特定状态码)"""
        try:
            requests.get(f"http://{self.host}:{self.port}/ping", timeout=timeout)
            return True
        except requests.RequestException:
            return False

    @staticmethod
    def _port_listener_pid(port: int, proc_root: str = "/proc") -> Optional[int]:
        """
        找出正在 LISTEN 指定端口的进程 pid (Linux; 其它平台/查不到时返回 None)。

        做法: 从 /proc/net/tcp{,6} 里找到该端口 LISTEN 套接字的 inode,
        再在 /proc/<pid>/fd/* 里找出持有该 inode 的进程。
        不依赖 ss/lsof (AstrBot 容器里通常没装这些工具)。
        """
        try:
            inodes = set()
            for name in ("tcp", "tcp6"):
                try:
                    with open(f"{proc_root}/net/{name}", "r", encoding="utf-8") as f:
                        next(f, None)   # 跳过表头
                        for line in f:
                            parts = line.split()
                            if len(parts) < 10 or parts[3] != "0A":   # 0A = LISTEN
                                continue
                            try:
                                if int(parts[1].rsplit(":", 1)[-1], 16) != port:
                                    continue
                            except ValueError:
                                continue
                            inodes.add(parts[9])
                except OSError:
                    continue
            if not inodes:
                return None

            for entry in os.listdir(proc_root):
                if not entry.isdigit():
                    continue
                fd_dir = os.path.join(proc_root, entry, "fd")
                try:
                    fds = os.listdir(fd_dir)
                except OSError:
                    continue
                for fd in fds:
                    try:
                        target = os.readlink(os.path.join(fd_dir, fd))
                    except OSError:
                        continue
                    if target.startswith("socket:[") and target[8:-1] in inodes:
                        return int(entry)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"查找端口 {port} 的监听进程失败: {e}")
        return None

    async def _kill_port_owner(self) -> bool:
        """
        结束"占着端口但不是本次拉起的子进程"的残留桥接进程。

        只有 /dy_bridge_restart 会走到这里 —— 那是用户显式要求"彻底重启"的动作,
        可以动手清理残留; 日常 ensure_started 只复用, 不会去杀进程。
        """
        owner = await asyncio.to_thread(self._port_listener_pid, self.port)
        if owner is None:
            return False
        if self._proc and owner == self._proc.pid:
            return False
        if owner == os.getpid():
            return False

        logger.warning(f"清理残留桥接进程 pid={owner} (占用端口 {self.port}, 但不是当前子进程)")
        try:
            os.kill(owner, signal.SIGTERM)
        except OSError as e:
            logger.error(f"结束残留进程 pid={owner} 失败: {e}")
            return False

        for _ in range(20):
            await asyncio.sleep(0.25)
            if not await asyncio.to_thread(self._probe, 0.5):
                logger.info(f"残留进程 pid={owner} 已退出, 端口 {self.port} 已释放")
                return True
        try:
            os.kill(owner, getattr(signal, "SIGKILL", signal.SIGTERM))
            logger.warning(f"残留进程 pid={owner} 未响应 SIGTERM, 已强制结束")
        except OSError as e:
            logger.error(f"强制结束残留进程 pid={owner} 失败: {e}")
        return True

    async def _wait_ready(self, timeout: float = 20) -> bool:
        """轮询 /ping 直到端口可连接 (说明服务已监听)"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc and self._proc.returncode is not None:
                return False
            if await asyncio.to_thread(self._probe, 1.0):
                return True
            await asyncio.sleep(0.25)
        return False

    async def _terminate_proc(self):
        if not self._proc:
            return
        proc = self._proc
        self._proc = None
        self._started = False
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"终止桥接进程出错: {e}")

        # 杀完之后端口必须真的被释放; 没释放说明有残留进程, 这是后面一切异常的根源
        if await asyncio.to_thread(self._probe, 0.5):
            owner = await asyncio.to_thread(self._port_listener_pid, self.port)
            logger.warning(
                f"桥接进程已结束, 但端口 {self.port} 仍在监听 (pid={owner if owner else '未知'}) —— "
                f"存在残留进程; 需要彻底清理请执行 /dy_bridge_restart"
            )

    async def stop(self):
        await self._terminate_proc()

    async def restart(self):
        """重启桥接 (用于 Cookie 变更后); 会强制重新检查/安装 amagi 运行时"""
        await self._terminate_proc()
        await self._kill_port_owner()
        self._ready_attempted_at = 0.0
        await self.prepare(force=True)
        return await self.ensure_started()

    # ---------------- HTTP 调用 ----------------

    @staticmethod
    def _http_get_params(url: str, params: Optional[Dict[str, Any]],
                         timeout: float = 25) -> Dict[str, Any]:
        """同步 GET (带 query 参数) 并解析 JSON"""
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            raise AmagiError(f"请求桥接失败 ({url}): {e}") from e
        if resp.status_code >= 500:
            raise AmagiAPIError(f"桥接 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise AmagiError(f"桥接返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}") from e

    async def request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        调用 amagi 内置路由, 返回其 data 字段 (amagi 业务成功时).

        失败时抛出 AmagiError/AmagiAPIError.
        """
        if not (self.running and self._started):
            await self.ensure_started()
        if not (self.running and self._started):
            raise AmagiNotReady(f"amagi 桥接不可用: {self._last_err or self._build_msg or '未启动'}")

        url = f"http://{self.host}:{self.port}{path}"
        payload = await asyncio.to_thread(self._http_get_params, url, params, timeout=25)
        if payload.get("success") is not True:
            code = payload.get("code", "?")
            message = payload.get("message") or "未知错误"
            error = payload.get("error") or {}
            raise AmagiAPIError(f"amagi 请求失败 (code={code}): {message} ({json.dumps(error, ensure_ascii=False)[:300]})")
        data = payload.get("data")
        if data is None:
            raise AmagiError(f"amagi 返回空数据: {path} {params}")
        return data
