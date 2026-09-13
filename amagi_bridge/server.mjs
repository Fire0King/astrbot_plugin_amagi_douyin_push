#!/usr/bin/env node
/**
 * amagi 桥接服务入口 (由 Python 插件侧常驻拉起)
 *
 * 职责:
 *   1. 加载插件目录下 amagi 子模块的构建产物 (packages/core/dist/default/index.{mjs,cjs})
 *   2. 用插件配置的抖音 Cookie 创建 amagi 客户端并启动其内置 HTTP 服务
 *   3. 在 stdout 输出机器可读的就绪/错误信息, 供 Python 侧判断启动结果
 *
 * 环境变量:
 *   DOUYIN_COOKIE  抖音 Cookie (必须)
 *   AMAGI_PORT     监听端口 (默认 48211)
 *   AMAGI_DIR      amagi 仓库所在目录 (默认: 本文件上一级的 amagi/)
 *
 * 退出码:
 *   0  正常退出
 *   1  启动失败 (产物缺失 / 端口占用 / 未安装依赖等)
 */
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const pluginRoot = path.resolve(__dirname, '..')

const env = process.env
const amagiDir = env.AMAGI_DIR ? path.resolve(env.AMAGI_DIR) : path.join(pluginRoot, 'amagi')
const cookie = env.DOUYIN_COOKIE || ''
const port = Number.parseInt(env.AMAGI_PORT || '48211', 10)

const DIST_DIR = path.join(amagiDir, 'packages', 'core', 'dist')
const ENTRY_CANDIDATES = [
  path.join(DIST_DIR, 'default', 'index.mjs'),
  path.join(DIST_DIR, 'default', 'index.cjs'),
]

function fail(msg) {
  // 统一以固定前缀输出, Python 侧按行解析
  console.log(`[amagi-bridge] error ${msg}`)
  process.exit(1)
}

async function resolveEntry() {
  for (const candidate of ENTRY_CANDIDATES) {
    try {
      await import('node:fs/promises').then((fs) => fs.access(candidate))
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
      `未找到 amagi 构建产物 (${path.join(DIST_DIR, 'default')}). ` +
        `请先在插件目录执行: cd amagi && pnpm install && pnpm --filter @ikenxuan/amagi run build`
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

  let app
  try {
    app = client.startServer(port)
  } catch (err) {
    return fail(`启动 amagi HTTP 服务失败 (端口 ${port}): ${err?.message ?? err}`)
  }

  // amagi 内部在回调里才真正 listen, 这里稍等一拍再广播就绪,
  // 真正的健康检查仍由 Python 侧轮询 /ping 决定。
  setTimeout(() => {
    console.log(`[amagi-bridge] ready port=${port} cookie=${cookie ? 'configured' : 'empty'}`)
  }, 300)

  // 兜底: 端口被占用等异步监听错误直接退出, 由 Python 侧捕获
  const onUncaught = (err) => {
    console.log(`[amagi-bridge] error ${err?.message ?? err}`)
    process.exit(1)
  }
  process.on('uncaughtException', onUncaught)
  process.on('unhandledRejection', onUncaught)

  const shutdown = () => {
    try {
      app?.close?.()
    } catch {
      /* 忽略关闭错误 */
    }
    process.exit(0)
  }
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}

main()
