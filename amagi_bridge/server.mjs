#!/usr/bin/env node
/**
 * amagi 桥接服务入口 (由 Python 插件侧常驻拉起)
 *
 * 职责:
 *   1. 加载 amagi 的构建产物 (支持完整仓库 / npm 安装 / 裸包目录三种布局)
 *   2. 用插件配置的抖音 Cookie 创建 amagi 客户端并启动其内置 HTTP 服务
 *   3. 在 stdout 输出机器可读的就绪/错误信息, 供 Python 侧判断启动结果
 *
 * 环境变量:
 *   DOUYIN_COOKIE  抖音 Cookie (必须)
 *   AMAGI_PORT     监听端口 (默认 48211)
 *   AMAGI_ENTRY    amagi dist 入口文件的绝对路径 (优先使用; 由 Python 侧定位后传入)
 *   AMAGI_DIR      amagi 运行时目录 (默认: 本文件上一级的 .amagi/)
 *
 * 退出码:
 *   0  正常退出
 *   1  启动失败 (产物缺失 / 端口占用 / 未安装依赖等)
 */
import { access } from 'node:fs/promises'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const pluginRoot = path.resolve(__dirname, '..')

const env = process.env
const amagiDir = env.AMAGI_DIR ? path.resolve(env.AMAGI_DIR) : path.join(pluginRoot, '.amagi')
const cookie = env.DOUYIN_COOKIE || ''
const port = Number.parseInt(env.AMAGI_PORT || '48211', 10)

const ENTRY_CANDIDATES = [
  // Python 侧已定位好的入口 (最可靠, 优先)
  env.AMAGI_ENTRY ? path.resolve(env.AMAGI_ENTRY) : null,
  // 完整仓库布局
  path.join(amagiDir, 'packages', 'core', 'dist', 'default', 'index.mjs'),
  path.join(amagiDir, 'packages', 'core', 'dist', 'default', 'index.cjs'),
  // npm 安装布局
  path.join(amagiDir, 'node_modules', '@ikenxuan', 'amagi', 'dist', 'default', 'index.mjs'),
  path.join(amagiDir, 'node_modules', '@ikenxuan', 'amagi', 'dist', 'default', 'index.cjs'),
  // 裸包目录布局
  path.join(amagiDir, 'dist', 'default', 'index.mjs'),
  path.join(amagiDir, 'dist', 'default', 'index.cjs'),
].filter(Boolean)

function fail(msg) {
  // 统一以固定前缀输出, Python 侧按行解析。
  // stdout 与 stderr 都写一份: Python 侧排查时会同时 tail 两个日志文件。
  console.log(`[amagi-bridge] error ${msg}`)
  console.error(`[amagi-bridge] error ${msg}`)
  process.exit(1)
}

/**
 * 端口预检: 返回 null 表示端口空闲, 否则返回占用原因(如 EADDRINUSE)。
 *
 * 为什么必须预检: amagi 的 startServer() 是同步返回、异步 listen,
 * 端口被占用时不会抛出异常; 不预检就会出现"打印 ready 然后静默退出"的假就绪。
 */
function probePort(port) {
  return new Promise((resolve) => {
    const probe = net.createServer()
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      resolve(result)
    }
    probe.once('error', (err) => finish(err?.code || err?.message || 'unknown'))
    probe.once('listening', () => probe.close(() => finish(null)))
    try {
      probe.listen(port, '::')
    } catch (err) {
      finish(err?.code || err?.message || 'unknown')
    }
  })
}

async function resolveEntry() {
  for (const candidate of ENTRY_CANDIDATES) {
    try {
      await access(candidate)
      return candidate
    } catch {
      // 继续尝试下一个候选
    }
  }
  return null
}

async function main() {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return fail(`非法端口号: ${env.AMAGI_PORT}`)
  }

  const entry = await resolveEntry()
  if (!entry) {
    return fail(
      `未找到 amagi 构建产物 (已尝试: ${ENTRY_CANDIDATES.join(', ')}). ` +
        `正常情况下插件会自动执行 npm install @ikenxuan/amagi, ` +
        `也可手动执行: cd ${amagiDir} && npm install @ikenxuan/amagi`
    )
  }

  let mod
  try {
    mod = await import(pathToFileURL(entry).href)
  } catch (err) {
    return fail(`加载 amagi 构建产物失败: ${err?.message ?? err}`)
  }

  // 兼容 default / named 两种导出形态
  const createClient = mod.default || mod.amagi || mod.CreateApp
  if (typeof createClient !== 'function') {
    return fail('amagi 构建产物中没有找到客户端构造函数 (default/amagi/CreateApp)')
  }

  let client
  try {
    client = createClient({
      cookies: { douyin: cookie },
    })
  } catch (err) {
    return fail(`创建 amagi 客户端失败: ${err?.message ?? err}`)
  }

  // 端口预检: 被占用时立刻大声失败, 而不是"假就绪 + 静默退出"
  const busyReason = await probePort(port)
  if (busyReason === 'EADDRINUSE') {
    return fail(
      `端口 ${port} 已被占用 —— 大概率是上一个没退干净的桥接进程还在监听。` +
        `请先结束占用 ${port} 的进程(或把插件配置里的 amagi_port 换成其它端口)再启动。`
    )
  }
  if (busyReason) {
    console.log(`[amagi-bridge] warn 端口预检异常(${busyReason}), 仍继续尝试启动`)
  }

  let app
  try {
    app = client.startServer(port)
  } catch (err) {
    return fail(`启动 amagi HTTP 服务失败 (端口 ${port}): ${err?.message ?? err}`)
  }

  // 监听错误必须被抓住并大声报出来。
  // 注意: amagi 的 startServer() 是**同步返回、异步 listen**, 端口被占用时错误
  // 只会以 'error' 事件的形式冒出来; 不接管它就会变成"打印 ready 然后静默退出"。
  let ready = false
  const onListenFailed = (err) => {
    fail(
      `监听端口 ${port} 失败: ${err?.code || err?.message || err}` +
        ` —— 端口大概率被上一个没退干净的桥接进程占着。` +
        `请先结束占用 ${port} 的进程 (或改用别的端口) 再启动。`
    )
  }
  for (const candidate of [app, app?.server, app?.httpServer]) {
    if (candidate && typeof candidate.on === 'function') {
      candidate.on('error', onListenFailed)
    }
  }

  // amagi 内部在回调里才真正 listen, 这里稍等一拍再广播就绪,
  // 真正的健康检查仍由 Python 侧轮询 /ping 决定。
  // 只有在没有触发监听错误时才广播 ready —— 否则 Python 侧会把"别人的端口"
  // 当成自己的桥接已就绪。
  setTimeout(() => {
    ready = true
    console.log(`[amagi-bridge] ready port=${port} cookie=${cookie ? 'configured' : 'empty'}`)
  }, 300)

  // 兜底: 其它异步错误直接退出, 由 Python 侧捕获
  const onUncaught = (err) => {
    fail(`未捕获异常: ${err?.message ?? err}`)
  }
  process.on('uncaughtException', onUncaught)
  process.on('unhandledRejection', onUncaught)

  const shutdown = () => {
    try {
      app?.close?.()
    } catch {
      /* 忽略关闭错误 */
    }
    process.exit(ready ? 0 : 1)
  }
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}

main()
