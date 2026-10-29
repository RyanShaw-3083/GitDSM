#!/usr/bin/env python3
"""gitdsm: requirements.txt style Git cloner for DSM.

File format (repos.txt)
    /volume1/gitrepos
    https://github.com/microsoft/qlib.git
    https://github.com/mementum/backtrader.git
    # comments and blank lines are ignored

The first non-comment line may be a store path (absolute).
Every later line is one git clone URL.

Supports optional proxy prefix via UI or --proxy (used only for the transfer via
`git -c url.<proxy>/https://.insteadOf=https://`; origin always stays the real URL).
Resume: git clone is not fully resumable by default; re-run skips existing dirs.

    python3 gitdsm.py -r repos.txt
    python3 gitdsm.py -r repos.txt --proxy https://gh-proxy.com/
    python3 gitdsm.py serve
    python3 gitdsm.py serve --host 0.0.0.0 --port 8011
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import urllib.request
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parent
WEBS = ROOT / "webs"
STATE = Path(os.environ.get("GITDSM_STATE", ROOT / "state"))
STATE.mkdir(exist_ok=True)

def log(msg: str) -> None:
    print(msg, flush=True)

def safe_nas_path(raw: str) -> Path:
    path = Path(raw or "/volume1").expanduser()
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    resolved = path.resolve()
    text = str(resolved)
    if not (text == "/volume1" or text.startswith("/volume1/") or text.startswith("/volumeUSB") or text.startswith("/volume")):
        raise ValueError("only /volume paths are allowed")
    return resolved

def parse_requirements(text: str, store_override: str | None = None) -> tuple[Path, list[str]]:
    store = Path(store_override).expanduser() if store_override else None
    urls: list[str] = []
    first = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if first and (line.startswith("/") or line.startswith("~")):
            if store_override is None:
                store = Path(line).expanduser()
            first = False
            continue
        first = False
        if line.startswith("http://") or line.startswith("https://") or line.startswith("git@"):
            urls.append(line)
        else:
            # treat as org/name and convert
            if "/" in line and not line.startswith("http"):
                urls.append(f"https://github.com/{line}.git")
            else:
                urls.append(line)
    if store is None:
        store = Path(os.environ.get("GIT_STORE", "/volume1/gitrepos"))
    if not urls:
        raise SystemExit("no repo urls in file")
    return store, urls

_FORGES = ("github.com", "gitlab.com", "gitee.com", "bitbucket.org", "codeberg.org",
           "huggingface.co", "hf-mirror.com", "raw.githubusercontent.com")
_SCHEME = re.compile(r"https?://", re.I)

def unproxy(url: str) -> str:
    """剥掉任意层代理前缀，还原源站地址。
    https://a.com/https://b.net/https://github.com/u/r.git -> https://github.com/u/r.git
    https://gh-proxy.com/github.com/u/r                     -> https://github.com/u/r
    """
    u = url.strip()
    if u.startswith("git@"):
        return u
    for _ in range(10):
        hits = [m.start() for m in _SCHEME.finditer(u)]
        if len(hits) > 1:            # 内层还有 http(s)://，取最后一层
            u = u[hits[-1]:]
            continue
        m = re.match(r"(https?://)([^/]+)/(.*)$", u, re.I)
        if not m or m.group(2).lower() in _FORGES:
            break
        rest = m.group(3)
        for forge in _FORGES:        # 不带 scheme 的嵌套：proxy.com/github.com/u/r
            i = rest.lower().find(forge + "/")
            if i == 0 or (i > 0 and rest[i - 1] == "/"):
                u = "https://" + rest[i:]
                break
        else:
            break
    return u

def clean_proxy(proxy: str) -> str:
    p = (proxy or "").strip().rstrip("/")
    p = re.sub(r"/(https?:)?$", "", p, flags=re.I)   # 粘贴成 https://gh-proxy.com/https:// 的情况
    return p.rstrip("/")

def repo_parts(url: str) -> tuple[str, str, str]:
    """(host, user, repo)；git@host:user/repo.git 也支持。"""
    u = unproxy(url).rstrip("/")
    if u.startswith("git@"):
        host, _, path = u[4:].partition(":")
    else:
        u = re.sub(r"^https?://", "", u, flags=re.I)
        host, _, path = u.partition("/")
    segs = [x for x in path.split("/") if x]
    repo = segs[-1] if segs else host
    if repo.endswith(".git"):
        repo = repo[:-4]
    user = segs[-2] if len(segs) >= 2 else ""
    return host.lower(), user, repo

def repo_key(url: str) -> str:
    h, u, r = repo_parts(url)
    return f"{h}/{u}/{r}".lower()

def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "-", name).strip("-.") or "repo"

def origin_of(dest: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(dest), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def dest_for(store: Path, url: str, taken: dict[Path, str] | None = None, dup_names: set[str] | None = None) -> Path:
    """目录名默认 repo；重名时用 user_repo（再冲突就 host_user_repo，最后加序号）。
    taken: 本次/已有任务占用的目录 -> repo_key；dup_names: 本批里出现多次的 repo 名（小写）。
    磁盘上已存在的目录：origin 是同一个仓库就复用（更新），否则视为被占用。"""
    taken = taken or {}
    key = repo_key(url)
    host, user, repo = repo_parts(url)
    cands = [_safe(repo)]
    if user:
        cands.append(_safe(f"{user}_{repo}"))
        cands.append(_safe(f"{host.split('.')[0]}_{user}_{repo}"))
    if dup_names and repo.lower() in dup_names and len(cands) > 1:
        # 本批内重名：除非磁盘上的 repo 目录本来就是它，否则一律用 user_repo
        first = store / cands[0]
        if not ((first / ".git").exists() and repo_key(origin_of(first)) == key):
            cands = cands[1:]

    def free(d: Path) -> bool:
        if d in taken:
            return taken[d] == key
        if not d.exists():
            return True
        if (d / ".git").exists():
            return repo_key(origin_of(d)) == key
        return not any(d.iterdir())          # 空目录可用
    for c in cands:
        if free(store / c):
            return store / c
    base = cands[-1]
    for i in range(2, 1000):
        if free(store / f"{base}_{i}"):
            return store / f"{base}_{i}"
    return store / base

class Job:
    def __init__(self, url: str, dest: Path, proxy: str = ""):
        self.url = url
        self.dest = dest
        self.proxy = clean_proxy(proxy)
        self.status = "queued"  # queued, running, done, error, skipped
        self.progress = ""
        self.log = []
        self.proc = None
        self.start_time = 0.0
        self.error = ""      # git 的 fatal/error 原因（失败时）
        self.code = None     # git 退出码
        self.attempts = 0
        self.stop_req = False
        self._action = "clone"

    def to_dict(self):
        return {
            "url": self.url,
            "dest": str(self.dest),
            "status": self.status,
            "progress": self.progress,
            "log": self.log[-20:],
            "error": self.error,
            "code": self.code,
            "attempts": self.attempts,
            "proxy": self.proxy,
            "action": self._action,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls(d["url"], Path(d["dest"]), d.get("proxy", ""))
        j.status = d.get("status", "queued")
        if j.status in ("running", "queued"):
            j.status, j.progress = "stopped", "服务重启时中断，可点「状态恢复」继续"
        else:
            j.progress = d.get("progress", "")
        j.log, j.error, j.code = d.get("log", []), d.get("error", ""), d.get("code")
        j.attempts, j._action = d.get("attempts", 0), d.get("action", "clone")
        return j

def _settings_path() -> Path:
    base = os.environ.get("GITDSM_VAR") or os.environ.get("SYNOPKG_PKGVAR") or str(Path.home() / ".gitdsm")
    return Path(base) / "settings.json"

def load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_settings(upd: dict):
    p = _settings_path()
    d = load_settings(); d.update(upd)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except OSError as e:
        log(f"settings not saved: {e}")


def _jobs_path() -> Path:
    return _settings_path().with_name("jobs.json")


class Cloner:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.max_parallel = 3
        try:
            self.max_parallel = max(1, min(16, int(load_settings().get("max_parallel", 3))))
        except (TypeError, ValueError):
            pass
        self._load_jobs()

    def _load_jobs(self):
        try:
            data = json.loads(_jobs_path().read_text(encoding="utf-8"))
            for d in data:
                j = Job.from_dict(d)
                self.jobs[j.url] = j
        except Exception:
            pass

    def save(self):
        """把任务状态落盘，服务/NAS 重启后还能看到并恢复。"""
        try:
            with self.lock:
                data = [j.to_dict() for j in self.jobs.values()]
            p = _jobs_path(); p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            log(f"jobs not saved: {e}")

    def stop(self, urls: list[str] | None = None) -> int:
        """停止排队中/进行中的任务；urls 为空表示全部。中断的 clone 会清掉半成品目录。"""
        n = 0
        with self.lock:
            for job in self.jobs.values():
                if urls and job.url not in urls:
                    continue
                if job.status == "queued":
                    job.status, job.progress = "stopped", "已停止（未开始）"
                    n += 1
                elif job.status == "running":
                    job.stop_req = True
                    pr = job.proc
                    if pr and pr.poll() is None:
                        try:
                            os.killpg(pr.pid, 15)
                        except Exception:
                            pr.terminate()
                    n += 1
        self.save()
        return n

    def resume(self, urls: list[str] | None = None) -> int:
        """状态恢复：把已停止/中断的任务重新排队（已有 .git 就走更新）。"""
        n = 0
        with self.lock:
            for job in self.jobs.values():
                if job.status != "stopped" or (urls and job.url not in urls):
                    continue
                job._action = "pull" if (job.dest / ".git").exists() else "clone"
                job.status, job.progress, job.error = "queued", "恢复排队", ""
                n += 1
        self._start_next()
        return n

    def add(self, urls: list[str], store: Path, proxy: str = "", update: bool = True):
        proxy = clean_proxy(proxy)
        seen, clean = set(), []
        for u in urls:                       # 剥代理 + 同一仓库去重
            cu = unproxy(u)
            k = repo_key(cu)
            if k not in seen:
                seen.add(k); clean.append(cu)
        names = [repo_parts(u)[2].lower() for u in clean]
        dup = {n for n in names if names.count(n) > 1}
        with self.lock:
            taken = {j.dest: repo_key(j.url) for j in self.jobs.values()}
            for j in self.jobs.values():     # 和已有任务重名也算
                n = repo_parts(j.url)[2].lower()
                if n in names and repo_key(j.url) not in seen:
                    dup.add(n)
            for url in clean:
                old = self.jobs.get(url)
                if old and old.status in ("queued", "running"):
                    continue
                dest = dest_for(store, url, taken, dup)
                taken[dest] = repo_key(url)
                job = Job(url, dest, proxy)
                if dest.exists() and (dest / ".git").exists():
                    if update:
                        job.status = "queued"
                        job.progress = "will update"
                        job._action = "pull"
                    else:
                        job.status = "skipped"
                        job.progress = "already exists"
                else:
                    job.status = "queued"
                    job._action = "clone"
                self.jobs[url] = job
        self._start_next()
        self.save()
        return len(clean)

    def set_parallel(self, n) -> int:
        n = int(n)
        if not 1 <= n <= 16:
            raise ValueError("并发数需在 1-16 之间")
        self.max_parallel = n
        save_settings({"max_parallel": n})
        self._start_next()
        return n

    def _start_next(self):
        with self.lock:
            running = sum(1 for j in self.jobs.values() if j.status == "running")
            for job in self.jobs.values():
                if running >= self.max_parallel:
                    break
                if job.status == "queued":
                    self._start(job)
                    running += 1

    def _start(self, job: Job):
        job.status = "running"
        job.start_time = time.time()
        job.error, job.code, job.log = "", None, []
        job.attempts += 1
        job.stop_req = False
        action = getattr(job, "_action", "clone")
        fresh = not job.dest.exists()
        # 代理只在本次命令里生效（-c url.<代理>/https://.insteadOf=https://），
        # 不写进 .git/config，origin 始终是源站地址。
        pre = ["git"]
        if job.proxy:
            pre += ["-c", f"url.{job.proxy}/https://.insteadOf=https://"]
        if action == "pull":
            fix_origin(job.dest, job.url)  # 旧版本把代理写进了 origin，这里改回源站
            cmd = pre + ["-C", str(job.dest), "pull", "--progress", "--ff-only"]
        else:
            if job.dest.exists() and any(job.dest.iterdir()) and not (job.dest / ".git").exists():
                job.status, job.code = "error", None
                job.error = f"目标目录已存在且不是空目录，也不是 git 仓库：{job.dest}（请改名或删除后再重试）"
                job.progress = job.error
                return
            job.dest.parent.mkdir(parents=True, exist_ok=True)
            cmd = pre + ["clone", "--progress", "--depth", "1", job.url, str(job.dest)]
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # 需要账号密码时直接失败，不要卡住
        def run():
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    start_new_session=True,   # 停止时整组杀掉（git 会起子进程）
                )
                job.proc = proc
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        job.log.append(line)
                        if "%" in line or "Receiving" in line or "Resolving" in line or "Updating" in line or "Fast-forward" in line:
                            job.progress = line[:120]
                code = proc.wait()
                job.code = code
                if job.stop_req:
                    job.status, job.code, job.error = "stopped", None, ""
                    job.progress = "已停止"
                    if action == "clone" and fresh and job.dest.exists():
                        shutil.rmtree(job.dest, ignore_errors=True)   # 半成品目录，下次重新 clone
                elif code == 0:
                    job.status = "done"
                    job.progress = "updated" if action == "pull" else "completed"
                else:
                    job.status = "error"
                    job.error = explain_error(job.log, code, job.proxy)
                    job.progress = job.error
            except Exception as e:
                job.status = "error"
                job.error = str(e)
                job.progress = str(e)
            finally:
                job.proc = None
                self._start_next()
                self.save()
        threading.Thread(target=run, daemon=True).start()

    def retry(self, urls: list[str] | None = None, proxy: str | None = None) -> int:
        """把失败的任务重新排队；urls 为空表示全部失败任务。proxy 不为 None 时换成新代理。"""
        n = 0
        with self.lock:
            for job in self.jobs.values():
                if job.status != "error" or (urls and job.url not in urls):
                    continue
                if proxy is not None:
                    job.proxy = clean_proxy(proxy)
                job._action = "pull" if (job.dest / ".git").exists() else "clone"
                job.status, job.progress, job.error = "queued", "retry queued", ""
                n += 1
        self._start_next()
        self.save()
        return n

    def clear_finished(self) -> int:
        with self.lock:
            keep = {u: j for u, j in self.jobs.items() if j.status in ("queued", "running")}
            n = len(self.jobs) - len(keep)
            self.jobs = keep
        self.save()
        return n

    def list(self):
        with self.lock:
            return [j.to_dict() for j in self.jobs.values()]


def fix_origin(dest: Path, url: str) -> None:
    """origin 带了代理前缀（可能嵌套多层，旧版本遗留），改回源站地址。"""
    cur = origin_of(dest)
    if cur and cur != url and unproxy(cur) != cur and repo_key(cur) == repo_key(url):
        subprocess.run(["git", "-C", str(dest), "remote", "set-url", "origin", unproxy(url)], capture_output=True)


_HINTS = [
    ("could not resolve host", "域名解析失败：NAS 的 DNS 不通，或代理地址写错"),
    ("failed to connect", "连接失败：网络不通或被墙，可以换个代理再重试"),
    ("connection timed out", "连接超时：网络不通或代理太慢，可以换个代理再重试"),
    ("operation timed out", "连接超时：网络不通或代理太慢，可以换个代理再重试"),
    ("ssl", "TLS/证书错误：代理不支持 https，或 NAS 时间不对"),
    ("could not read username", "服务器要求登录：仓库不存在或是私有的；用代理时也可能是代理不支持这个地址"),
    ("authentication failed", "认证失败：私有仓库需要账号/Token"),
    ("repository not found", "仓库不存在，或者是私有仓库"),
    ("not found", "地址不存在（404）：仓库名写错，或代理不支持这个地址"),
    ("already exists and is not an empty directory", "目标目录已存在且非空"),
    ("permission denied", "没有写权限：检查套件运行账号对共享文件夹的权限"),
    ("dubious ownership", "目录属主和运行账号不一致：需要 git config --global --add safe.directory <目录>"),
    ("not possible to fast-forward", "本地有改动或分叉，无法快进更新"),
    ("rpc failed", "传输中断：仓库太大或网络不稳，重试即可"),
    ("early eof", "传输中断：仓库太大或网络不稳，重试即可"),
    ("gnutls", "传输中断：网络不稳，重试即可"),
]


def explain_error(lines: list[str], code: int, proxy: str = "") -> str:
    """从 git 输出里挑出 fatal/error 行，并给一句中文原因。"""
    bad = [l for l in lines if l.lower().startswith(("fatal:", "error:", "remote:")) or "fatal:" in l.lower()]
    msg = " | ".join(bad[-3:]) if bad else (lines[-1] if lines else "")
    low = msg.lower()
    hint = next((h for k, h in _HINTS if k in low), "")
    if proxy and "unable to access" in low and proxy.split("://")[-1].split("/")[0].lower() in low:
        hint = f"代理 {proxy} 连不上或不支持这个地址：去掉代理或换一个，再点重试"
    if not msg:
        msg = "git 没有输出任何信息"
        if proxy:
            hint = "用了代理但没有任何输出：多半是代理不可用，去掉代理或换一个再重试"
    head = f"exit {code}"
    return f"{head}：{hint} —— {msg}" if hint else f"{head} —— {msg}"

TREND_CACHE = {}
TREND_TTL = 600  # 10 min

def github_get(url: str, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "GitDSM/1.0", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_github_trending(kind: str, limit: int = 8, proxy=""):
    """实时从 GitHub 获取热门/最新仓库。"""
    now = time.time()
    hit = TREND_CACHE.get(kind)
    if hit and now - hit[0] < TREND_TTL:
        return hit[1]
    try:
        if kind == "hot":
            q = "stars:>5000"
            sort = "stars"
        elif kind == "latest":
            q = "stars:>1000"
            sort = "updated"
        else:
            q = "llm OR agent OR mcp stars:>500"
            sort = "stars"
        params = urllib.parse.urlencode({
            "q": q,
            "sort": sort,
            "order": "desc",
            "per_page": str(limit),
        })
        data = github_get(f"{proxy}https://api.github.com/search/repositories?{params}")
        items = data.get("items") or []
        rows = []
        for it in items[:limit]:
            rows.append({
                "name": it.get("full_name") or it.get("name"),
                "url": it.get("clone_url") or f"https://github.com/{it.get('full_name')}.git",
                "desc": (it.get("description") or "")[:80],
                "stars": it.get("stargazers_count", 0),
            })
        TREND_CACHE[kind] = (now, rows)
        return rows
    except Exception as e:
        log(f"trends {kind} failed: {e}")
        return hit[1] if hit else []

def get_trends():
    gh_proxy="https://gh-proxy.com/"  # 工具方法，非类成员，这里补上，如果有问题就换。
    return {
        "hot": fetch_github_trending("hot",proxy=gh_proxy),
        "latest": fetch_github_trending("latest",proxy=gh_proxy),
        "interest": fetch_github_trending("interest",proxy=gh_proxy),
    }


cloner = Cloner()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _norm(self):
        # The UI calls rpc/* (DSM's nginx blocks */api/* paths); /api/* stays for local/CLI use.
        if self.path.startswith("/rpc/"):
            self.path = "/api/" + self.path[5:]

    def do_GET(self):
        self._norm()
        if self.path == "/" or self.path.startswith("/?"):
            html = (WEBS / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/jobs":
            self._send(200, json.dumps(cloner.list()))
        elif self.path == "/api/example":
            ex = (ROOT / "repos.example.txt").read_text(encoding="utf-8", errors="replace")
            self._send(200, json.dumps({"text": ex}))
        elif self.path == "/api/config":
            self._send(200, json.dumps({"max_parallel": cloner.max_parallel}))
        elif self.path == "/api/trends":
            self._send(200, json.dumps(get_trends()))
        else:
            self._send(404, "not found")

    def do_POST(self):
        self._norm()
        if self.path in ("/api/retry", "/api/clear", "/api/config", "/api/stop", "/api/resume"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
            try:
                data = json.loads(raw or "{}")
                if self.path == "/api/config":
                    n = cloner.set_parallel(data.get("max_parallel"))
                    self._send(200, json.dumps({"ok": True, "max_parallel": n}))
                    return
                if self.path == "/api/stop":
                    n = cloner.stop(data.get("urls") or None)
                elif self.path == "/api/resume":
                    n = cloner.resume(data.get("urls") or None)
                elif self.path == "/api/retry":
                    n = cloner.retry(data.get("urls") or None, data.get("proxy"))
                else:
                    n = cloner.clear_finished()
                self._send(200, json.dumps({"ok": True, "count": n}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/clone":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8", "replace")
            try:
                data = json.loads(raw)
                text = data.get("text", "")
                proxy = data.get("proxy", "")
                store, urls = parse_requirements(text)
                n = cloner.add(urls, store, proxy)
                self._send(200, json.dumps({"ok": True, "count": n}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
        else:
            self._send(404, "not found")

def serve(host="127.0.0.1", port=8011):
    server = ThreadingHTTPServer((host, port), Handler)
    log(f"serving http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")

def cli_clone(args):
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    store, urls = parse_requirements(text, args.store)
    log(f"store: {store}")
    log(f"repos: {len(urls)}")
    cloner.add(urls, store, args.proxy or "")
    while True:
        jobs = cloner.list()
        done = sum(1 for j in jobs if j["status"] in ("done", "error", "skipped"))
        log(f"progress {done}/{len(jobs)}")
        if done == len(jobs):
            break
        time.sleep(2)

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8011)
    c = sub.add_parser("clone")
    c.add_argument("-r", "--file", required=True)
    c.add_argument("--store")
    c.add_argument("--proxy", default="")
    args = p.parse_args()
    if args.cmd == "serve":
        serve(args.host, args.port)
    elif args.cmd == "clone":
        cli_clone(args)
    else:
        # default serve
        serve()

if __name__ == "__main__":
    main()
