"""GitHub Releases 更新检查、下载和安装脚本生成。

只访问 https://api.github.com/repos/<REPO>/releases/latest 与 Release 里的
HTTPS 资源地址；任何错误都以 UpdateError 返回给界面，不影响机器人运行。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "xueze-ai/wx-helper"
USER_AGENT = "wx-helper-updater"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


def parse_version(value):
    if not isinstance(value, str):
        return ()
    numbers = re.findall(r"\d+", value)
    return tuple(int(item) for item in numbers[:4])


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def _api_url(repo):
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo.strip()):
        raise UpdateError("更新仓库格式应为 owner/repo")
    return f"https://api.github.com/repos/{repo.strip()}/releases/latest"


def check_latest(repo=REPO, timeout=10):
    """读取最新 Release；没有 Release 时抛出 UpdateError。"""
    request = Request(_api_url(repo), headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as error:
        if error.code == 404:
            raise UpdateError("GitHub 仓库还没有发布 Release") from None
        if error.code == 403:
            raise UpdateError("GitHub 接口访问受限，请稍后再试") from None
        raise UpdateError(f"检查更新失败：HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise UpdateError("检查更新失败：无法连接 GitHub") from None
    if len(raw) > 2 * 1024 * 1024:
        raise UpdateError("Release 信息过大")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise UpdateError("Release 信息格式不正确") from None
    if not isinstance(data, dict):
        raise UpdateError("Release 信息格式不正确")
    tag = data.get("tag_name") or data.get("name") or ""
    assets = []
    for item in data.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        if name and url.startswith("https://"):
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            assets.append({"name": name[:160], "url": url, "size": max(0, size)})
    return {
        "tag": str(tag),
        "version": str(tag).lstrip("vV"),
        "url": data.get("html_url") or f"https://github.com/{repo.strip()}/releases",
        "notes": str(data.get("body") or "")[:4000],
        "published_at": str(data.get("published_at") or ""),
        "assets": assets,
    }


def choose_asset(assets):
    """优先选择 Windows 目录压缩包，其次单文件 EXE。"""
    def score(item):
        name = item["name"].lower()
        if not (name.endswith(".zip") or name.endswith(".exe")):
            return -1
        value = 0
        if name.endswith(".zip"):
            value += 20
        if name.endswith(".exe"):
            value += 10
        if "wx-helper" in name or "wx_helper" in name:
            value += 8
        if "win" in name:
            value += 5
        if "setup" in name or "install" in name:
            value -= 4
        if "sha" in name or ".sig" in name or ".txt" in name:
            value -= 60
        return value
    candidates = [item for item in assets if score(item) >= 0]
    if not candidates:
        return None
    return max(candidates, key=score)


def download(asset, directory, progress=None, timeout=60):
    """下载资源到 directory，返回本地路径；支持断点复用已下载完整文件。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(asset["name"]).name
    expected = int(asset.get("size") or 0)
    if target.exists() and (expected <= 0 or target.stat().st_size == expected):
        return target
    temporary = target.with_name(target.name + ".part")
    request = Request(asset["url"], headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response, open(temporary, "wb") as handle:
            total = int(response.headers.get("Content-Length") or expected or 0)
            received = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise UpdateError("更新包超过 300MB，已停止下载")
                handle.write(chunk)
                if progress is not None:
                    progress(received, total)
    except HTTPError as error:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：{error}") from None
    if expected > 0 and temporary.stat().st_size != expected:
        temporary.unlink(missing_ok=True)
        raise UpdateError("下载的更新包不完整")
    os.replace(temporary, target)
    return target


def stage(asset_path, directory):
    """解压/整理更新包，返回 (stage_root, exe_path)。stage_root 下的内容会复制到程序目录。"""
    asset_path, directory = Path(asset_path), Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = asset_path.suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(asset_path) as archive:
                for info in archive.infolist():
                    target = (directory / info.filename).resolve()
                    if not target.is_relative_to(directory.resolve()):
                        raise UpdateError("更新包包含不安全的路径")
                archive.extractall(directory)
        except zipfile.BadZipFile:
            raise UpdateError("更新包不是有效的 ZIP 文件") from None
        exe = _find_executable(directory)
        if exe is None:
            raise UpdateError("更新包里没有找到 wx-helper.exe")
        return exe.parent, exe
    if suffix == ".exe":
        target = directory / "wx-helper.exe"
        shutil.copy2(asset_path, target)
        return directory, target
    raise UpdateError("不支持的更新包格式（仅支持 .zip 或 .exe）")


def _find_executable(directory):
    matches = sorted(directory.rglob("wx-helper.exe"), key=lambda path: len(path.parts))
    return matches[0] if matches else None


INSTALL_SCRIPT = r"""param(
    [Parameter(Mandatory=$true)][int]$WaitPid,
    [Parameter(Mandatory=$true)][string]$Stage,
    [Parameter(Mandatory=$true)][string]$AppDir,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$Log,
    [switch]$Autostart
)
$ErrorActionPreference = 'Stop'
function Write-Log([string]$Message) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" | Add-Content -LiteralPath $Log -Encoding UTF8
}
try {
    Write-Log "等待进程 $WaitPid 退出"
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 1
    $exclude = @('bot_config.json', 'llm_config.json', 'members_config.json', '.runtime', 'logs')
    foreach ($item in Get-ChildItem -LiteralPath $Stage -Force) {
        if ($exclude -contains $item.Name) { continue }
        $destination = Join-Path $AppDir $item.Name
        if ($item.PSIsContainer) {
            Write-Log "合并目录 $($item.Name)"
            New-Item -ItemType Directory -Force -Path $destination | Out-Null
            Copy-Item -Path (Join-Path $item.FullName '*') -Destination $destination -Recurse -Force
        } else {
            Write-Log "替换文件 $($item.Name)"
            Copy-Item -LiteralPath $item.FullName -Destination $destination -Force
        }
    }
    Write-Log "更新完成，启动新版本"
    if ($Autostart) {
        Start-Process -FilePath $Exe -WorkingDirectory $AppDir -ArgumentList '--autostart'
    } else {
        Start-Process -FilePath $Exe -WorkingDirectory $AppDir
    }
} catch {
    Write-Log "更新失败：$_"
    try {
        Start-Process -FilePath $Exe -WorkingDirectory $AppDir
    } catch {
        Write-Log "旧版本启动失败：$_"
    }
}
"""


def write_install_script(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(INSTALL_SCRIPT, encoding="utf-8-sig")
    return path
