"""一键下载并替换当前 exe 的自动更新模块.

调用方(settings_dialog._on_update_now)只关心一个入口:
    download_and_apply_release(info: UpdateInfo,
                               progress_cb=..., cancel_cb=..., parent_log=...)
        -> ApplyResult

其余都是辅助:

- `current_exe_path()` 返回当前正在运行的 exe(打包时);源码运行时返回 None
- `download_with_progress(url, dest, ...)` 通用流式下载(可取消、可回调)
- `apply_pending_update(cfg, main_window)` 由主窗口 closeEvent 调用:
    若 cfg 有 pending_update_path → spawn swap helper 后返回 True
- `SWAP_HELPER_PS1` / `launch_swap_helper(...)` 由 PowerShell 干
    「等父进程退出 → 覆盖 → 启动」

整体安全要点:
1. 只在打包后(`sys.frozen`)替换运行中的 exe;源码运行只能打开下载页
2. 下载落地路径 = %TEMP%\\<APP>_<version>.exe,完成即转,失败删
3. swap helper 启动 30 秒内等不到父进程退出 → 放弃(不冒险覆盖)
4. 校验下载文件大小 < 30MB 视为不完整 → 放弃
5. 拷贝时 -Force 覆盖目标,启动新版本后顺手清掉 %TEMP% 里的临时 exe

完整性(审计报告 #3 —— 这条链路等于「以用户权限执行下载下来的代码」):
6. 下载地址必须是本仓库的 release 资产(`https://github.com/<repo>/releases/download/...`),
   其它 host / scheme 一律拒绝;
7. 下载完必须按发布时提供的 SHA-256(sidecar `.exe.sha256` 或 release 正文)校验,
   取不到校验值就**拒绝自动更新**(只给下载页链接),宁可不更也不冒被执行的风险;
8. swap helper 覆盖前先把旧 exe 备份成 `.bak`,复制或启动失败时回滚。
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from ..__init__ import __app_name__, __version__
from .paths import temp_dir
from .update_checker import (
    DEFAULT_REPO, UpdateInfo, fetch_expected_digest, is_allowed_download_url,
)

# === 常量 ===
USER_AGENT: str = f"{__app_name__}/{__version__} (Windows)"
MIN_VALID_EXE_SIZE: int = 30 * 1024 * 1024  # < 30MB 视为下载不完整
PROGRESS_CHUNK: int = 64 * 1024  # 64KB
HELPER_WAIT_TIMEOUT_SEC: int = 30
DIGEST_CHUNK: int = 1024 * 1024
# 没有 SHA-256 校验值时是否允许更新。默认 True(拒绝):#3 的根因就是下载即执行。
REQUIRE_SHA256: bool = True


@dataclasses.dataclass
class ApplyResult:
    """下载并触发替换的最终结果."""

    ok: bool
    downloaded_to: str = ""    # 本次下载的 exe 落地路径(若成功)
    error: str = ""            # 用户可读错误,空表示成功
    target_exe: str = ""       # 当前正在运行的 exe 路径
    sha256: str = ""           # 已通过校验的摘要(交给 closeEvent 的助手再核一次)


# === 路径探测 ===
def current_exe_path() -> Optional[Path]:
    """当前正在运行的 exe 路径。

    打包运行:sys.executable 就是 AutoFarmStation.exe
    源码运行:返回 None(无法自动替换,只能打开下载页)
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def _pending_dest(target_exe: Path, version: str) -> Path:
    """下载到 %TEMP%/<APP>_<version>.exe。"""
    safe = version.lstrip("v").replace("/", "_").replace("\\", "_")
    return temp_dir() / f"{__app_name__}_{safe}.exe"


