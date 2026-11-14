"""
按账号（凭证 email）限制并发请求数。

配置项：account_concurrency（面板「并发控制」页可改，0 = 不限制）
环境变量：ACCOUNT_CONCURRENCY（设置后优先级高于面板配置）

用法（只在真正发往上游的请求处包一层）：
    from src import acct_limit
    async with acct_limit.slot(email):
        ...
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Dict, Tuple

from log import log
from config import get_account_concurrency

# (email, limit) -> Semaphore；限流值变更后生成新信号量，旧的自然排空退出
_sems: Dict[Tuple[str, int], asyncio.Semaphore] = {}
_inflight: Dict[str, int] = {}
_effective_limit = 0
_last_read = 0.0
_CACHE_TTL = 5.0  # 配置缓存秒数，允许面板热改后 5 秒内生效


async def _resolve_limit() -> int:
    global _effective_limit, _last_read
    now = time.time()
    if now - _last_read < _CACHE_TTL:
        return _effective_limit
    _last_read = now
    try:
        _effective_limit = await get_account_concurrency()
    except Exception:
        _effective_limit = 0
    return _effective_limit


async def get_limit() -> int:
    """当前生效的每账号并发上限（0 = 不限制）"""
    return await _resolve_limit()


async def get_inflight(email: str = "") -> int:
    """当前在飞请求数（可选按账号）"""
    if email:
        return _inflight.get(email, 0)
    return sum(_inflight.values())


@asynccontextmanager
async def slot(email: str):
    """占用一个账号并发槽位；limit<=0 时直接放行。"""
    limit = await _resolve_limit()
    if limit <= 0 or not email:
        yield
        return

    key = (email, limit)
    sem = _sems.get(key)
    if sem is None:
        sem = _sems[key] = asyncio.Semaphore(limit)
        log.info(f"[ACCT LIMIT] {email} 并发上限={limit}")

    if sem.locked():
        log.debug(f"[ACCT LIMIT] {email} 已达上限 {limit}，排队等待")

    await sem.acquire()
    _inflight[email] = _inflight.get(email, 0) + 1
    try:
        yield
    finally:
        _inflight[email] = max(0, _inflight.get(email, 1) - 1)
        sem.release()


def reset_state() -> None:
    """测试用：清空信号量与计数"""
    _sems.clear()
    _inflight.clear()
