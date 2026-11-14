"""实验、学员与分配的核心业务逻辑。

关键约束：
- 分配先于方案展示；曝光失败仍保留分配事实。
- 首次申请时按当时资料判定资格并快照；重复请求（同学员或同 request_id）返回首次事实。
- 换设备不换组：按稳定 student_key 哈希分流，设备仅做绑定解析。
- 实验停招只停新分配，既有分配与观察继续。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock
from .eligibility import evaluate, failed_conditions, validate_rule
from .models import VARIANTS, Assignment, Experiment, RatioConfig, Student, StudentDevice

DEFAULT_ENROLLMENT_DEFINITION = {
    "created_events": ["order_created"],
    "revoke_events": ["order_canceled", "order_refunded"],
}


class DomainError(Exception):
    """业务错误，message 面向调用方，status 为 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- 实验

def validate_ratios(ratios: dict) -> dict:
    if not isinstance(ratios, dict) or set(ratios.keys()) != set(VARIANTS):
        raise DomainError(f"ratios 必须且只能包含键：{', '.join(VARIANTS)}")
    out = {}
    for k in VARIANTS:
        v = ratios[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or v > 1:
            raise DomainError(f"ratios.{k} 必须是 [0,1] 内的数值")
        out[k] = float(v)
    if abs(sum(out.values()) - 1.0) > 1e-6:
        raise DomainError("ratios 各组比例之和必须等于 1")
    return out


def create_experiment(
    session: Session,
    *,
    name: str,
    seed: str,
    observation_days: int,
    ratios: dict,
    audience_rule: dict | None = None,
    enrollment_definition: dict | None = None,
    chi2_threshold: float = 0.001,
) -> Experiment:
    if not name:
        raise DomainError("name 不能为空")
    if not seed:
        raise DomainError("seed 不能为空（固定种子，创建后不可改）")
    if not isinstance(observation_days, int) or observation_days < 1:
        raise DomainError("observation_days 必须为 >=1 的整数")
    if not (0 < chi2_threshold < 1):
        raise DomainError("chi2_threshold 必须在 (0,1) 内")
    rule = audience_rule or {"conditions": []}
    try:
        validate_rule(rule)
    except ValueError as e:
        raise DomainError(str(e)) from e
    definition = enrollment_definition or DEFAULT_ENROLLMENT_DEFINITION
    for key in ("created_events", "revoke_events"):
        if key not in definition or not isinstance(definition[key], list) or not definition[key]:
            raise DomainError(f"enrollment_definition.{key} 必须是非空事件类型数组")
    now = clock.now(session)
    exp = Experiment(
        name=name,
        seed=str(seed),
        status="enrolling",
        audience_rule=rule,
        enrollment_definition=definition,
        observation_days=observation_days,
        chi2_threshold=float(chi2_threshold),
        created_at=now,
    )
    session.add(exp)
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise DomainError(f"实验名称已存在: {name}", 409) from e
    rc = RatioConfig(experiment_id=exp.id, effective_from=now, ratios=validate_ratios(ratios))
    session.add(rc)
    session.commit()
    session.refresh(exp)
    return exp


def get_experiment(session: Session, experiment_id: int) -> Experiment:
    exp = session.get(Experiment, experiment_id)
    if exp is None:
        raise DomainError(f"实验不存在: {experiment_id}", 404)
    return exp


def list_ratio_configs(session: Session, experiment_id: int) -> list[RatioConfig]:
    return list(
        session.scalars(
            select(RatioConfig)
            .where(RatioConfig.experiment_id == experiment_id)
            .order_by(RatioConfig.effective_from, RatioConfig.id)
        )
    )


def active_ratio_config(session: Session, experiment_id: int, at: datetime) -> RatioConfig:
    """取生效时间 <= at 的最新配置。"""
    rc = session.scalars(
        select(RatioConfig)
        .where(RatioConfig.experiment_id == experiment_id, RatioConfig.effective_from <= at)
        .order_by(RatioConfig.effective_from.desc(), RatioConfig.id.desc())
        .limit(1)
    ).first()
    if rc is None:
        raise DomainError("实验缺少生效中的分流比例配置", 500)
    return rc


def change_ratio(session: Session, experiment_id: int, ratios: dict) -> RatioConfig:
    """调整比例：插入新配置，仅作用于生效之后的分配；历史分配仍归属原配置期间。"""
    exp = get_experiment(session, experiment_id)
    if exp.status != "enrolling":
        raise DomainError("实验已停招，不能再调整比例", 409)
    now = clock.now(session)
    rc = RatioConfig(experiment_id=exp.id, effective_from=now, ratios=validate_ratios(ratios))
    session.add(rc)
    session.commit()
    session.refresh(rc)
    return rc


def stop_experiment(session: Session, experiment_id: int) -> Experiment:
    exp = get_experiment(session, experiment_id)
    if exp.status == "stopped":
        return exp
    exp.status = "stopped"
    exp.stopped_at = clock.now(session)
    session.commit()
    session.refresh(exp)
    return exp


# ---------------------------------------------------------------- 学员

def register_student(
    session: Session,
    *,
    student_key: str | None = None,
    device_id: str | None = None,
    profile: dict | None = None,
) -> tuple[Student, bool]:
    """注册或解析学员；返回 (student, created)。device_id 已绑定时解析到原学员。"""
    now = clock.now(session)
    student: Student | None = None
    if student_key:
        student = session.scalars(select(Student).where(Student.student_key == student_key)).first()
    if student is None and device_id:
        bound = session.get(StudentDevice, device_id)
        if bound is not None:
            student = session.scalars(
                select(Student).where(Student.student_key == bound.student_key)
            ).first()
    created = False
    if student is None:
        student = Student(
            student_key=student_key or f"stu_{uuid.uuid4().hex[:12]}",
            profile=profile or {},
            created_at=now,
        )
        session.add(student)
        session.flush()
        created = True
    elif profile:
        student.profile = {**student.profile, **profile}
        session.flush()
    if device_id and session.get(StudentDevice, device_id) is None:
        session.add(StudentDevice(device_id=device_id, student_key=student.student_key, first_seen_at=now))
        session.flush()
    return student, created


# ---------------------------------------------------------------- 分流

def variant_for(seed: str, student_key: str, ratios: dict) -> str:
    """按 (seed, student_key) 稳定哈希到 [0,1) 区间，再按当前比例映射到组。"""
    digest = hashlib.sha256(f"{seed}:{student_key}".encode("utf-8")).hexdigest()
    x = int(digest[:16], 16) / 2**64
    cumulative = 0.0
    for variant in sorted(ratios.keys()):  # control < treatment，顺序固定
        cumulative += ratios[variant]
        if x < cumulative:
            return variant
    return sorted(ratios.keys())[-1]


def _assignment_payload(a: Assignment, *, outcome: str, idempotent_replay: bool = False) -> dict:
    return {
        "outcome": outcome,
        "idempotent_replay": idempotent_replay,
        "experiment_id": a.experiment_id,
        "student_key": a.student_key,
        "request_id": a.request_id,
        "status": a.status,
        "variant": a.variant,
        "reason": a.reason,
        "reason_detail": a.reason_detail,
        "assigned_at": a.assigned_at.isoformat(),
        "ratio_config_id": a.ratio_config_id,
        "exposure_status": a.exposure_status,
        "eligibility": a.eligibility,
    }


def request_assignment(
    session: Session,
    experiment_id: int,
    *,
    request_id: str,
    student_key: str | None = None,
    device_id: str | None = None,
    profile: dict | None = None,
) -> dict:
    """申请分配。返回 outcome ∈ assigned | duplicate | ineligible | stopped。"""
    if not request_id:
        raise DomainError("request_id 不能为空（幂等键）")
    exp = get_experiment(session, experiment_id)

    # 1) 幂等键重放：同一 request_id 直接返回首次结果
    existing = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id, Assignment.request_id == request_id
        )
    ).first()
    if existing is not None:
        if student_key and existing.student_key != student_key:
            raise DomainError(
                f"request_id {request_id} 已用于学员 {existing.student_key}，不能复用", 409
            )
        return _assignment_payload(existing, outcome="duplicate", idempotent_replay=True)

    # 2) 解析稳定学员标识（显式 key 优先；否则按设备绑定；都没有则新建）
    student, _ = register_student(
        session, student_key=student_key, device_id=device_id, profile=profile
    )

    # 3) 同学员重复请求（含跨设备）：返回首次事实
    existing = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id, Assignment.student_key == student.student_key
        )
    ).first()
    if existing is not None:
        return _assignment_payload(existing, outcome="duplicate")

    now = clock.now(session)

    # 4) 依次决定首次事实：停招 > 资格不足 > 正常分配（不为凑组数临时接纳不合格者）
    if exp.status != "enrolling":
        row = Assignment(
            experiment_id=exp.id,
            student_key=student.student_key,
            request_id=request_id,
            device_id=device_id,
            status="stopped",
            reason="experiment_stopped",
            reason_detail={"message": "实验已停招，不再接纳新分配；既有观察继续"},
            assigned_at=now,
        )
        outcome = "stopped"
    else:
        # 资格：首次申请时按当时资料判定并快照
        evaluation = evaluate(exp.audience_rule, student.profile)
        if not evaluation["eligible"]:
            row = Assignment(
                experiment_id=exp.id,
                student_key=student.student_key,
                request_id=request_id,
                device_id=device_id,
                status="ineligible",
                reason="audience_rule_not_matched",
                reason_detail={"failed_conditions": failed_conditions(evaluation)},
                eligibility=evaluation,
                assigned_at=now,
            )
            outcome = "ineligible"
        else:
            rc = active_ratio_config(session, exp.id, now)
            row = Assignment(
                experiment_id=exp.id,
                student_key=student.student_key,
                request_id=request_id,
                device_id=device_id,
                status="assigned",
                variant=variant_for(exp.seed, student.student_key, rc.ratios),
                eligibility=evaluation,
                ratio_config_id=rc.id,
                assigned_at=now,
            )
            outcome = "assigned"

    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # 并发下另一请求已写入同学员/同键记录：回滚后返回首次事实
        session.rollback()
        existing = session.scalars(
            select(Assignment).where(
                Assignment.experiment_id == exp.id,
                Assignment.student_key == student.student_key,
            )
        ).first()
        if existing is not None:
            return _assignment_payload(existing, outcome="duplicate")
        raise
    return _assignment_payload(row, outcome=outcome)


def record_exposure(session: Session, experiment_id: int, *, student_key: str, status: str) -> dict:
    """记录曝光结果。分配先于展示：曝光失败/未曝光都保留分配事实，仍计入分析。"""
    if status not in ("success", "failed"):
        raise DomainError("status 必须是 success 或 failed")
    exp = get_experiment(session, experiment_id)
    a = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id,
            Assignment.student_key == student_key,
            Assignment.status == "assigned",
        )
    ).first()
    if a is None:
        raise DomainError("该学员在此实验中没有已分配记录", 404)
    a.exposure_status = status
    a.exposure_at = clock.now(session)
    session.commit()
    return _assignment_payload(a, outcome="assigned")