# === 通用流式下载(可取消、可进度回调) ===
def download_with_progress(
    url: str,
    dest: Path,
    *,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> None:
    """把 url 流到 dest(覆盖)。progress_cb(recv_bytes, total_bytes)。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            recv = 0
            with open(dest, "wb") as f:
                while True:
                    if cancel_cb and cancel_cb():
                        raise _DownloadCancelled()
                    chunk = resp.read(PROGRESS_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    recv += len(chunk)
                    if progress_cb:
                        progress_cb(recv, total)
    except _DownloadCancelled:
        # 半截文件清理交给调用方
        raise
    except urllib.error.URLError as e:
        raise DownloadError(f"网络错误:{e.reason}") from e
    except Exception as e:  # noqa: BLE001
        raise DownloadError(f"下载失败:{type(e).__name__}:{e}") from e


class DownloadError(Exception):
    """下载阶段失败(网络、HTTP 错误、解码错误)。"""


class _DownloadCancelled(Exception):
    """用户取消。"""


def sha256_of_file(path: Path) -> str:
    """分块计算文件 SHA-256(十六进制小写)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(DIGEST_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# === 顶层入口 ===
def download_and_apply_release(
    info: UpdateInfo,
    *,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    log=None,
) -> ApplyResult:
    """下载 info 指向的 release exe,落地到 %TEMP% 并校验 SHA-256。

    settings_dialog 在用户确认后调用本函数:
    - 返回 ok=False:仅展示 error,不动 cfg
    - 返回 ok=True:settings_dialog 把 pending_update_path / pending_update_sha256
      写进 cfg,然后调主窗口 close()
    """
    if not info.download_url:
        return ApplyResult(ok=False, error="远端 release 没有 .exe 资产可供下载")
    # 1) 来源白名单:只允许本仓库的 release 下载地址(#3)
    if not is_allowed_download_url(info.download_url, DEFAULT_REPO):
        return ApplyResult(
            ok=False,
            error=(
                "下载地址不属于本仓库的 release 资产,已拒绝。"
                "请从 GitHub release 页面手动下载。"
            ),
        )
    target = current_exe_path()
    if target is None:
        return ApplyResult(
            ok=False,
            error="当前是源码运行,无法自动替换 exe。请到 release 页面手动下载。",
        )
    if not target.exists():
        return ApplyResult(ok=False, error=f"目标 exe 不存在:{target}")

    # 2) 取发布物声明的 SHA-256(sidecar 优先,其次 release 正文)
    expected = (fetch_expected_digest(info) or "").lower()
    if not expected and REQUIRE_SHA256:
        return ApplyResult(
            ok=False,
            error=(
                "该版本发布物没有提供 SHA-256 校验值,已拒绝自动更新"
                "(下载即执行的风险不可接受)。请到 release 页面手动下载,"
                "或等作者补上 .sha256 校验文件。"
            ),
        )

    dest = _pending_dest(target, info.latest_version)
    try:
        # 同名旧文件先删(避免占用)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        download_with_progress(
            info.download_url, dest,
            progress_cb=progress_cb, cancel_cb=cancel_cb,
        )
    except _DownloadCancelled:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return ApplyResult(ok=False, error="已取消下载")
    except DownloadError as e:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return ApplyResult(ok=False, error=str(e))

    # 体积自检
    try:
        size = dest.stat().st_size
    except OSError as e:
        return ApplyResult(ok=False, error=f"无法读取下载文件:{e}")
    if size < MIN_VALID_EXE_SIZE:
        try:
            dest.unlink()
        except OSError:
            pass
        return ApplyResult(
            ok=False,
            error=f"下载文件过小({size // 1024 // 1024}MB),疑似不完整,已删除",
        )

    # 3) 哈希校验:不符就删掉,绝不落到最终路径(#3)
    try:
        actual = sha256_of_file(dest)
    except OSError as e:
        try:
            dest.unlink()
        except OSError:
            pass
        return ApplyResult(ok=False, error=f"无法校验下载文件:{e}")
    if expected and actual != expected:
        try:
            dest.unlink()
        except OSError:
            pass
        if log:
            try:
                log.error("更新包哈希不匹配:期望 %s 实际 %s", expected, actual)
            except Exception:  # noqa: BLE001
                pass
        return ApplyResult(
            ok=False,
            error="下载文件的 SHA-256 与发布值不一致,已删除。可能是传输被篡改或资产被替换。",
        )

    if log:
        try:
            log.info("下载完成并通过校验: %s (%.1fMB) sha256=%s",
                     dest, size / 1024 / 1024, actual[:16])
        except Exception:  # noqa: BLE001
            pass
    return ApplyResult(
        ok=True, downloaded_to=str(dest), target_exe=str(target), sha256=actual)


# === 关闭时:实际触发替换 ===
def apply_pending_update(cfg, main_window) -> bool:
    """主窗口 closeEvent 调用:若 cfg 有 pending_update_path → spawn 助手并返回 True。

    返回 True 表示「正在去更新」,调用方应该紧接着 super().closeEvent(ev) / app.quit()。
    返回 False 表示没有 pending 更新,正常走旧路径。

    注意:调用前 cfg.save() 必须已经完成(若用户改了设置)。
    """
    pending = (cfg.get("settings.pending_update_path") or "").strip()
    if not pending:
        return False
    expected = (cfg.get("settings.pending_update_sha256") or "").strip().lower()
    target = (cfg.get("settings.pending_update_target_exe") or "").strip()
    if not target:
        # 没记目标 → 重新探测一次(理论上 cfg 里有,但防丢失)
        cur = current_exe_path()
        if cur is None:
            return False
        target = str(cur)

    def _clear_pending() -> None:
        cfg.set("settings.pending_update_path", "")
        cfg.set("settings.pending_update_target_exe", "")
        cfg.set("settings.pending_update_sha256", "")
        try:
            cfg.save()
        except Exception:  # noqa: BLE001
            pass

    pending_p = Path(pending)
    target_p = Path(target)
    if not pending_p.exists():
        _clear_pending()
        return False

    # 临覆盖前再核一次哈希:下载后这个 %TEMP% 文件被动过就不更新(#3)
    if REQUIRE_SHA256 and not expected:
        _clear_pending()
        return False
    if expected:
        try:
            actual = sha256_of_file(pending_p)
        except OSError:
            _clear_pending()
            return False
        if actual != expected:
            try:
                pending_p.unlink()
            except OSError:
                pass
            _clear_pending()
            return False

    parent_pid = os.getpid()
    try:
        launch_swap_helper(pending_p, target_p, parent_pid, expected_sha256=expected)
    except Exception as e:  # noqa: BLE001
        # 助手起不来 → 清掉 pending,主程序正常退出
        _clear_pending()
        if hasattr(main_window, "_log"):
            try:
                main_window._log.error("无法启动更新助手:%s", e)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return False

    # 标记主窗口「正在去更新」,closeEvent 别再弹确认 / 别杀游戏进程
    if main_window is not None:
        try:
            main_window._updating = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    return True


# === 交换助手 ===
SWAP_HELPER_PS1 = r"""
# AutoFarmStation 一键更新助手(由 utils/updater.py 写入临时文件并启动)
param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$NewExe,
    [Parameter(Mandatory=$true)][string]$TargetExe
)
$ErrorActionPreference = 'Stop'

function Write-UpdLog([string]$msg) {
    try {
        $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
        if ($env:AFS_UPDATER_LOG -and $env:AFS_UPDATER_LOG -ne 'NUL') {
            Add-Content -LiteralPath $env:AFS_UPDATER_LOG -Value $line -ErrorAction SilentlyContinue
        }
    } catch {}
}

function Restore-Backup {
    # 覆盖失败 / 启动失败时把旧 exe 放回去,别让用户既没了新版也没了旧版
    try {
        if ((Test-Path -LiteralPath $script:BackupExe) -and -not (Test-Path -LiteralPath $TargetExe)) {
            Copy-Item -LiteralPath $script:BackupExe -Destination $TargetExe -Force
            Write-UpdLog ("已回滚到旧版本 " + $TargetExe)
        }
    } catch {
        Write-UpdLog ("回滚失败: " + $_.Exception.Message)
    }
}

$script:BackupExe = $TargetExe + ".bak"
try {
    Write-UpdLog ("启动 ParentPid=" + $ParentPid + " NewExe=" + $NewExe + " TargetExe=" + $TargetExe)
    # 1) 等父进程退出,最多 30 秒
    $exited = $false
    try {
        $null = Wait-Process -Id $ParentPid -Timeout $env:AFS_UPDATER_TIMEOUT -ErrorAction Stop
        $exited = $true
    } catch {
        $proc = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
        if (-not $proc) { $exited = $true }
    }
    if (-not $exited) {
        Write-UpdLog "父进程未在超时内退出 exit=2"
        exit 2
    }
    Write-UpdLog "父进程已退出"

    # 2) 校验下载文件存在 + 大小
    if (-not (Test-Path -LiteralPath $NewExe)) {
        Write-UpdLog "下载文件不存在 exit=3"
        exit 3
    }
    $size = (Get-Item -LiteralPath $NewExe).Length
    $minSize = [int]$env:AFS_UPDATER_MIN_SIZE
    if ($size -lt $minSize) {
        Write-UpdLog ("下载文件过小 " + $size + " bytes exit=4")
        exit 4
    }

    # 2b) 覆盖前再核一次 SHA-256(主程序下载时已核过,这里防 %TEMP% 里被掉包)(#3)
    $expect = ($env:AFS_UPDATER_SHA256 | Out-String).Trim().ToLower()
    if ($expect) {
        $actual = (Get-FileHash -LiteralPath $NewExe -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expect) {
            Write-UpdLog ("哈希不一致,放弃覆盖 expect=" + $expect + " actual=" + $actual + " exit=5")
            exit 5
        }
        Write-UpdLog "SHA-256 校验通过"
    }

    # 3) 备份旧 exe,再拷贝新 exe 覆盖目标
    if (Test-Path -LiteralPath $TargetExe) {
        try {
            Copy-Item -LiteralPath $TargetExe -Destination $script:BackupExe -Force
            Write-UpdLog ("已备份旧版本 " + $script:BackupExe)
        } catch {
            Write-UpdLog ("备份失败,放弃覆盖 exit=6: " + $_.Exception.Message)
            exit 6
        }
    }
    Copy-Item -LiteralPath $NewExe -Destination $TargetExe -Force
    Write-UpdLog ("已覆盖目标 " + $TargetExe)

    # 4) 启动新版本
    try {
        Start-Process -FilePath $TargetExe
    } catch {
        Write-UpdLog ("启动新版本失败,准备回滚: " + $_.Exception.Message)
        Restore-Backup
        exit 7
    }
    Write-UpdLog "已启动新版本"

    # 5) 清掉 %TEMP% 里的临时下载;备份文件留着(下次更新前会被覆盖)
    try {
        Remove-Item -LiteralPath $NewExe -Force -ErrorAction SilentlyContinue
    } catch {}
    Write-UpdLog "完成 exit=0"
    exit 0
} catch {
    Restore-Backup
    Write-UpdLog ("异常 exit=1: " + $_.Exception.Message)
    exit 1
}
"""


def _detect_log_path() -> Optional[Path]:
    """把助手日志写到 %APPDATA%\\<APP>\\logs\\updater.log,失败则返回 None(助手不写日志)。"""
    try:
        from .paths import user_log_dir
        log = user_log_dir() / "updater.log"
        return log
    except Exception:  # noqa: BLE001
        return None


def launch_swap_helper(
    new_exe: Path,
    target_exe: Path,
    parent_pid: int,
    *,
    expected_sha256: str = "",
) -> None:
    """启动 PowerShell 助手:detach、不开窗、参数透传。

    ``expected_sha256`` 交给助手在覆盖前再核一次(#3)。
    """
    # 1) 脚本落地到 %TEMP%(UTF-8 BOM,让 PS5.1 也能识别中文路径)
    tmpdir = Path(tempfile.gettempdir())
    script_path = tmpdir / f"{__app_name__}_updater_{os.getpid()}.ps1"
    script_path.write_text(SWAP_HELPER_PS1, encoding="utf-8-sig")

    log_path = _detect_log_path()
    env = os.environ.copy()
    env["AFS_UPDATER_TIMEOUT"] = str(HELPER_WAIT_TIMEOUT_SEC)
    env["AFS_UPDATER_MIN_SIZE"] = str(MIN_VALID_EXE_SIZE)
    env["AFS_UPDATER_SHA256"] = (expected_sha256 or "").lower()
    env["AFS_UPDATER_LOG"] = str(log_path) if log_path is not None else "NUL"

    # DETACHED_PROCESS(0x8)+CREATE_NO_WINDOW(0x08000000):不挂控制台、不弹窗
    flags = 0x00000008 | 0x08000000
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", str(script_path),
        "-ParentPid", str(parent_pid),
        "-NewExe", str(new_exe),
        "-TargetExe", str(target_exe),
    ]
    subprocess.Popen(  # noqa: S603  (启动的是 PowerShell 助手,非用户输入)
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


# === 自检辅助 ===
def build_fake_release(tag: str = "v9.9.9", asset_name: str = "AutoFarmStation.exe",
                       asset_size: int = 65_000_000) -> UpdateInfo:
    """构造一个 UpdateInfo 给 UI/自检用(不去 GitHub 联网).

    下载地址刻意用「本仓库 release」的合法前缀,这样自检走的是和真实更新
    一样的白名单分支(#3);故意写成别的 host 的用例请自行构造。
    """
    return UpdateInfo(
        has_update=True,
        latest_version=tag,
        current_version=__version__,
        release_url=f"https://github.com/{DEFAULT_REPO}/releases/tag/{tag}",
        release_notes="(fake release notes)",
        download_url=f"https://github.com/{DEFAULT_REPO}/releases/download/{tag}/{asset_name}",
        asset_name=asset_name,
        asset_size=asset_size,
    )