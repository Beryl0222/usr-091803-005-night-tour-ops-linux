"""测试公用：搭建一个含主/备区域、双入口、两条游线、设施与商户的场次。"""

from nighttour import OperationsCenter, RulePolicy, TicketKind

T0 = 1_000_000.0  # 固定时钟基准，退款窗口断言不依赖真实时间

# 可变时钟：测试里推进"当前时间"以覆盖开场前/后退款窗口
NOW = [T0]


def set_now(value: float) -> None:
    NOW[0] = value


def build_center(policy: RulePolicy | None = None, event_log_path: str | None = None):
    center = OperationsCenter(event_log_path=event_log_path,
                              clock=lambda: NOW[0])
    populate(center, policy=policy)
    return center


def populate(center: OperationsCenter, policy: RulePolicy | None = None):
    engine = center.engine
    engine.add_area("a1", "主舞台区", 100, kind="stage", backup_area_id="a2")
    engine.add_area("a2", "备用舞台区", 60, kind="backup")
    engine.add_entrance("g1", "东门", "a1")
    engine.add_entrance("g2", "西门", "a1")
    engine.add_entrance("g3", "备用门", "a2")
    engine.add_route("r1", "主游线", 80, ["a1"])
    engine.add_route("r2", "备用游线", 80, ["a2"])
    engine.add_facility("f1", "舞台灯光", "a1")
    engine.add_merchant("m1", "非遗糖画", 0.3)
    engine.create_session(
        "s1", "夜场实景演出", "a1", TicketKind.SHOW,
        T0 + 3600, T0 + 5400,
        entrance_ids=["g1", "g2"], route_ids=["r1"],
        policy=policy,
        reopen_conditions=["气象解除", "设备复检", "清场确认"],
    )
    engine.create_session(
        "s2", "次日演出", "a1", TicketKind.SHOW,
        T0 + 90000, T0 + 91800,
        entrance_ids=["g1", "g2"], route_ids=["r1"],
        policy=policy,
    )
    engine.set_entrance_quota("g1", "s1", 60)
    engine.set_entrance_quota("g2", "s1", 40)
    engine.set_entrance_quota("g1", "s2", 60)
    engine.set_entrance_quota("g2", "s2", 40)
    return center


def buy(engine, session_id, entrance, quantity, visitor, route=None,
        price=10000, kind=TicketKind.SHOW, merchant_id=None):
    hold = engine.create_hold(session_id, entrance, quantity, "ctrip",
                              route_id=route)
    item = {"kind": kind.value, "quantity": quantity, "unit_price": price}
    if merchant_id:
        item["merchant_id"] = merchant_id
    return engine.confirm_order(hold.id, visitor, [item])
