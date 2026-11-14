"""数据库模型。

设计要点：
- 分配（assignments）保存"首次事实"：同一实验同一学员仅一行，重复请求返回原结果。
- 事件（events）为不可变事实流，按 (source, biz_id, seq) 去重；全局自增 id 即"事件可用序号"，
  分析运行时对其做快照，晚报事件只属于更新的分析。
- 分析运行（analysis_runs）冻结截止点、事件序号快照、上报水位快照与比例配置快照，
  结果落库后不可变，可断点续算同一批次。
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

VARIANTS = ("control", "treatment")


class Clock(Base):
    """可推进虚拟时钟（单行，id=1）。系统内所有"现在"均取自此处。"""

    __tablename__ = "clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    now: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    seed: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="enrolling")  # enrolling | stopped
    audience_rule: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enrollment_definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    observation_days: Mapped[int] = mapped_column(Integer, nullable=False)
    chi2_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.001)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)


class RatioConfig(Base):
    """分流比例配置；实验期间调整只产生新行，仅作用于生效之后的分配。"""

    __tablename__ = "ratio_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    effective_from: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    ratios: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {"control": w, "treatment": w}


class Student(Base):
    """稳定学员标识。student_key 由调用方提供或由系统生成，跨设备不变。"""

    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class StudentDevice(Base):
    """设备到学员的绑定；同一学员换设备仍解析到同一 student_key。"""

    __tablename__ = "student_devices"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    first_seen_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Assignment(Base):
    """分配事实（含资格不足/停招等未分配决定的首次记录）。"""

    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("experiment_id", "student_key", name="uq_assignment_exp_student"),
        UniqueConstraint("experiment_id", "request_id", name="uq_assignment_exp_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # assigned | ineligible | stopped
    variant: Mapped[str | None] = mapped_column(String(16), nullable=True)  # control | treatment
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    eligibility: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 首次申请时的资料与逐条判定
    ratio_config_id: Mapped[int | None] = mapped_column(ForeignKey("ratio_configs.id"), nullable=True)
    exposure_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")  # unknown | success | failed
    exposure_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Source(Base):
    """行为来源及其"已完整上报"的时间界限（水位线）。"""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    complete_until: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Event(Base):
    """不可变行为事件。id 为全局摄取序号（分析快照即 max(id)）。"""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "biz_id", "seq", name="uq_event_source_biz_seq"),
        Index("ix_events_student", "student_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    biz_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ingested_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalysisRun(Base):
    """一次分析批次：冻结截止点/事件序号/水位线/比例快照。中断后可按原参数续算。"""

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    cutoff: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    event_seq_snapshot: Mapped[int] = mapped_column(BigInteger, nullable=False)
    watermark_snapshot: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    ratio_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running | done
    # 汇总统计（完成时写入）
    diff: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ok | insufficient_counts | chi2_paused
    evidence_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    chi2_stat: Mapped[float | None] = mapped_column(Float, nullable=True)
    chi2_df: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chi2_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    chi2_paused: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chi2_details: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reconciled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalysisResult(Base):
    """每个运行 × 变体的分段计数。"""

    __tablename__ = "analysis_results"

    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    variant: Mapped[str] = mapped_column(String(16), primary_key=True)
    total_assigned: Mapped[int] = mapped_column(Integer, nullable=False)
    obs_insufficient: Mapped[int] = mapped_column(Integer, nullable=False)
    report_incomplete: Mapped[int] = mapped_column(Integer, nullable=False)
    mature: Mapped[int] = mapped_column(Integer, nullable=False)
    mature_success: Mapped[int] = mapped_column(Integer, nullable=False)
    mature_failure: Mapped[int] = mapped_column(Integer, nullable=False)


class AnalysisStudent(Base):
    """每个运行中学员所属分段与报名依据（用于对账与回查）。"""

    __tablename__ = "analysis_students"

    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    variant: Mapped[str] = mapped_column(String(16), nullable=False)
    section: Mapped[str] = mapped_column(String(24), nullable=False)  # obs_insufficient | report_incomplete | mature_success | mature_failure
    window_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    exposure_status: Mapped[str] = mapped_column(String(16), nullable=False)
    qualifying_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qualifying_biz_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    basis_event_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
