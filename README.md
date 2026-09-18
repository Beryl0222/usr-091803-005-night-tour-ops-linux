# 景区夜游资源调度

日夜观光、实景演出与非遗市集共享同一套运行记录。本服务是运营中心后端：
统一判断分时容量、多入口分流、路线占用、演出批次、设施状态与安全告警，
并保证锁位不超额、退款按购票时规则、现场离线处置可合并且不重复执行、
收入三类分别核清，以及任一场次结束后可完整还原"容量怎么变、游客怎么调、
为什么允许重新开放"。

## 运行

```bash
python3 service.py --check          # 基础自检
python3 service.py --port 8000      # 启动；GET /health 返回服务身份
python3 service.py --event-log ./run/events.jsonl  # 事件落盘，重启可重建
```

健康检查契约保持不变：

```json
{"status": "ok", "service": "night-tour-ops", "name": "景区夜游资源调度"}
```

## 领域模型（`nighttour/`）

| 模块 | 职责 |
| --- | --- |
| `models.py` | 区域、入口、游线、设施、商户、场次、锁位、订单、规则快照、告警、台账 |
| `events.py` | 追加式 JSONL 事件日志（内存模式可选），审计的唯一权威来源 |
| `store.py` | 单锁保护的状态与四维度容量计数：入口配额 / 场次 / 区域 / 游线 |
| `engine.py` | 锁位确认、规则快照化的退款/改期/补偿、暂停恢复、取消、备用区迁移、结束 |
| `safety.py` | 安全看板、风险级别/处置人/恢复条件、限流/转移/疏散、幂等与合并 |
| `finance.py` | 门票 / 演出 / 商户引流三类收入核清，商户按锁定分成率结算 |
| `audit.py` | 场次时间线：重放容量、归并游客调整、给出重新开放依据 |
| `replay.py` | 从事件日志重建全部运行态（重启后状态与幂等索引一致） |
| `api.py` | 标准库实现的 JSON HTTP 接口 |

## 关键规则

- **并发锁位不破上限**：任何"校验容量→计数→写事件"都在同一把锁内完成，
  同时占用入口分时配额、场次总量、区域上限、游线在途四个维度。
- **一场活动拆多入口**：按入口设置分时配额；任一维度先到上限即拒绝锁位。
- **退款/改期/补偿按购票时规则**：订单确认时冻结 `policy_snapshot`，
  事后调整规则或商户分成率都不溯及既往。
- **取消与迁移**：取消即释放未支付锁位并按快照批量退款；迁往备用区域时
  容量按整张订单原子搬运，装不下的订单按园区原因退款，迁移成功的订单
  按快照发放补偿权益。
- **离线现场处置**：`Idempotency-Key` 相同的上报回放首次结果、绝不重复
  执行；`merge_key` 相同的限流/转移/疏散合并为一条处置记录并累加人数，
  记名游客按订单搬运、散客按人头核减（不会减成负数）。
- **安全恢复门禁**：恢复条件逐条核验，任一缺失或相关设施仍故障/安全
  关闭，都不得解除限流或重新开放；核验证据与原因写入事件。
- **财务三分离**：每条流水带收入类别，商户引流按订单行锁定的 `lead_rate`
  计算应结金额，退款同步冲减。
- **场次审计**：每个容量变动事件都带标准化 `capacity_effects`，时间线
  据此重放逐时点读数，并汇总被调整的订单与重开决策。

## 主要 HTTP 接口

```
POST /admin/areas|entrances|routes|facilities|merchants|sessions   资源建档
POST /admin/entrances/{id}/quotas                                  多入口配额
POST /admin/facilities/{id}/state                                  设施检修/故障/恢复
POST /holds                                                         渠道锁位
POST /holds/{id}/release | /holds/{id}/confirm                     释放 / 支付确认
GET  /orders/{id}/refund/evaluate                                  只算不退
POST /orders/{id}/refund|reschedule|compensation|checkout          退改/补偿/离场
POST /sessions/{id}/suspend|resume|cancel|migrate|end               场次调度
GET  /sessions/{id}/capacity                                       实时容量
POST /alerts ; /alerts/{id}/assign|recover|close                   安全告警处置
GET  /safety/board                                                 风险/处置人/恢复条件
POST /field/actions                                                离线限流/转移/疏散
GET  /finance/summary|merchants|entries                            财务核清与分账
GET  /sessions/{id}/timeline                                       场次审计还原
```

所有写接口接受 `Idempotency-Key` 请求头（或请求体 `idempotency_key`）。
错误码：容量/状态冲突 `409`，规则不满足 `422`，对象不存在 `404`。

## 测试

```bash
npm test
# 等价于：
python3 -m unittest discover -s . -p "test_*.py"
python3 -m unittest service_contract
```

覆盖：多入口与四维度容量、高并发锁位/支付不超额、锁位超时、规则快照
不变性、取消/迁移退款与补偿、改期容量回滚、离线幂等与合并、安全恢复
门禁、三类收入与商户分账、事件日志重启重建、审计时间线，以及 HTTP
端到端与 `/health` 契约。
