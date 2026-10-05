"""领域枚举与业务异常。"""
from __future__ import annotations

import enum


class OrderStatus(str, enum.Enum):
    QUEUED = "QUEUED"            # 目的仓容量不足，排队中（保留批次占用）
    IN_TRANSIT = "IN_TRANSIT"    # 已发出，在途
    COMPLETED = "COMPLETED"      # 已到货
    CANCELLED = "CANCELLED"      # 已取消（占用已释放）


class ReconStatus(str, enum.Enum):
    PENDING = "PENDING"          # 跨仓对账待处理
    RESOLVED = "RESOLVED"


class ReconResolution(str, enum.Enum):
    WRITE_OFF = "WRITE_OFF"                # 确认损耗，核减源仓账面
    RELEASE_TO_SOURCE = "RELEASE_TO_SOURCE"  # 货物找回/未发出，解除挂账
    DELIVER_TO_DEST = "DELIVER_TO_DEST"    # 补记到货，转入目的仓


class LedgerType(str, enum.Enum):
    OPENING = "OPENING"                    # 新批次登记期初
    OPENING_BACKFILL = "OPENING_BACKFILL"  # 旧数据按现存数量回填期初
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    FREEZE = "FREEZE"
    UNFREEZE = "UNFREEZE"
    RECON_WRITE_OFF = "RECON_WRITE_OFF"
    RECON_RELEASE = "RECON_RELEASE"
    RECON_DELIVER = "RECON_DELIVER"


class EventType(str, enum.Enum):
    DEPARTED = "DEPARTED"          # 车辆出发
    TEMP_READING = "TEMP_READING"  # 车厢温度上报
    ARRIVED = "ARRIVED"            # 到货（含实收数量）


class ApiError(Exception):
    """可直接映射为 HTTP 响应的业务异常。"""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


class BatchOccupiedError(ApiError):
    """批次占用未释放：挡回后到的调拨单，并告知当前持有占用的单号。"""

    def __init__(self, current_order_no: str):
        super().__init__(
            409,
            "BATCH_OCCUPIED",
            f"批次占用未释放，当前持有单号: {current_order_no}",
            current_order_no=current_order_no,
        )
