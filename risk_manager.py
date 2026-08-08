"""
风险管理模块
跟踪每个市场的敞口并实施最大敞口限制
"""
import threading
from typing import Dict, Optional
from config import config
from logger import setup_logger

logger = setup_logger("risk_manager")


class RiskManager:
    """风险管理器"""
    
    def __init__(self, max_exposure_per_market_usdc: Optional[float] = None):
        """
        初始化风险管理器
        
        Args:
            max_exposure_per_market_usdc: 每市场最大敞口（USDC），如果为None则从配置读取
        """
        if max_exposure_per_market_usdc is None:
            max_exposure_per_market_usdc = config.max_exposure_per_market_usdc
        
        self.max_exposure_per_market_usdc = max_exposure_per_market_usdc
        self.market_exposures: Dict[str, float] = {}  # market_id -> exposure_usdc（包括挂单和已成交订单）
        self.filled_orders_exposure: Dict[str, float] = {}  # market_id -> filled_exposure_usdc（已成交订单的敞口）
        self.lock = threading.Lock()  # 线程锁，确保线程安全
        
        logger.info(f"风险管理器初始化，最大敞口限制: {max_exposure_per_market_usdc} USDC/市场")
    
    def calculate_exposure(self, order_price: float, order_size: float, side: str) -> float:
        """
        计算单个订单的敞口（USDC）
        
        注意：只有买单计算敞口，卖单不计算敞口（卖出已持有的token，不需要投入新资金）
        
        Args:
            order_price: 订单价格（0-1之间）
            order_size: 订单份额
            side: 订单方向，"BUY" 或 "SELL"
            
        Returns:
            订单敞口（USDC），买单返回实际敞口，卖单返回 0
        """
        if side.upper() == "BUY":
            # 买单敞口 = 订单价格 × 订单份额
            exposure = order_price * order_size
        elif side.upper() == "SELL":
            # 卖单不计算敞口（卖出已持有的token，不需要投入新资金）
            exposure = 0.0
        else:
            logger.warning(f"未知的订单方向: {side}，返回 0 敞口")
            return 0.0
        
        return exposure
    
    def get_market_exposure(self, market_id: str) -> float:
        """
        获取指定市场的当前敞口
        
        Args:
            market_id: 市场ID
            
        Returns:
            市场当前敞口（USDC），如果市场不存在则返回 0
        """
        with self.lock:
            return self.market_exposures.get(market_id, 0.0)
    
    def get_all_exposures(self) -> Dict[str, float]:
        """
        获取所有市场的敞口
        
        Returns:
            字典 {market_id: exposure_usdc}
        """
        with self.lock:
            return self.market_exposures.copy()
    
    def can_place_order(
        self, 
        market_id: str, 
        order_price: float, 
        order_size: float, 
        side: str
    ) -> bool:
        """
        检查是否可以下单（未超过最大敞口限制）
        
        注意：卖单不计算敞口，直接允许下单；只有买单受最大敞口限制约束
        
        Args:
            market_id: 市场ID
            order_price: 订单价格（0-1之间）
            order_size: 订单份额
            side: 订单方向，"BUY" 或 "SELL"
            
        Returns:
            如果可以下单返回 True，否则返回 False
        """
        # 卖单不计算敞口，直接允许下单
        if side.upper() == "SELL":
            return True
        
        # 买单需要检查敞口限制
        # 计算新订单的敞口
        new_exposure = self.calculate_exposure(order_price, order_size, side)
        
        # 获取当前市场敞口
        current_exposure = self.get_market_exposure(market_id)
        
        # 计算下单后的总敞口
        total_exposure = current_exposure + new_exposure
        
        # 检查是否超过限制
        if total_exposure > self.max_exposure_per_market_usdc:
            logger.warning(
                f"市场 {market_id} 下单将超过最大敞口限制: "
                f"当前 {current_exposure:.2f} + 新增 {new_exposure:.2f} = {total_exposure:.2f} > "
                f"限制 {self.max_exposure_per_market_usdc:.2f} USDC"
            )
            return False
        
        return True
    
    def add_exposure(
        self,
        market_id: str,
        exposure_usdc: float,
        *,
        allow_over_limit: bool = False,
    ) -> bool:
        """
        添加市场敞口（下单时调用）
        
        Args:
            market_id: 市场ID
            exposure_usdc: 要添加的敞口（USDC）
            allow_over_limit: 仅当外部订单已提交时，保守记录超过限额的敞口
            
        Returns:
            如果添加成功返回 True，如果超过限制返回 False
        """
        with self.lock:
            current_exposure = self.market_exposures.get(market_id, 0.0)
            new_exposure = current_exposure + exposure_usdc
            
            # 检查是否超过限制
            if (
                new_exposure > self.max_exposure_per_market_usdc
                and not allow_over_limit
            ):
                logger.warning(
                    f"市场 {market_id} 添加敞口将超过限制: "
                    f"{current_exposure:.2f} + {exposure_usdc:.2f} = {new_exposure:.2f} > "
                    f"{self.max_exposure_per_market_usdc:.2f} USDC"
                )
                return False

            if new_exposure > self.max_exposure_per_market_usdc:
                logger.warning(
                    f"市场 {market_id} 已存在外部订单，保守记录超限敞口: "
                    f"{current_exposure:.2f} + {exposure_usdc:.2f} = "
                    f"{new_exposure:.2f} USDC"
                )
            
            # 更新敞口
            self.market_exposures[market_id] = new_exposure
            logger.debug(
                f"市场 {market_id} 敞口更新: {current_exposure:.2f} + {exposure_usdc:.2f} = "
                f"{new_exposure:.2f} USDC"
            )
            return True
    
    def remove_exposure(self, market_id: str, exposure_usdc: float):
        """
        移除市场敞口（订单取消时调用）
        
        注意：订单成交时不应该调用此方法，因为已成交的订单仍然占用资金，应该计入敞口
        
        Args:
            market_id: 市场ID
            exposure_usdc: 要移除的敞口（USDC）
        """
        with self.lock:
            current_exposure = self.market_exposures.get(market_id, 0.0)
            new_exposure = max(0.0, current_exposure - exposure_usdc)  # 确保不小于0
            
            if new_exposure == 0.0 and market_id in self.market_exposures:
                # 如果敞口为0，从字典中移除
                del self.market_exposures[market_id]
            else:
                self.market_exposures[market_id] = new_exposure
            
            logger.debug(
                f"市场 {market_id} 敞口更新（订单取消）: {current_exposure:.2f} - {exposure_usdc:.2f} = "
                f"{new_exposure:.2f} USDC"
            )
    
    def add_filled_order_exposure(self, market_id: str, order_price: float, filled_size: float):
        """
        添加已成交订单的敞口（订单成交时调用）
        
        已成交的订单占用资金，应该计入敞口，防止不断吃单又不断挂单增加成本
        
        Args:
            market_id: 市场ID
            order_price: 成交价格（0-1之间）
            filled_size: 成交份额
        """
        # 只计算买单的敞口（卖单不占用新资金）
        exposure = order_price * filled_size
        
        with self.lock:
            # 更新总敞口（包括挂单和已成交订单）
            current_exposure = self.market_exposures.get(market_id, 0.0)
            new_exposure = current_exposure + exposure
            self.market_exposures[market_id] = new_exposure
            
            # 更新已成交订单的敞口（用于统计）
            current_filled_exposure = self.filled_orders_exposure.get(market_id, 0.0)
            new_filled_exposure = current_filled_exposure + exposure
            self.filled_orders_exposure[market_id] = new_filled_exposure
            
            logger.debug(
                f"市场 {market_id} 已成交订单敞口: {current_filled_exposure:.2f} + {exposure:.2f} = "
                f"{new_filled_exposure:.2f} USDC, 总敞口: {new_exposure:.2f} USDC"
            )
    
    def remove_filled_order_exposure(self, market_id: str, order_price: float, filled_size: float):
        """
        移除已成交订单的敞口（对冲卖出时调用）
        
        当已成交的买单被对冲卖出后，可以移除这部分敞口
        
        Args:
            market_id: 市场ID
            order_price: 成交价格（0-1之间）
            filled_size: 成交份额
        """
        exposure = order_price * filled_size
        
        with self.lock:
            # 更新总敞口
            current_exposure = self.market_exposures.get(market_id, 0.0)
            new_exposure = max(0.0, current_exposure - exposure)
            self.market_exposures[market_id] = new_exposure
            
            # 更新已成交订单的敞口
            current_filled_exposure = self.filled_orders_exposure.get(market_id, 0.0)
            new_filled_exposure = max(0.0, current_filled_exposure - exposure)
            if new_filled_exposure == 0.0 and market_id in self.filled_orders_exposure:
                del self.filled_orders_exposure[market_id]
            else:
                self.filled_orders_exposure[market_id] = new_filled_exposure
            
            logger.debug(
                f"市场 {market_id} 已成交订单敞口（对冲卖出）: {current_filled_exposure:.2f} - {exposure:.2f} = "
                f"{new_filled_exposure:.2f} USDC, 总敞口: {new_exposure:.2f} USDC"
            )
    
    def reset_market_exposure(self, market_id: str):
        """
        重置指定市场的敞口（清零）
        
        Args:
            market_id: 市场ID
        """
        with self.lock:
            if market_id in self.market_exposures:
                old_exposure = self.market_exposures[market_id]
                del self.market_exposures[market_id]
                logger.info(f"市场 {market_id} 敞口已重置: {old_exposure:.2f} -> 0.0 USDC")
            else:
                logger.debug(f"市场 {market_id} 没有敞口记录，无需重置")
