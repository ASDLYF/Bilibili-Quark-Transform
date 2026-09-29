/**
 * 跨平台辅助：Windows / macOS / Linux 通用。
 *
 * 这里集中处理平台差异，index.js 不再出现任何写死的 `\` 或 Windows 专有命令：
 *   - Python 解释器查找（macOS/Linux 常是 python3，Windows 是 python / py -3）
 *   - 杀进程树（Windows 用 taskkill /T，类 Unix 用进程组负 PID）
 *   - 子进程环境：设置后端目录、ffmpeg 路径，并去除无用/干扰的 PYTHON* 变量
 *   - schemastery 的定位（Config 需要 standard-schema 对象）
 */
import { spawn } from 'node:child_process';
import { accessSync, constants, existsSync, readdirSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { delimiter, dirname, isAbsolute, join, resolve as pathResolve } from 'node:path';

export const IS_WINDOWS = process.platform === 'win32';

// ---------------------------------------------------------------- 路径解析

/**
 * 把可能含 `~`、环境变量、相对路径的写法，解析成绝对路径。
 * 同时把分隔符统一成本平台风格（Windows 上 `D:/a/b` → `D:\a\b`）。
 */
export function resolveUserPath(p, baseDir) {
  let s = String(p ?? '').trim();
  if (!s) return s;
  if (s === '~') s = process.env.HOME || process.env.USERPROFILE || s;
  else if (s.startsWith('~/') || s.startsWith('~\\')) {
    s = join(process.env.HOME || process.env.USERPROFILE || '~', s.slice(2));
  }
  s = s.replace(/\$(\w+)|\$\{(\w+)\}|%(\w+)%/g, (m, a, b, c) => {
    const key = a || b || c;
    return process.env[key] !== undefined ? process.env[key] : m;
  });

  // 统一分隔符，但不能丢掉根：
  //   Windows `D:\a/b`  -> 保留盘符
  //   Unix    `/a/b`    -> 保留根斜杠
  const winDrive = s.match(/^([A-Za-z]:)[\\/]+(.*)$/);
  const uniRoot = s.match(/^[\\/]+(.*)$/);
  if (winDrive) {
    const rest = winDrive[2].split(/[\\/]+/).filter(Boolean);
    s = rest.length ? join(`${winDrive[1]}\\`, ...rest) : `${winDrive[1]}\\`;
  } else if (uniRoot) {
    const rest = uniRoot[1].split(/[\\/]+/).filter(Boolean);
    s = rest.length ? join('/', ...rest) : '/';
  } else {
    const rest = s.split(/[\\/]+/).filter(Boolean);
    s = rest.length ? join(...rest) : s;
  }
  return isAbsolute(s) ? s : pathResolve(baseDir || process.cwd(), s);
}

function isExecutableFile(p) {
  try {
    if (!statSync(p).isFile()) return false;
    if (IS_WINDOWS) return true;
    accessSync(p, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function findOnPath(names) {
  const dirs = String(process.env.PATH || '').split(delimiter).filter(Boolean);
  for (const dir of dirs) {
    for (const name of names) {
      const full = join(dir, name);
      if (isExecutableFile(full)) return full;
    }
  }
  return null;
}

function findInPythonFrameworks() {
  const roots = ['/Library/Frameworks/Python.framework/Versions', '/usr/local/bin', '/opt/homebrew/bin'];
  for (const root of roots) {
    try {
      if (!existsSync(root) || !statSync(root).isDirectory()) continue;
      const names = readdirSync(root)
        .filter((n) => /^3(\.\d+)*$/.test(n) || /^python3(\.\d+)?$/.test(n))
        .sort()
        .reverse();
      for (const n of names) {
        const cand = /^3\./.test(n) ? join(root, n, 'bin', 'python3') : join(root, n);
        if (isExecutableFile(cand)) return cand;
      }
    } catch {
      /* 忽略无权限目录 */
    }
  }
  return null;
}

/**
 * 依次尝试找到可用的 Python，返回 { cmd, pre, version }。
 * 覆盖 Windows(python / py -3)、macOS/Linux(python3 / python)。
 */
export async function resolvePythonCmd(pythonPath, probe) {
  const explicit = String(pythonPath || '').trim();
  const candidates = [];

  if (explicit) {
    const abs = isAbsolute(explicit) ? explicit : findOnPath([explicit]);
    if (abs && isExecutableFile(abs)) {
      candidates.push({ cmd: abs, pre: [] });
    } else if (!IS_WINDOWS && (explicit === 'python3' || explicit === 'python')) {
      const found = findOnPath([explicit]);
      if (found) candidates.push({ cmd: found, pre: [] });
    }
    if (!candidates.length) {
      // 显式指定却找不到：仍然试一次，让错误信息更直观
      candidates.push({ cmd: explicit, pre: [] });
    }
  }

  if (IS_WINDOWS) {
    candidates.push({ cmd: 'python', pre: [] }, { cmd: 'py', pre: ['-3'] }, { cmd: 'python3', pre: [] });
  } else {
    candidates.push({ cmd: 'python3', pre: [] }, { cmd: 'python', pre: [] });
  }
  for (const name of ['python3', 'python']) {
    const found = findOnPath([name]);
    if (found) candidates.push({ cmd: found, pre: [] });
  }
  const framed = findInPythonFrameworks();
  if (framed) candidates.push({ cmd: framed, pre: [] });

  const seen = new Set();
  const failures = [];
  for (const c of candidates) {
    const key = `${c.cmd} ${c.pre.join(' ')}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const r = await probe(c.cmd, [...c.pre, '-c', 'import sys;print(sys.version.split()[0])']);
    if (r.error) {
      failures.push(`${c.cmd}: ${r.error}`);
      continue;
    }
    if (r.code === 0) {
      const version = (String(r.log || '').match(/\d+\.\d+\.\d+/) || ['?'])[0];
      return { ok: true, cmd: c.cmd, pre: c.pre, version };
    }
    failures.push(`${c.cmd}: exit ${r.code}`);
  }
  return { ok: false, failures };
}

// ---------------------------------------------------------------- 进程控制

/** 杀掉整棵进程树：Windows 用 taskkill /T，类 Unix 杀进程组。 */
export function killTree(child, pid) {
  try {
    if (IS_WINDOWS) {
      spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true }).unref();
      return true;
    }
    try {
      process.kill(-pid, 'SIGKILL'); // 负 PID = 整个进程组
      return true;
    } catch {
      child.kill('SIGKILL');
      return true;
    }
  } catch {
    try {
      child.kill('SIGKILL');
      return true;
    } catch {
      return false;
    }
  }
}

/** 判断 pid 是否仍存活（跨平台）。 */
export function isAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return Boolean(e && e.code === 'EPERM');
  }
}

// ---------------------------------------------------------------- 子进程环境

/**
 * 组装子进程环境：
 *   - 清除 PYTHONHOME/PYTHONPATH 等可能干扰解释器启动的变量
 *   - 指定后端目录，让 Python 能 import 同目录模块
 *   - 仅在显式配置了 ffmpeg 时才设置 BILI_FFMPEG/BILI_FFPROBE（否则交给后端自行查找）
 */
export function workerEnv({ backendDir, ffmpegPath, ffprobePath, extra } = {}) {
  const env = { ...process.env, ...(extra || {}) };
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  if (backendDir) env.BILI_BACKEND_DIR = backendDir;
  if (ffmpegPath) env.BILI_FFMPEG = ffmpegPath;
  if (ffprobePath) env.BILI_FFPROBE = ffprobePath;
  env.PYTHONIOENCODING = 'utf-8';
  env.PYTHONUTF8 = '1';
  env.PYTHONUNBUFFERED = '1';
  return env;
}

// ---------------------------------------------------------------- schemastery

function appBaseDirs() {
  const out = [];
  const push = (p) => {
    if (p && !out.includes(p)) out.push(p);
  };
  if (process.resourcesPath) push(process.resourcesPath);
  if (process.execPath) push(join(dirname(process.execPath), 'resources'));
  if (IS_WINDOWS) {
    push('D:\\dsh\\DSH Desktop\\resources');
  } else {
    push('/Applications/DSH Desktop.app/Contents/Resources');
    push(join(process.env.HOME || '/', 'Applications', 'DSH Desktop.app', 'Contents', 'Resources'));
  }
  return out.filter((p) => {
    try {
      return existsSync(p);
    } catch {
      return false;
    }
  });
}

/**
 * 找到 schemastery（DSH 自带，用于声明 Config）。
 * 从 DSH 应用目录解析；找不到时返回 null，调用方退化为自实现的等价 schema。
 */
export function loadSchemastery() {
  const bases = appBaseDirs();
  const specs = ['@deepseek-ai/schemastery'];
  for (const base of bases) {
    specs.push(join(base, 'app', 'node_modules', '@deepseek-ai', 'schemastery'));
    specs.push(join(base, 'app', 'node_modules', '@deepseek-ai', 'schemastery', 'lib', 'index.cjs'));
  }
  for (const spec of specs) {
    const refs = bases.length ? bases.map((b) => join(b, 'app', 'package.json')) : [];
    refs.push(import.meta.url);
    for (const ref of refs) {
      try {
        const req = createRequire(ref.startsWith('file:') ? ref : `file://${ref}`);
        const m = req(spec);
        const z = (m && m.default) || m;
        if (z && typeof z.object === 'function') return z;
      } catch {
        /* 换下一个来源 */
      }
    }
  }
  return null;
}
