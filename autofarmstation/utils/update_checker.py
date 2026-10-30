"""检查更新.

读 GitHub releases/latest 与本地 __version__ 比对,返回是否有新版本.
纯标准库,无需联网库.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Final

from ..__init__ import __app_name__, __version__, __author_handle__

# === 常量(可在自检里覆盖) ===
DEFAULT_REPO: Final[str] = f"{__author_handle__}/AutoFarmStation"
LATEST_URL: Final[str] = "https://api.github.com/repos/{repo}/releases/latest"
API_TIMEOUT: Final[float] = 5.0
USER_AGENT: Final[str] = f"{__app_name__}/{__version__} (Windows)"
# 资产名旁边那份纯文本 sha256(单行十六进制,可带 "  文件名" 后缀)
DIGEST_SIDECAR_SUFFIX: Final[str] = ".sha256"
# release 正文里也接受这种写法:SHA256: <hex> / sha256=<hex>
_BODY_DIGEST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:sha256|哈希)\s*[:=]\s*([0-9a-f]{64})", re.IGNORECASE)


@dataclass
class UpdateInfo:
    """更新检查结果."""

    has_update: bool
    latest_version: str
    current_version: str
    release_url: str
    release_notes: str
    # 新增(供「立即更新」按钮用):首个 .exe 资产的直接下载 URL / 大小 / 文件名。
    # 可能为空(没有 .exe 资产 / 没联网 / 没找到匹配)。
    download_url: str = ""
    asset_name: str = ""
    asset_size: int = 0
    # 发布物对应的 SHA-256(sidecar 文件或 release 正文提供);空 = 发布者没给校验值
    expected_sha256: str = ""
    # sidecar(.exe.sha256)的直接下载地址;点击「立即更新」后才去取内容
    sha256_url: str = ""
    error: str = ""


def release_download_prefix(repo: str = DEFAULT_REPO) -> str:
    """本仓库 release 资产的合法下载前缀(#3 的 URL 白名单)."""
    return f"https://github.com/{repo}/releases/download/"


def is_allowed_download_url(url: str, repo: str = DEFAULT_REPO) -> bool:
    """下载地址是否属于本仓库的 release 资产.

    更新流程是「下载 → 覆盖自身 exe → 重新启动」,等于以用户权限执行任意代码,
    所以只认这一个前缀:必须是 https、必须是本仓库、必须在 releases/download 下。
    """
    if not url:
        return False
    return url.startswith(release_download_prefix(repo))


def _normalize_digest(raw: str) -> str:
    """从 sidecar 文本里取出 64 位十六进制摘要(容忍 `hash  文件名` 形式)."""
    s = str(raw or "").strip().lower()
    m = re.search(r"[0-9a-f]{64}", s)
    return m.group(0) if m else ""


def _pick_exe_asset(
    assets: list, repo: str = DEFAULT_REPO
) -> tuple[str, str, int, str]:
    """挑第一个「合法来源」的 .exe 资产,返回 (url, name, size, expected_sha256).

    非本仓库 releases/download 前缀的 URL 一律忽略(#3):即便有人改动了
    release 资产描述,也不会把我们引到别的 host 上执行。
    """
    by_name: dict[str, str] = {}
    for a in assets or []:
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if name.lower().endswith(DIGEST_SIDECAR_SUFFIX) and is_allowed_download_url(url, repo):
            by_name[name[: -len(DIGEST_SIDECAR_SUFFIX)].lower()] = url
    for a in assets or []:
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        size = int(a.get("size") or 0)
        if not name.lower().endswith(".exe") or not url:
            continue
        if not is_allowed_download_url(url, repo):
            continue
        return url, name, size, by_name.get(name.lower(), "")
    return "", "", 0, ""


def _parse_version(v: str) -> tuple[int, ...]:
    """解析版本号,容忍 '1.2.3' / 'v1.2.3' / '1.2.3-beta1'."""
    s = v.strip()
    if s.lower().startswith("v"):
        s = s[1:]
    m = re.match(r"^(\d+(?:\.\d+)*)", s)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """latest > current ?."""
    a, b = _parse_version(latest), _parse_version(current)
    # 长度对齐(短的补 0)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def check(repo: str = DEFAULT_REPO, current: str = __version__) -> UpdateInfo:
    """联网检查更新.失败时 has_update=False,error 填错误描述."""
    info = UpdateInfo(
        has_update=False,
        latest_version=current,
        current_version=current,
        release_url=f"https://github.com/{repo}/releases/latest",
        release_notes="",
    )
    try:
        req = urllib.request.Request(
            LATEST_URL.format(repo=repo),
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        info.error = f"网络错误:{e.reason}"
        return info
    except Exception as e:  # noqa: BLE001
        info.error = f"检查失败:{type(e).__name__}: {e}"
        return info

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        info.error = "远端未发布版本"
        return info
    info.latest_version = tag
    info.release_url = str(payload.get("html_url") or info.release_url)
    info.release_notes = str(payload.get("body") or "")
    # 解析首个 .exe 资产(供「立即更新」按钮)
    info.download_url, info.asset_name, info.asset_size, sha256_url = _pick_exe_asset(
        payload.get("assets") or [], repo
    )
    info.sha256_url = sha256_url
    # 没带 sidecar 时,退一步从 release 正文里找 `SHA256: <hex>`
    if not sha256_url:
        m = _BODY_DIGEST_RE.search(info.release_notes or "")
        if m:
            info.expected_sha256 = m.group(1).lower()
    info.has_update = is_newer(tag, current)
    return info


def fetch_expected_digest(info: UpdateInfo, *, timeout: float = API_TIMEOUT) -> str:
    """取发布物的 SHA-256:优先 sidecar 文件,其次 release 正文.取不到返回 ''.

    只在用户点了「立即更新」之后调用,不放到每小时一次的检查里,免得白跑一趟网络。
    """
    if info.expected_sha256:
        return info.expected_sha256.lower()
    if not info.sha256_url or not is_allowed_download_url(info.sha256_url):
        return ""
    try:
        req = urllib.request.Request(
            info.sha256_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(4096).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    digest = _normalize_digest(text)
    info.expected_sha256 = digest
    return digest


# 避免 import 时意外触发网络
_check_on_import = False  # noqa: F841


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(check().__dict__, indent=2, ensure_ascii=False))
    sys.exit(0)