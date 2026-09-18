# 景区夜游资源调度

本项目服务于景区分时活动与安全调度。日夜场次、路线容量、设备状态、天气和商户结算共享同一套运行记录。
系统应支持清晰的领域对象、事件记录和责任追溯，运行入口提供稳定的健康检查，便于本地联调和运维巡检。

## 运行

```bash
python3 service.py --check        # 基础自检
python3 service.py --port 8000    # 启动服务
curl localhost:8000/health        # 健康检查
python3 -m unittest service_contract test_domain test_api   # 全部测试（或 npm test）
```

## 领域结构（nightops/）

| 模块 | 职责 |
| --- | --- |
| `models.py` | 区域、游线、活动、锁位、订单、退改政策、告警、风险、台账、运行记录 |
| `store.py` | 内存仓储：全部状态 + 追加式运行记录，写操作共用一把可重入锁 |
| `capacity.py` | 分时容量与锁位：并发锁位不突破区域上限与活动入口配额，锁位超时惰性释放 |
| `orders.py` | 确认锁位出票；购票时留存退改政策快照；按门票/演出/商户引流分别记账 |
| `events.py` | 活动多入口拆分、取消结算（退款/改期/补偿按快照计算）、迁往备用区域、散场、场次回放 |
| `safety.py` | 气象/设施告警、游线风险（等级/处置人/恢复条件）、设施状态联动、重新开放审批 |
| `field.py` | 现场离线操作合并：限流与游客转移，按 action_id 幂等，不重复执行 |
| `finance.py` | 财务核清：门票、演出、商户引流分账，退款与补偿单独列示 |

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/zones` · `/routes` · `/policies` · `/events` | 登记区域、游线、退改政策、活动（活动可拆到多个入口，可指定备用区域） |
| GET | `/zones/{id}/capacity?date=&slot=` | 某区域某时段的容量/占用 |
| POST | `/zones/{id}/status` | 设备检修/封闭（自动产生设施告警并联动游线风险） |
| POST | `/zones/{id}/reopen` | 重新开放：要求告警解除且风险闭环，记录审批人与理由 |
| POST | `/locks` | 渠道锁位（`key` 幂等，`ttl_seconds` 超时释放；超容量返回 409） |
| POST | `/orders` | 确认锁位出票（`order_id` 幂等，留存政策快照） |
| POST | `/events/{id}/cancel` | 取消：按购票时快照逐单退款/改期/补偿，`responsible=scenic` 触发补偿 |
| POST | `/events/{id}/relocate` | 取消后迁往备用区域，游客随活动转移，容量重新校验 |
| POST | `/events/{id}/finish` | 散场 |
| GET | `/events/{id}/replay` | 回放：容量变化、游客调整、重新开放理由的完整时间线 |
| POST | `/alerts` · `/alerts/{id}/clear` | 气象/设施告警与解除 |
| GET | `/safety/routes` | 安全负责人视图：每条路线的风险、处置人、恢复条件 |
| POST | `/routes/{id}/risk` · `/routes/{id}/risk/resolve` | 登记处置人/恢复条件、风险闭环 |
| POST | `/field-actions/batch` | 现场离线限流/转移批量合并，按 `action_id` 去重 |
| GET | `/finance/reconciliation?event_id=` | 财务核清：分账户与分商户的销售/退款/补偿/净额 |

## 关键规则

- **并发锁位**：校验与写入在同一把锁内完成，区域上限与活动入口配额双重约束，超额返回 409。
- **退改补偿**：订单在购票时固化政策快照，活动取消时只按快照计算；景区责任取消另计补偿。
- **离线合并**：现场操作携带 `action_id`，重复上报返回首次结果，不重复执行；失败的记录不占幂等键，可修正后重试。
- **重新开放**：区域重开必须满足"告警已解除 + 相关游线风险已闭环"，并记录审批人与理由，供场次回放还原。
- **可追溯**：所有容量变化、游客调整、状态变更都写入追加式运行记录，`/events/{id}/replay` 可还原任一场次的来龙去脉。
