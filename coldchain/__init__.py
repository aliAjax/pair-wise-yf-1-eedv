"""冷库药品调拨核对服务。

解决同一批次在途量与可放量对不上的问题，核心能力：
- 批次占用互斥：并发调拨后到者先确认占用，未释放则挡回并告知当前单号
- 运输断网恢复后合并上报：同一记录重复上报只入库一次
- 到货短缺挂跨仓对账待处理，批次余量不跟着改
- 目的仓容量满则排队，容量腾出后按序放行
- 温控变化重算可放量，温度超限过的库存自动冻结
- 旧数据缺调拨记录的批次按现存数量回填期初
"""

from .models import ApiError, BatchOccupiedError
from .service import ColdChainService
from .store import Store

__all__ = ["ApiError", "BatchOccupiedError", "ColdChainService", "Store"]
