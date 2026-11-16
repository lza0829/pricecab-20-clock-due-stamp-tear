# 06-pricecab（价签柜）

Pricecab — 改价单按预约时刻生效，现行价签可查，并与销售夹具对账。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）、Proofpack（采样放行）主链路不同。  
不是电商订单/购物车，不是仓库进出库 CRUD，不是考勤 OA。夹具全本地。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5600 |
| API | http://localhost:10600 |

## 主链（0-1）

选商品建改价单（写明生效时刻）→ 拨业务钟或点「应用到期」→ 现行价签更新 → 销售对账看一致/不一致

## 生效临界区：拨钟 × 应用到期

拨钟与应用到期共享**同一个生效临界区**：同一时刻只允许一方持锁推进。

持锁方必须把

1. 读到期集合
2. 写价签 / 价格历史
3. 更新改价单状态
4. 落应用批次

放在**同一个事务**里，同成同败。锁记录带持有者、获取时刻、到期时刻与单调递增的栅栏号（fence）；持锁超时未完成时，下一次获取会回收该锁并把栅栏 +1，原持有者在提交前复核必然失败，只能整批回滚，因此不会残留「半截 applied」，也不会改写他人已成功的批次。

### 冲突让路策略（全局只有一种）

| 策略 | 行为 |
| --- | --- |
| `apply_first`「应用优先」（默认） | 应用持锁期间，一切拨钟立即失败；拨钟不改钟，也不迫使应用回滚 |
| `clock_first`「拨钟优先」 | 应用持锁期间收到拨钟，应用中止并整批回滚已写价签/历史/单状态，然后拨钟生效；应用回执记冲突中止，不留半截 applied |

两种策略不会并存：运行期生效值存于 `settings.apply_conflict_policy`，界面上切换即替换，任一时刻只有一种生效。初始默认值可用环境变量 `APPLY_CONFLICT_POLICY` 指定。

### 相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/apply/clock` | 拨钟；冲突时 409 |
| POST | `/api/apply/due` | 应用到期；冲突/中止时 409，本批已整批回滚 |
| GET | `/api/apply/lock` | 当前锁持有者、获取/到期时刻、栅栏 |
| POST | `/api/apply/lock/recover` | 回收超时未释放的锁 |
| GET/PUT | `/api/apply/policy` | 查看 / 切换全局让路策略 |
| GET | `/api/apply/conflicts` | 冲突回执 |
| GET | `/api/apply/aborts` | 应用中止（已整批回滚）回执 |

`POST /api/apply/due?hold_ms=…&line_ms=…` 可人为拉长临界区，用于演示和测试超时恢复与拨钟抢占。

## 测试

```bash
cd backend
pip install fastapi==0.115.6 'uvicorn[standard]==0.32.1' pydantic==2.10.3 pytest httpx
python -m pytest tests -q
```

覆盖：应用优先下拨钟被拒且价签按旧钟应用成功、拨钟优先下应用中止回滚且新钟落库、双开应用互斥、超时恢复无半截写。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。
