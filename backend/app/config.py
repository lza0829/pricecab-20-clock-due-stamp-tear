from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parents[2] / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pricecab.db"
# 「拨钟优先」的中止信号放在独立信号库：
#  - 应用持锁推进时持有主库写事务，拨钟必须仍能写下信号，因此不能放主库；
#  - 信号写入与应用提交必须互斥，否则信号可能恰好落在「提交前检查」与 commit
#    之间，应用就会带着新钟尚未生效的旧状态提交。用信号库的写事务当闸门：
#    应用在持闸门期间提交，拨钟只能在提交之后写入信号。
SIGNAL_DB_PATH = DATA_DIR / "pricecab.signal.db"
# 冲突 / 中止回执单独存一个审计库：应用持锁时握着主库写事务，
# 拨钟的「被拒」回执不能因此被阻塞。
AUDIT_DB_PATH = DATA_DIR / "pricecab.audit.db"
PROJECT = "pricecab"
PROJECT_NAME = "价签柜"

# 业务钟与应用到期共享生效临界区。冲突让路策略全局只有一种，不能并存：
#   apply_first —— 应用优先：应用持锁期间一切拨钟立即失败；拨钟不得改钟，
#                  也不得迫使应用回滚。
#   clock_first —— 拨钟优先：应用持锁期间收到拨钟，应用必须中止并回滚本批
#                  已写价签/历史/单状态，然后允许拨钟生效；应用回执记冲突中止，
#                  不得留下半截 applied。
ALLOWED_POLICIES = ("apply_first", "clock_first")
POLICY_LABELS = {"apply_first": "应用优先", "clock_first": "拨钟优先"}

_env_policy = os.environ.get("APPLY_CONFLICT_POLICY", "apply_first").strip().lower()
if _env_policy not in ALLOWED_POLICIES:
    raise ValueError(
        f"APPLY_CONFLICT_POLICY 只能是 {ALLOWED_POLICIES} 之一，当前={_env_policy!r}"
    )
# 环境变量只给初始默认值；运行期生效值存于 settings，同一时刻只有一种。
DEFAULT_CONFLICT_POLICY = _env_policy

# 锁记录超过该时长仍未完成，视为失效：下一次获取可以回收（栅栏 +1）。
# 原持有者在提交前复核 fence 必然失败，只能整批回滚，绝不会改写他人已成功批次。
APPLY_LOCK_TIMEOUT_SECONDS = float(os.environ.get("APPLY_LOCK_TIMEOUT_SECONDS", "30"))
# 应用让路：对方是拨钟（短临界区）时最多等这么久；对方是另一应用立即冲突。
APPLY_LOCK_WAIT_SECONDS = float(os.environ.get("APPLY_LOCK_WAIT_SECONDS", "10"))
# 拨钟尝试进入临界区时容忍的库写锁等待；超过即按冲突立即失败。
CLOCK_LOCK_ACQUIRE_TIMEOUT = float(os.environ.get("CLOCK_LOCK_ACQUIRE_TIMEOUT", "0.3"))
# 拨钟优先：拨钟最多等待应用察觉中止信号并回滚多久。
CLOCK_PREEMPT_WAIT_SECONDS = float(os.environ.get("CLOCK_PREEMPT_WAIT_SECONDS", "10"))
# 演示/测试用：应用临界区内人为停留的毫秒数，用于复现超时恢复。
APPLY_DEMO_HOLD_MS = int(os.environ.get("APPLY_DEMO_HOLD_MS", "0"))
APPLY_DEMO_LINE_MS = int(os.environ.get("APPLY_DEMO_LINE_MS", "0"))
