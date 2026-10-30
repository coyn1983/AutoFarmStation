"""日志模块.

- 主日志: %APPDATA%\\AutoFarmStation\\logs\\autofarmstation.log
- 自动轮转(单文件 2MB,保留 5 个)
- 所有写入自动过 Sanitize 脱敏
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .paths import user_log_dir
from .sanitize import sanitize

_LOGGER_NAME = "autofarmstation"
_LOG_FILE = "autofarmstation.log"


class _SanitizeFilter(logging.Filter):
    """日志过滤器:对所有 record 的 getMessage() 结果做脱敏.

    注意:这**只**覆盖消息本身。``logger.exception()`` / ``exc_info=True`` 的
    traceback 是 Formatter 另外追加的,不经过这里 —— 所以真正的脱敏在
    ``_SanitizingFormatter`` 里对整行做(#14)。本过滤器保留是为了让非本项目
    的 handler 也能拿到已脱敏的 message。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            sanitized = sanitize(msg)
            # 直接覆写 formatted message
            record.msg = sanitized
            record.args = ()
        except Exception:  # noqa: BLE001
            pass
        return True


class _SanitizingFormatter(logging.Formatter):
    """对**最终整行**(含 exc_info 栈、extra 文本)做脱敏的 Formatter(#14).

    traceback 里的文件绝对路径带用户名,以前会原样写进 autofarmstation.log,
    并被日志查看器的「复制全部」带走。
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        try:
            line = super().format(record)
        except Exception as e:  # noqa: BLE001
            line = f"{record.levelname}: (日志格式化失败 {type(e).__name__})"
        try:
            return sanitize(line)
        except Exception:  # noqa: BLE001
            return line


_INITIALIZED = False


def setup(level: int = logging.INFO) -> logging.Logger:
    """初始化日志(幂等,只生效一次)."""
    global _INITIALIZED
    logger = logging.getLogger(_LOGGER_NAME)
    if _INITIALIZED:
        return logger
    _INITIALIZED = True

    logger.setLevel(level)
    logger.propagate = False

    fmt = _SanitizingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sf = _SanitizeFilter()

    # 控制台
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    sh.addFilter(sf)
    logger.addHandler(sh)

    # 文件(自动轮转)
    log_path: Path = log_file_path()
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        fh.addFilter(sf)
        logger.addHandler(fh)
    except Exception:
        # 文件日志失败不影响控制台
        pass

    return logger


def get() -> logging.Logger:
    """获取全局 logger(自动 setup)."""
    if not _INITIALIZED:
        setup()
    return logging.getLogger(_LOGGER_NAME)


def log_file_path() -> Path:
    """当前日志文件路径(界面里查看日志 / 打开目录用)."""
    return user_log_dir() / _LOG_FILE