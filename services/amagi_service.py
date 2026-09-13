"""
amagi 桥接服务管理 (Python 侧)

职责:
  1. 定位并(按需)构建插件目录下的 amagi 子模块
  2. 以常驻 Node 子进程方式启动 amagi_bridge/server.mjs (amagi 官方 HTTP 服务)
  3. 提供健康检查 / HTTP JSON 请求 / 重启 / 停止 / 状态查询

约定:
  - amagi 子模块目录: <plugin_dir>/amagi
  - 桥接脚本:        <plugin_dir>/amagi_bridge/server.mjs
  - 桥接日志:        写入 AstrBot 数据目录下的 amagi_bridge/ 文件夹
  - HTTP 请求走 amagi 自带的 /api/douyin/* 路由, 响应包裹格式:
      {"success": true,  "code": 200, "message": "...", "data": {...}, ...}
      {"success": false, "code": 400, "message": "...", "error": {...}, ...}
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from astrbot.api import logger
from astrbot.api.star import StarTools

# amagi 构建产物入口 (tsdown 输出)
_DIST_CANDIDATES = (
    Path("packages", "core", "dist", "default", "index.mjs"),
    Path("packages", "core", "dist", "default", "index.cjs"),
)

# 桥接进程就绪后 stdout 输出的固定行, 用于校验
_READY_PREFIX = "[amagi-bridge] ready"
_ERROR_PREFIX = "[amagi-bridge] error"


class AmagiError(Exception):
    """amagi 调用相关错误 (桥接不可用 / HTTP 错误 / 数据异常)"""


class AmagiNotReady(AmagiError):
    """桥接尚未就绪或未配置 Cookie"""


class AmagiAPIError(AmagiError):
    """amagi 返回的业务错误 (含 code/message)"""


def _find_executable(name: str) -> str:
    """在 PATH 中查找可执行文件 (Windows 下优先 .cmd)"""
    exts = (".cmd", ".exe", ".bat") if os.name == "nt" else ("",)
    for ext in exts:
        found = shutil.which(name + ext)
        if found:
            return found
    found = shutil.which(name)
    if found:
        return found
    return name


class AmagiService:
    """管理 amagi 子模块的构建与常驻 HTTP 桥接进程"""

    def __init__(self, plugin_dir: Path, cfg: Dict[str, Any]):
        self.plugin_dir = Path(plugin_dir)
        self.cfg = cfg

        # 子模块定位: 默认 <plugin_dir>/amagi, 允许配置 amagi_dir 覆盖
        configured = str(cfg.get("amagi_dir") or "").strip()
        if configured:
            self.amagi_dir = Path(configured).expanduser()
        else:
            self.amagi_dir = self.plugin_dir / "amagi"
        self.server_script = self.plugin_dir / "amagi_bridge" / "server.mjs"

        self.host = "127.0.0.1"
        self.port = int(cfg.get("amagi_port", 48211) or 48211)

        # 数据/日志目录 (AstrBot 标准数据目录)
        data_dir = StarTools.get_data_dir(plugin_name="astrbot_plugin_amagi_douyin_push")
        self.log_dir = Path(data_dir) / "amagi_bridge"
        os.makedirs(self.log_dir, exist_ok=True)

        self.node_bin = _find_executable(str(cfg.get("node_path") or "node"))
        self.pnpm_bin = _find_executable(str(cfg.get("pnpm_path") or "pnpm"))

        self.cookie: str = ""
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._started: bool = False      # 是否已成功监听端口
        self._build_done: bool = False   # 本轮是否已检查/构建过 amagi
        self._build_ok: bool = False
        self._build_msg: str = "尚未检查 amagi 构建产物"
        self._last_err: str = ""
        self._ready_attempted_at: float = 0.0
        self._warn_cookie: bool = False

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
        """构建产物是否存在"""
        return any((self.amagi_dir / cand).exists() for cand in _DIST_CANDIDATES)

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
            "amagi_dir": str(self.amagi_dir),
            "last_error": self._last_err,
        }

    # ---------------- 构建 ----------------

    def _prepare_sync(self) -> bool:
        """同步确保 amagi 已安装依赖并产出 dist (供线程中调用)"""
        if not self.amagi_dir.exists():
            self._build_msg = f"未找到 amagi 目录: {self.amagi_dir}"
            logger.error(self._build_msg)
            return False
        if not (self.amagi_dir / "package.json").exists():
            self._build_msg = f"amagi 目录无效 (缺少 package.json): {self.amagi_dir}"
            logger.error(self._build_msg)
            return False
        if self.dist_ready:
            self._build_ok = True
            self._build_msg = "amagi 构建产物已就绪"
            return True

        # 需要构建
        logger.warning(
            f"amagi 尚未构建 (缺少 {self.amagi_dir / 'packages' / 'core' / 'dist'}). "
            f"将尝试自动执行 pnpm install / build ..."
        )
        if not self._run_cmd(self.pnpm_bin, ["install", "--ignore-scripts"],
                             self.amagi_dir, step="pnpm install"):
            self._build_msg = "pnpm install 失败, 请手动在 amagi 目录执行: pnpm install"
            return False
        if not self._run_cmd(self.pnpm_bin, ["--filter", "@ikenxuan/amagi", "run", "build"],
                             self.amagi_dir, step="pnpm build"):
            self._build_msg = "pnpm build 失败, 请手动在 amagi 目录执行: pnpm --filter @ikenxuan/amagi run build"
            return False

        if self.dist_ready:
            self._build_ok = True
            self._build_msg = "amagi 构建成功"
            logger.info("amagi 构建成功")
            return True
        self._build_msg = "构建结束后仍未找到 amagi 产物"
        return False

    def _run_cmd(self, cmd: str, args: list, cwd: Path, step: str,
                 timeout: int = 1800) -> bool:
        """运行命令, 输出重定向到日志文件 (避免管道限制)"""
        log_file = self.log_dir / f"build_{int(time.time())}.log"
        try:
            with open(log_file, "w", encoding="utf-8") as fp:
                proc = subprocess.run(
                    [cmd, *args], cwd=str(cwd),
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

    async def prepare(self):
        """异步准备: 构建检查在线程中执行, 避免阻塞事件循环"""
        if self._build_done:
            return self._build_ok
        self._build_done = True
        loop = asyncio.get_running_loop()
        try:
            self._build_ok = await loop.run_in_executor(None, self._prepare_sync)
        except Exception as e:  # noqa: BLE001
            self._build_msg = f"构建检查异常: {e}"
            logger.error(self._build_msg)
            self._build_ok = False
        return self._build_ok

    # ---------------- 进程管理 ----------------

    async def ensure_started(self):
        """确保桥接进程已启动; 未配置 Cookie / 未构建成功则记录原因并返回"""
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
        logger.info(f"amagi 桥接就绪: http://{self.host}:{self.port}")
        return True

    def _probe(self, timeout: float = 1) -> bool:
        """端口探测: 能建立 HTTP 连接即视为就绪 (不要求 JSON/特定状态码)"""
        try:
            requests.get(f"http://{self.host}:{self.port}/ping", timeout=timeout)
            return True
        except requests.RequestException:
            return False

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

    async def stop(self):
        await self._terminate_proc()

    async def restart(self):
        """重启桥接 (用于 Cookie 变更后)"""
        await self._terminate_proc()
        self._ready_attempted_at = 0.0
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
