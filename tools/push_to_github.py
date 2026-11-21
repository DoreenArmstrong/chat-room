#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过 GitHub REST API 把当前仓库推送到远端，并可选地创建 Release 上传 APK。

适用场景：`github.com:443` 被网络挡住，但 `api.github.com` 和 `uploads.github.com` 能通
（本项目就遇到过这种情况：网页打不开、git push 超时，但 API 正常）。

依赖：Python 标准库 + 本机 git（只用来列出文件和算 blob 哈希）。

用法：
    set GITHUB_TOKEN=ghp_xxxxxxxx
    python tools\\push_to_github.py --repo DoreenArmstrong/chat-room --dry-run
    python tools\\push_to_github.py --repo DoreenArmstrong/chat-room

    # 顺便发 Release（会把 APK 作为附件上传）
    python tools\\push_to_github.py --repo DoreenArmstrong/chat-room ^
        --release-tag v3.0.0 --release-name "v3.0.0 多房间 / 图片 / 头像" ^
        --release-notes RELEASE_NOTES.md --asset android\\build\\chatroom-v3.apk

Token 也可以放在 C:\\Users\\Kiki\\github_token.txt（一行，别提交进仓库）。
Token 权限：classic PAT 勾 `repo`；fine-grained 需要 Contents: Read/Write。
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path.home() / "github_token.txt"

GIT_CANDIDATES = [
    os.environ.get("GIT_EXE", ""),
    r"C:\Users\Kiki\Desktop\前后端开发环境\Git\cmd\git.exe",
    "git",
]


def find_git():
    for candidate in GIT_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() or candidate == "git":
            return candidate
    raise SystemExit("找不到 git，可设置环境变量 GIT_EXE 指向 git.exe")


def git(args):
    result = subprocess.run(
        [find_git()] + args, cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def load_token(explicit_file=None):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    path = Path(explicit_file) if explicit_file else TOKEN_FILE
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def api(method, url_or_path, token, body=None, raw=None, content_type=None):
    url = url_or_path if url_or_path.startswith("http") else API + url_or_path
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "chat-room-release-tool",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload.decode("utf-8")) if payload else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} 失败：HTTP {exc.code}\n{detail}")


def blob_sha(data):
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


def local_files():
    """index 里的文件（自动遵守 .gitignore，不会带 chat.db / .venv / .toolchain）"""
    out = git(["ls-files", "-z"])
    names = [n for n in out.split("\0") if n]
    files = {}
    for name in names:
        path = ROOT / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        files[name] = {"data": data, "sha": blob_sha(data), "mode": "100644"}
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="")
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--release-tag", default=None)
    parser.add_argument("--release-name", default=None)
    parser.add_argument("--release-notes", default=None)
    parser.add_argument("--asset", action="append", default=[])
    args = parser.parse_args()

    token = load_token(args.token_file)
    if not token and not args.dry_run:
        raise SystemExit(
            "没有 GITHUB_TOKEN。把 token 写到 %s，或先设环境变量。\n"
            "（dry-run 不需要 token）" % TOKEN_FILE
        )

    # 1) 远端分支的最新提交
    status, ref = api("GET", f"/repos/{args.repo}/git/ref/heads/{args.branch}", token)
    base_commit = ref["object"]["sha"]
    status, commit = api("GET", f"/repos/{args.repo}/git/commits/{base_commit}", token)
    base_tree = commit["tree"]["sha"]
    print(f"远端 {args.branch} = {base_commit[:10]}（tree {base_tree[:10]}）")

    status, tree = api("GET", f"/repos/{args.repo}/git/trees/{base_tree}?recursive=1", token)
    remote = {e["path"]: e for e in tree.get("tree", []) if e["type"] == "blob"}

    files = local_files()
    added, changed, unchanged = [], [], []
    for path, info in sorted(files.items()):
        old = remote.get(path)
        if old is None:
            added.append(path)
        elif old["sha"] != info["sha"]:
            changed.append(path)
        else:
            unchanged.append(path)
    deleted = sorted(set(remote) - set(files))

    print(f"本地 {len(files)} 个文件：新增 {len(added)} / 修改 {len(changed)} / "
          f"未变 {len(unchanged)} / 删除 {len(deleted)}")
    for label, items in (("新增", added), ("修改", changed), ("删除", deleted)):
        for path in items[:20]:
            print(f"  {label}: {path}")
        if len(items) > 20:
            print(f"  {label}: … 还有 {len(items) - 20} 个")

    if args.dry_run:
        print("\ndry-run：只读了远端信息，没有写入。")
        return

    if not added and not changed and not deleted:
        print("远端已经是最新内容，无需推送。")
        new_commit = base_commit
    else:
        # 2) 上传变化的 blob（未变的直接复用远端 sha）
        entries = []
        for path, info in sorted(files.items()):
            old = remote.get(path)
            if old is not None and old["sha"] == info["sha"]:
                sha = old["sha"]
            else:
                status, blob = api("POST", f"/repos/{args.repo}/git/blobs", token, body={
                    "content": base64.b64encode(info["data"]).decode("ascii"),
                    "encoding": "base64",
                })
                sha = blob["sha"]
                print(f"  上传 {path} → {sha[:10]}")
            entries.append({"path": path, "mode": info["mode"], "type": "blob", "sha": sha})

        # 3) 新 tree / commit / 更新分支
        message = args.message or git(["log", "-1", "--pretty=%B"]).strip()
        status, new_tree = api("POST", f"/repos/{args.repo}/git/trees", token,
                               body={"tree": entries})
        status, new_commit_obj = api("POST", f"/repos/{args.repo}/git/commits", token, body={
            "message": message,
            "tree": new_tree["sha"],
            "parents": [base_commit],
        })
        new_commit = new_commit_obj["sha"]
        api("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}", token, body={
            "sha": new_commit,
            "force": False,
        })
        print(f"已推送：{new_commit}")

    # 4) 可选：创建 Release 并上传附件
    if args.release_tag:
        body = ""
        if args.release_notes:
            body = (ROOT / args.release_notes).read_text(encoding="utf-8")
        status, release = api("POST", f"/repos/{args.repo}/releases", token, body={
            "tag_name": args.release_tag,
            "target_commitish": args.branch,
            "name": args.release_name or args.release_tag,
            "body": body,
            "draft": False,
            "prerelease": False,
        })
        upload_url = release["upload_url"].split("{")[0]
        print(f"Release 已创建：{release['html_url']}")

        for asset in args.asset:
            path = (ROOT / asset) if not Path(asset).is_absolute() else Path(asset)
            data = path.read_bytes()
            status, uploaded = api(
                "POST",
                f"{upload_url}?name={urllib.parse.quote(path.name)}",
                token, raw=data, content_type="application/vnd.android.package-archive",
            )
            print(f"  附件已上传：{uploaded['name']}（{len(data) // 1024} KB）")


if __name__ == "__main__":
    main()
