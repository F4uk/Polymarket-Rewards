"""
订单管理模块
使用 py-clob-client 实现订单生命周期管理、自动补单、对冲卖出
"""
import os
import sys
import time
import threading
import requests
from typing import Dict, List, Optional, Any, Tuple, Set
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs,
    OrderType,
    OpenOrderParams,
    TradeParams,
    PartialCreateOrderOptions,
    OrderPayload,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

from config import config
from logger import setup_logger
from risk_manager import RiskManager
from market_making_strategy import MarketMakingStrategy, normalize_orderbook
from api_client import PolymarketAPIClient

# WebSocket 相关导入已移除，现在使用 HTTP 接口获取订单簿

logger = setup_logger("order_manager")


class OrderManager:
    """订单管理器"""
    
    def __init__(
        self,
        api_client: PolymarketAPIClient,
        risk_manager: RiskManager,
        strategy: MarketMakingStrategy,
        private_key: Optional[str] = None,
        funder_address: Optional[str] = None,
        signature_type: Optional[int] = None,
        chain_id: Optional[int] = None
    ):
        """
        初始化订单管理器
        
        Args:
            api_client: Polymarket API 客户端
            risk_manager: 风险管理器
            strategy: 做市策略
            private_key: 私钥（如果为None，从环境变量 POLYMARKET_PRIVATE_KEY 读取）
            funder_address: 存款/代理钱包地址（如果为None，从环境变量 POLYMARKET_PROXY_ADDRESS 读取）
            signature_type: 签名类型（如果为None，从配置读取，0=EOA, 1=email/Magic）
            chain_id: 链ID（如果为None，从配置读取，默认137=Polygon）
        """
        self.api_client = api_client
        self.risk_manager = risk_manager
        self.strategy = strategy
        
        # 从环境变量或配置读取私钥和 funder 地址
        if private_key is None:
            private_key = config.get_private_key() or os.getenv("POLYMARKET_PRIVATE_KEY")
        if funder_address is None:
            funder_address = config.get_funder_address() or os.getenv("POLYMARKET_PROXY_ADDRESS")
        
        # 从配置读取签名类型和链ID
        if signature_type is None:
            signature_type = config.signature_type
        if chain_id is None:
            chain_id = config.chain_id
        
        if not private_key:
            raise ValueError("未提供私钥，请设置环境变量 POLYMARKET_PRIVATE_KEY 或在初始化时传入")
        if not funder_address:
            raise ValueError("未提供存款/代理钱包地址，请设置环境变量 POLYMARKET_PROXY_ADDRESS 或在初始化时传入")
        
        # 初始化 ClobClient
        self.clob_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=chain_id,
            key=private_key,
            signature_type=signature_type,
            funder=funder_address
        )
        
        # 设置 API 凭证
        try:
            self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_key())
            logger.info("ClobClient 初始化成功")
        except Exception as e:
            logger.error(f"ClobClient 初始化失败: {e}")
            raise
        
        # 订单跟踪：market_id -> {token_id: {side: order_info}}
        self.active_orders: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
        # 已成交订单记录：用于对冲卖出
        self.filled_buy_orders: Dict[str, List[Dict[str, Any]]] = {}  # market_id -> [filled_order_info]
        # 市场数据缓存：用于价格调整
        self.market_data_cache: Dict[str, Dict[str, Any]] = {}  # market_id -> market_data
        # 待重新挂单的 token 列表：记录因风险管理（买一价检查）而跳过的 token
        # 格式：{token_id: {"market_id": ..., "side": ..., "last_attempt_time": ..., "target_price": ..., "order_size": ...}}
        self.pending_reorder_tokens: Dict[str, Dict[str, Any]] = {}
        # 部分成交订单跟踪：order_id -> tracking_info
        # 格式：{order_id: {"original_size": ..., "filled_size": ..., "hedged_size": ..., "last_position": ..., "last_check_time": ..., "market_id": ..., "token_id": ..., "price": ...}}
        self.partial_filled_tracking: Dict[str, Dict[str, Any]] = {}
        # 对冲卖出失败记录：token_id -> last_failure_time（用于避免频繁重试）
        # 格式：{token_id: last_failure_time}
        self.hedge_sell_failures: Dict[str, float] = {}
        # 订阅的 token 集合：用于管理小范围订阅（只订阅机会市场）
        # HTTP 方式不需要订阅，已移除 subscribed_tokens
        self.lock = threading.Lock()  # 线程锁
        
        # WebSocket 相关初始化已移除，现在使用 HTTP 接口获取订单簿
        
        logger.info("订单管理器初始化完成")
    
    def _build_order_options(self, market_id: str, token_id: str) -> PartialCreateOrderOptions:
        """构建 CLOB V2 下单选项。"""
        market = self.market_data_cache.get(market_id)
        tick_size = self.strategy.get_order_price_min_tick_size(market)
        tick_str = f"{tick_size:.4f}".rstrip("0").rstrip(".")
        if tick_str not in ("0.1", "0.01", "0.001", "0.0001"):
            tick_str = "0.01"

        neg_risk = None
        if market:
            neg_risk = market.get("neg_risk", market.get("negRisk"))

        if neg_risk is None:
            try:
                neg_risk = self.clob_client.get_neg_risk(token_id)
            except Exception:
                neg_risk = False

        return PartialCreateOrderOptions(tick_size=tick_str, neg_risk=neg_risk)
    
    def _get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        获取订单簿数据（使用 HTTP 接口）
        
        同时验证订单簿数据的有效性。
        
        Args:
            token_id: Token ID
            
        Returns:
            订单簿数据，如果没有数据或数据无效则返回 None
        """
        # 使用 HTTP 接口获取订单簿
        orderbook = self.api_client.get_orderbook(token_id)
        
        # 如果获取到数据，验证后返回
        if orderbook:
            # 验证订单簿数据的有效性
            if self._validate_orderbook(orderbook, token_id):
                return orderbook
            return None
        
        # 如果没有数据，返回 None
        return None
    
    def get_positions(
        self,
        user_address: str = None,
        size_threshold: float = 1.0,
        limit: int = 100,
        sort_by: str = "TOKENS",
        sort_direction: str = "DESC"
    ) -> List[Dict[str, Any]]:
        """
        获取用户持仓信息
        
        Args:
            user_address: 用户钱包地址，如果为None则从环境变量FUNDER_ADDRESS读取
            size_threshold: 最小持仓阈值（默认1.0）
            limit: 返回数量限制（默认100）
            sort_by: 排序字段，可选值：TOKENS, VALUE等（默认TOKENS）
            sort_direction: 排序方向，ASC或DESC（默认DESC）
        
        Returns:
            list: API返回的持仓列表，如果失败返回空列表
        """
        if user_address is None:
            user_address = os.getenv("FUNDER_ADDRESS") or config.get_funder_address()
        
        if not user_address:
            logger.error("无法获取用户地址，请设置FUNDER_ADDRESS环境变量")
            return []
        
        data_api_url = os.getenv("DATA_API_URL", "https://data-api.polymarket.com")
        url = f"{data_api_url}/positions"
        
        params = {
            "sizeThreshold": size_threshold,
            "limit": limit,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
            "user": user_address
        }
        
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # API返回的是数组格式
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "data" in data:
                # 兼容可能的字典格式
                return data.get("data", [])
            else:
                return []
        except requests.exceptions.RequestException as e:
            logger.error(f"获取持仓信息时发生错误: {e}")
            return []
    
    def _validate_orderbook(self, orderbook: Dict[str, Any], token_id: str) -> bool:
        """
        验证订单簿数据的有效性
        
        Args:
            orderbook: 订单簿数据
            token_id: Token ID（用于日志）
            
        Returns:
            如果数据有效返回 True，否则返回 False
        """
        if not orderbook:
            return False
        
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        
        # 验证 bids 和 asks 不为空
        if not bids and not asks:
            logger.warning(f"订单簿数据无效: token={token_id[:20]}..., bids 和 asks 都为空")
            return False
        
        # 验证价格在有效范围内 [0, 1]
        for bid in bids:
            try:
                price = float(bid.get("price", 0))
                if price < 0 or price > 1:
                    logger.warning(f"订单簿数据无效: token={token_id[:20]}..., bid 价格 {price} 超出范围 [0, 1]")
                    return False
            except (ValueError, TypeError):
                logger.warning(f"订单簿数据无效: token={token_id[:20]}..., bid 价格格式错误")
                return False
        
        for ask in asks:
            try:
                price = float(ask.get("price", 0))
                if price < 0 or price > 1:
                    logger.warning(f"订单簿数据无效: token={token_id[:20]}..., ask 价格 {price} 超出范围 [0, 1]")
                    return False
            except (ValueError, TypeError):
                logger.warning(f"订单簿数据无效: token={token_id[:20]}..., ask 价格格式错误")
                return False
        
        return True
    
    def _subscribe_market_tokens(self, market: Dict[str, Any]):
        """
        订阅市场的所有 token（HTTP 方式不需要订阅，此方法保留为空实现以保持接口兼容）
        
        Args:
            market: 市场数据
        """
        # HTTP 方式不需要订阅，直接返回
        pass
    
    def _unsubscribe_market_tokens(self, market_id: str):
        """
        取消订阅市场的所有 token（HTTP 方式不需要订阅，此方法保留为空实现以保持接口兼容）
        
        Args:
            market_id: 市场ID
        """
        # HTTP 方式不需要订阅，直接返回
        pass
    
    def place_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: OrderType = OrderType.GTC
    ) -> Optional[Dict[str, Any]]:
        """
        下单（在奖励区间边界挂单）
        
        Args:
            market_id: 市场ID
            token_id: Token ID
            side: 订单方向，"BUY" 或 "SELL"
            price: 订单价格（0-1之间）
            size: 订单份额
            order_type: 订单类型（默认 GTC - Good Till Cancel）
            
        Returns:
            订单响应数据，如果下单失败返回 None
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            logger.error(f"未知的订单方向: {side}，拒绝下单")
            return None

        # 检查敞口限制（卖出订单不受敞口限制，直接跳过检查）
        if side != "SELL":
            if not self.risk_manager.can_place_order(market_id, price, size, side):
                logger.warning(f"市场 {market_id} 下单被风险限制拒绝")
                return None
        
        # 方向性取整：BUY 向下、SELL 向上（避免浮点误差，使用 Decimal）
        market = self.market_data_cache.get(market_id)
        tick_size = self.strategy.get_order_price_min_tick_size(market)
        price = self.strategy.round_price_to_tick(price, tick_size, side)
        
        # 验证价格是否在有效范围内（双重检查）
        if price < 0.01 or price > 1.0:
            logger.error(
                f"价格超出有效范围 [0.01, 1.0]: {price:.4f}, "
                f"市场={market_id}, token={token_id[:20]}..., 方向={side}"
            )
            return None
        
        # 重试机制
        retry_count = config.order_retry_count
        retry_delay = config.order_retry_delay
        
        for attempt in range(retry_count):
            try:
                # 创建订单
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY if side == "BUY" else SELL
                )
                
                order_options = self._build_order_options(market_id, token_id)
                signed_order = self.clob_client.create_order(order_args, order_options)
                response = self.clob_client.post_order(signed_order, order_type)
                
                # 检查订单是否成功
                # 响应中可能使用 "id" 或 "orderID" 字段
                if response and response.get("success"):
                    order_id = response.get("id") or response.get("orderID")
                    if not order_id:
                        # 如果既没有 "id" 也没有 "orderID"，认为订单失败
                        error_msg = response.get("errorMsg", "未知错误") if response else "响应为空"
                        logger.error(f"下单失败: 响应中缺少订单ID, 错误信息={error_msg}, 响应={response}")
                        return None
                    
                    # 计算敞口并添加到风险管理器
                    exposure = self.risk_manager.calculate_exposure(price, size, side)
                    if not self.risk_manager.add_exposure(market_id, exposure):
                        # 如果添加敞口失败，取消订单
                        logger.warning(f"添加敞口失败，取消订单 {order_id}")
                        try:
                            self.clob_client.cancel_order(OrderPayload(orderID=order_id))
                        except Exception as e:
                            logger.error(f"取消订单失败: {e}")
                        return None
                    
                    # 记录订单
                    with self.lock:
                        if market_id not in self.active_orders:
                            self.active_orders[market_id] = {}
                        if token_id not in self.active_orders[market_id]:
                            self.active_orders[market_id][token_id] = {}
                        
                        self.active_orders[market_id][token_id][side.upper()] = {
                            "order_id": order_id,
                            "token_id": token_id,
                            "side": side.upper(),
                            "price": price,
                            "size": size,
                            "exposure": exposure,
                            "created_at": time.time(),
                            "response": response
                        }
                    
                    # 获取订单状态描述
                    order_status = response.get("status", "unknown")
                    status_map = {
                        "live": "订单已提交并挂单",
                        "matched": "订单已提交并与现有挂单匹配成交",
                        "delayed": "订单具备即时可成交性，但需遵循匹配延迟处理",
                        "unmatched": "订单可成交，但因系统延迟匹配失败，下单操作已成功"
                    }
                    status_desc = status_map.get(order_status, f"订单状态: {order_status}")
                    
                    # 根据价格精度格式化价格显示（如果价格有三位小数，显示三位；否则显示两位）
                    price_decimal_places = 2
                    price_str = f"{price:.10f}".rstrip('0').rstrip('.')
                    if '.' in price_str:
                        price_decimal_places = len(price_str.split('.')[1])
                        price_decimal_places = min(price_decimal_places, 4)  # 最多显示4位小数
                    
                    logger.info(
                        f"下单成功: 市场={market_id}, token={token_id[:20]}..., "
                        f"方向={side}, 价格={price:.{price_decimal_places}f}, 份额={size:.2f}, 订单ID={order_id}, {status_desc}"
                    )
                    
                    # 如果下单响应已经成交/匹配，则不会出现在 open orders 中，直接信任响应
                    if order_status in ("matched", "delayed", "unmatched"):
                        return response

                    # 验证订单是否真正挂单：轮询查询活跃订单列表
                    # 注意：CLOB V2 的 open orders 接口存在传播延迟，新挂订单不会立即出现，
                    # 因此需要多次重试，避免把真实存在的挂单误判为失败
                    try:
                        verify_attempts = 4
                        verify_delay = 0.5
                        order_found = False
                        for verify_idx in range(verify_attempts):
                            time.sleep(verify_delay)
                            open_orders = self.clob_client.get_open_orders(OpenOrderParams())
                            open_order_ids = {order.get("id") for order in open_orders if order.get("id")}
                            if order_id in open_order_ids:
                                order_found = True
                                break
                        
                        if not order_found:
                            # 轮询未查到，但 post_order 已返回 success=True 且 status=live。
                            # CLOB V2 的 open orders 接口存在传播延迟，这里很可能是误判，
                            # 因此信任下单响应、保留内部记录，仅记录告警。
                            # 后续 check_orders 循环会定期与真实挂单对账修正。
                            price_decimal_places = 2
                            price_str = f"{price:.10f}".rstrip('0').rstrip('.')
                            if '.' in price_str:
                                price_decimal_places = len(price_str.split('.')[1])
                                price_decimal_places = min(price_decimal_places, 4)  # 最多显示4位小数
                            
                            logger.warning(
                                f"订单暂未在活跃列表确认（可能为接口传播延迟），信任下单响应保留记录: "
                                f"订单ID={order_id}, 市场={market_id}, token={token_id[:20]}..., "
                                f"方向={side}, 价格={price:.{price_decimal_places}f}, 份额={size:.2f}"
                            )
                        else:
                            # 订单验证成功，真正挂单
                            logger.info(
                                f"订单验证成功: 订单ID={order_id} 已在活跃订单列表中，"
                                f"市场={market_id}, token={token_id[:20]}..., 方向={side}"
                            )
                    except Exception as e:
                        # 验证过程出错，记录警告但不确定订单是否成功
                        logger.warning(
                            f"订单验证过程出错: {e}, 订单ID={order_id}, "
                            f"无法确认订单是否真正挂单，但API返回成功"
                        )
                        # 继续返回响应，因为API已返回成功
                    
                    return response
                else:
                    error_msg = response.get("errorMsg", "未知错误") if response else "响应为空"
                    
                    # 判断是否可重试的错误
                    is_retryable = self._is_retryable_error(error_msg, response)
                    
                    if not is_retryable or attempt == retry_count - 1:
                        # 不可重试的错误或已达到最大重试次数
                        logger.error(f"下单失败: {error_msg}, 响应={response}")
                        return None
                    else:
                        # 可重试的错误，等待后重试
                        wait_time = retry_delay * (2 ** attempt)  # 指数退避
                        logger.warning(f"下单失败（可重试）: {error_msg}，{wait_time:.1f}秒后重试 ({attempt + 1}/{retry_count})")
                        time.sleep(wait_time)
                        continue
                        
            except Exception as e:
                error_str = str(e)
                is_retryable = self._is_retryable_exception(e)
                
                if not is_retryable or attempt == retry_count - 1:
                    # 不可重试的异常或已达到最大重试次数
                    logger.error(f"下单失败: {e}")
                    return None
                else:
                    # 可重试的异常，等待后重试
                    wait_time = retry_delay * (2 ** attempt)  # 指数退避
                    logger.warning(f"下单失败（可重试）: {error_str}，{wait_time:.1f}秒后重试 ({attempt + 1}/{retry_count})")
                    time.sleep(wait_time)
                    continue
        
        return None
    
    def _is_retryable_error(self, error_msg: str, response: Optional[Dict[str, Any]]) -> bool:
        """
        判断错误是否可重试
        
        Args:
            error_msg: 错误消息
            response: 响应数据
            
        Returns:
            如果可重试返回 True，否则返回 False
        """
        # 不可重试的错误
        non_retryable_keywords = [
            "insufficient balance",
            "余额不足",
            "invalid price",
            "价格无效",
            "invalid size",
            "份额无效",
            "unauthorized",
            "未授权"
        ]
        
        error_msg_lower = error_msg.lower()
        for keyword in non_retryable_keywords:
            if keyword in error_msg_lower:
                return False
        
        # 网络错误、超时等可重试
        retryable_keywords = [
            "timeout",
            "超时",
            "network",
            "网络",
            "connection",
            "连接",
            "temporary",
            "临时"
        ]
        
        for keyword in retryable_keywords:
            if keyword in error_msg_lower:
                return True
        
        # 默认情况下，如果响应中有 success=False，可能是可重试的
        if response and not response.get("success", True):
            return True
        
        return False
    
    def _is_retryable_exception(self, exception: Exception) -> bool:
        """
        判断异常是否可重试
        
        Args:
            exception: 异常对象
            
        Returns:
            如果可重试返回 True，否则返回 False
        """
        error_str = str(exception).lower()
        
        # 网络相关异常可重试
        retryable_keywords = [
            "timeout",
            "超时",
            "connection",
            "连接",
            "network",
            "网络",
            "temporary",
            "临时"
        ]
        
        for keyword in retryable_keywords:
            if keyword in error_str:
                return True
        
        # 其他异常默认不可重试
        return False
    
    def place_market_orders(
        self,
        market: Dict[str, Any],
        orderbooks_dict: Dict[str, Dict[str, Any]]
    ) -> Dict[str, bool]:
        """
        为市场挂单（在奖励区间边界挂买单和卖单）
        
        挂单前会先订阅该市场的所有 token，等待实时数据更新，然后使用最新数据下单
        
        Args:
            market: 市场数据
            orderbooks_dict: 订单簿数据字典 {token_id: orderbook}（备选数据源）
            
        Returns:
            字典 {token_id: success}，表示每个 token 的挂单是否成功
        """
        import time
        
        market_id = market.get("market_id")
        tokens = market.get("tokens", [])
        rewards_max_spread = market.get("rewards_max_spread", 0)
        results = {}
        
        # 1. 先订阅该市场的所有 token（用于获取实时数据）
        # HTTP 方式不需要订阅，直接使用传入的订单簿数据
        logger.info(f"使用 HTTP 接口获取市场 {market_id} 的订单簿数据...")
        
        # HTTP 方式不需要等待，直接使用传入的订单簿数据
        token_ids = [token.get("token_id") for token in tokens if token.get("token_id")]
        
        # 3. 更新市场数据缓存
        self.market_data_cache[market_id] = market
        
        # 4. 使用实时数据下单
        # 构建市场链接
        event_slug = market.get('event_slug', '')
        market_slug = market.get('market_slug', '')
        market_url = ""
        if event_slug and market_slug:
            market_url = f"https://polymarket.com/event/{event_slug}/{market_slug}"
        
        logger.info("=" * 80)
        logger.info(f"市场详情: ID={market_id}, 问题={market.get('question', 'N/A')[:60]}...")
        if market_url:
            logger.info(f"  市场链接: {market_url}")
        logger.info(f"  奖励配置: 每日奖励率={market.get('rewards_config', [{}])[0].get('rate_per_day', 0)} USDC, "
                   f"最小份额={market.get('rewards_min_size', 0)}, "
                   f"奖励最大价差={rewards_max_spread} 美分")
        logger.info("-" * 80)
        
        # 第一步：遍历所有 tokens，计算中间价，判断是否需要双边挂单
        # Polymarket 规定：如果任何 token 的中间价 <= 0.10，必须同时在 Yes 和 No 上挂单
        token_mid_prices = {}  # {token_id: mid_price}
        requires_both_tokens = False  # 是否需要同时在 Yes 和 No 上挂单
        
        for token in tokens:
            token_id = token.get("token_id")
            outcome = token.get("outcome", "N/A")
            
            if not token_id:
                continue
            
            # 获取订单簿数据用于计算中间价
            orderbook = self._get_orderbook(token_id)
            if not orderbook:
                orderbook = orderbooks_dict.get(token_id)
            
            if orderbook:
                # 计算中间价
                mid_price = self.strategy.calculate_mid_price(orderbook)
                if mid_price is not None:
                    token_mid_prices[token_id] = mid_price
                    # 如果任何 token 的中间价 <= 0.10，需要双边挂单
                    if mid_price <= 0.10:
                        requires_both_tokens = True
                        logger.info(f"  检测到 token {outcome} 中间价 {mid_price:.4f} <= 0.10，需要双边挂单（Yes 和 No）")
        
        # 第二步：根据判断结果决定挂单策略
        if requires_both_tokens:
            logger.info(f"  ⚠️  需要双边挂单：将在所有 token（Yes 和 No）上挂单")
        else:
            logger.info(f"  正常挂单：将对所有 token 挂单")
        
        # 第三步：遍历所有 tokens 进行挂单
        # 记录成功挂单的订单信息，用于双边挂单验证
        successful_orders = {}  # {token_id: order_id}
        
        for token in tokens:
            token_id = token.get("token_id")
            outcome = token.get("outcome", "N/A")
            
            if not token_id:
                continue
            
            # 强制实时获取订单簿数据（避免使用过期数据造成损失）
            orderbook = self._get_orderbook(token_id)
            use_conservative_price = False  # 是否使用保守价格
            
            # 如果实时获取失败，尝试使用备选数据源（但使用保守价格并记录警告）
            if not orderbook:
                logger.warning(
                    f"市场 {market_id} token {outcome} 实时获取订单簿失败，"
                    f"尝试使用备选数据源（可能已过期）"
                )
                orderbook = orderbooks_dict.get(token_id)
                if orderbook:
                    # 使用备选数据源时，强制使用保守价格（在边界基础上再向外偏移，降低被成交风险）
                    use_conservative_price = True
                    logger.warning(
                        f"市场 {market_id} token {outcome} 使用备选订单簿数据（可能已过期），"
                        f"将使用保守价格下单（降低被成交风险），等待主循环调整"
                    )
                else:
                    logger.error(f"市场 {market_id} token {outcome} 没有可用的订单簿数据，跳过挂单")
                    # 如果需要双边挂单但某个 token 没有数据，记录警告
                    if requires_both_tokens:
                        logger.error(
                            f"  ⚠️  警告：需要双边挂单，但 token {outcome} 没有订单簿数据，"
                            f"可能无法获得奖励"
                        )
                    results[token_id] = False
                    continue
            else:
                logger.info(f"市场 {market_id} token {outcome} 使用实时订单簿数据")
            
            # 计算订单价格（奖励区间边界，如果数据过期则使用保守价格）
            prices = self.strategy.calculate_order_prices(
                orderbook, 
                rewards_max_spread,
                use_conservative_price=use_conservative_price,
                market=market
            )
            if not prices:
                logger.warning(f"市场 {market_id} token {outcome} 无法计算订单价格，跳过")
                # 如果需要双边挂单但无法计算价格，记录警告
                if requires_both_tokens:
                    logger.error(
                        f"  ⚠️  警告：需要双边挂单，但 token {outcome} 无法计算价格，"
                        f"可能无法获得奖励"
                    )
                results[token_id] = False
                continue
            
            # 计算订单份额
            order_size = self.strategy.calculate_order_size(market)
            
            # 计算中间价和奖励区间
            mid_price = prices.get("mid_price", 0)
            buy_price = prices.get("buy_price")
            sell_price = prices.get("sell_price")
            
            # 计算竞争份额（奖励区间内的挂单份额）
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            competition_buy = 0.0  # 奖励区间内的买单份额
            competition_sell = 0.0  # 奖励区间内的卖单份额
            
            if buy_price and sell_price:
                for bid in bids:
                    bid_price = float(bid.get("price", 0))
                    if buy_price <= bid_price <= sell_price:
                        competition_buy += float(bid.get("size", 0))
                
                for ask in asks:
                    ask_price = float(ask.get("price", 0))
                    if buy_price <= ask_price <= sell_price:
                        competition_sell += float(ask.get("size", 0))
            
            total_competition = competition_buy + competition_sell
            
            # 计算实际挂单价格（买二价或奖励下边界）
            actual_buy_price = self.strategy.calculate_actual_buy_price(orderbook, buy_price)
            
            # 如果没有实际挂单价格（订单簿只有买一价），跳过挂单
            if actual_buy_price is None:
                logger.warning(f"    订单簿只有买一价，跳过挂单")
                results[token_id] = False
                if requires_both_tokens:
                    logger.error(
                        f"    ⚠️  严重警告：需要双边挂单，但 token {outcome} 因订单簿只有买一价跳过挂单，"
                        f"可能无法获得奖励"
                    )
                continue
            
            # 风险管理：检查是否可以安全挂买单（挂单后成为买二价，并检测价格断层）
            can_place, safety_info = self.strategy.can_place_buy_order_safely(
                orderbook, buy_price, sell_price, order_size, actual_buy_price
            )
            
            # 打印详细信息
            logger.info(f"  Outcome: {outcome}")
            logger.info(f"    Token ID: {token_id[:30]}...")
            logger.info(f"    中间价: {mid_price:.4f}")
            if requires_both_tokens:
                logger.info(f"    ⚠️  需要双边挂单（中间价 <= 0.10）")
            logger.info(f"    奖励区间: [{buy_price:.4f}, {sell_price:.4f}]")
            logger.info(f"    竞争份额:")
            logger.info(f"      - 奖励区间内买单份额: {competition_buy:.2f}")
            logger.info(f"      - 奖励区间内卖单份额: {competition_sell:.2f}")
            logger.info(f"      - 总竞争份额: {total_competition:.2f}")
            
            # 显示订单簿信息
            if safety_info.get("best_bid") is not None:
                best_bid_size = safety_info.get("best_bid_size", 0.0)
                logger.info(f"    订单簿买一价: {safety_info['best_bid']:.4f}, 份额: {best_bid_size:.2f}")
            if safety_info.get("second_bid") is not None:
                second_bid_size = safety_info.get("second_bid_size", 0.0)
                logger.info(f"    订单簿买二价: {safety_info['second_bid']:.4f}, 份额: {second_bid_size:.2f}")
            
            logger.info(f"    价格分析（相对于挂单价 {actual_buy_price:.4f}）:")
            logger.info(f"      - 价格 > 挂单价的买单数量: {safety_info.get('count_above', 0)}（排在我们前面）")
            logger.info(f"      - 价格 > 挂单价的不同价格数量: {safety_info.get('unique_prices_above', 0)}（买一价、买二价等）")
            if safety_info.get('bids_above'):
                unique_prices = sorted(set(safety_info['bids_above']), reverse=True)
                logger.info(f"      - 价格 > 挂单价的价格列表: {[f'{p:.4f}' for p in unique_prices[:5]]}{'...' if len(unique_prices) > 5 else ''}")
            logger.info(f"      - 价格 = 挂单价的买单数量: {safety_info.get('count_at', 0)}（和我们同一位置）")
            if safety_info.get('bids_at'):
                logger.info(f"      - 价格 = 挂单价的买单价格: {[f'{p:.4f}' for p in safety_info['bids_at']]}")
            logger.info(f"      - 价格 < 挂单价的买单数量: {safety_info.get('count_below', 0)}（排在我们后面）")
            logger.info(f"    挂单后位置: 买{safety_info.get('our_position', 'N/A')}价")
            
            logger.info(f"    奖励区间内买单数量: {safety_info.get('count_in_range', 0)}")
            if safety_info.get('bids_in_range'):
                logger.info(f"    奖励区间内买单价格: {[f'{p:.4f}' for p in safety_info['bids_in_range']]}")
            
            # 显示价格断层检测信息
            if safety_info.get('price_cliff_detected', False):
                logger.warning(f"    ⚠️  价格断层检测: 检测到价格断层风险")
                logger.warning(f"      原因: {safety_info.get('price_cliff_reason', '未知')}")
            else:
                logger.info(f"    ✓ 价格断层检测: 未检测到价格断层")
                # 显示后1/2/3价信息
                if safety_info.get('next_prices'):
                    logger.info(f"      后1/2/3价信息:")
                    for next_price_info in safety_info['next_prices']:
                        logger.info(
                            f"        后{next_price_info['position']}价: {next_price_info['price']:.4f}, "
                            f"价差={next_price_info['price_diff']:.4f}, "
                            f"份额={next_price_info['size']:.2f}, "
                            f"累计份额={next_price_info['cumulative_size']:.2f}"
                        )
                # 显示保护份额信息
                if safety_info.get('total_protection_size') is not None:
                    logger.info(
                        f"      买一价和买二价总份额: {safety_info['total_protection_size']:.2f}, "
                        f"最小保护份额要求: {safety_info.get('min_protection_size', 0):.2f}"
                    )
            
            logger.info(f"    计划挂单:")
            if actual_buy_price:
                price_desc = "买二价" if safety_info.get('is_second_bid_price', False) else "奖励下边界"
                if can_place:
                    logger.info(f"      - 买单: 价格={actual_buy_price:.4f} ({price_desc}), 份额={order_size:.2f} ✓ 安全：{safety_info['reason']}")
                else:
                    logger.warning(f"      - 买单: 价格={actual_buy_price:.4f} ({price_desc}), 份额={order_size:.2f} ⚠️ 风险：{safety_info['reason']}，跳过挂单")
            logger.info(f"      - 卖单: 不挂单（无持仓，卖单将在买单成交后自动对冲挂出）")
            logger.info("-" * 80)
            
            # 风险管理：如果不能安全挂单，跳过挂单
            if not can_place:
                logger.warning(
                    f"    ⚠️  风险管理：跳过挂单 - {safety_info['reason']}"
                )
                results[token_id] = False
                # 如果需要双边挂单但跳过挂单，记录警告
                if requires_both_tokens:
                    logger.error(
                        f"    ⚠️  严重警告：需要双边挂单，但 token {outcome} 因风险管理跳过挂单，"
                        f"可能无法获得奖励"
                    )
                
                # 将 token 信息添加到 pending_reorder_tokens，以便后续订单调整循环中重新检查
                # 只有在 actual_buy_price 存在时才添加到待重新挂单列表
                if actual_buy_price:
                    with self.lock:
                        self.pending_reorder_tokens[token_id] = {
                            "market_id": market_id,
                            "side": "BUY",
                            "last_attempt_time": time.time(),
                            "target_price": actual_buy_price,  # 使用实际挂单价格作为目标价格
                            "order_size": order_size,
                            "safety_info": safety_info
                        }
                        logger.info(
                            f"    已记录到待重新挂单列表: token={token_id[:30]}..., "
                            f"目标价格={actual_buy_price:.4f}, 原因={safety_info['reason']}"
                        )
                continue
            
            # 只挂买单（使用实际挂单价格：买二价或奖励下边界）
            # 注意：不挂卖单，因为无持仓无法挂卖单
            # 卖单将在买单成交后，通过 place_hedge_sell() 自动挂出
            if actual_buy_price:
                # 买单不进行规范化，直接使用 actual_buy_price（原始买二价或奖励下边界）
                # 因为原始买二价一定是能下单的价格，奖励下边界是两位小数也一定能下单
                buy_result = self.place_order(
                    market_id=market_id,
                    token_id=token_id,
                    side="BUY",
                    price=actual_buy_price,
                    size=order_size
                )
                results[f"{token_id}_BUY"] = buy_result is not None
                if buy_result:
                    # 记录成功挂单的订单ID（响应中可能使用 "id" 或 "orderID" 字段）
                    order_id = buy_result.get("id") or buy_result.get("orderID")
                    if order_id:
                        successful_orders[token_id] = order_id
                    else:
                        # 如果响应中没有订单ID，尝试从 active_orders 中获取
                        with self.lock:
                            if market_id in self.active_orders:
                                if token_id in self.active_orders[market_id]:
                                    if "BUY" in self.active_orders[market_id][token_id]:
                                        order_id = self.active_orders[market_id][token_id]["BUY"].get("order_id")
                                        if order_id:
                                            successful_orders[token_id] = order_id
                    price_desc = "买二价" if safety_info.get('is_second_bid_price', False) else "奖励下边界"
                    logger.info(f"    ✓ 买单已提交: 价格={actual_buy_price:.4f} ({price_desc}), 份额={order_size:.2f}, 订单ID={order_id}")
                else:
                    logger.warning(f"    ✗ 买单提交失败")
                    # 如果需要双边挂单但挂单失败，记录严重警告
                    if requires_both_tokens:
                        logger.error(
                            f"    ⚠️  严重警告：需要双边挂单，但 token {outcome} 挂单失败，"
                            f"可能无法获得奖励"
                        )
        
        logger.info("=" * 80)
        
        # 最后检查：如果需要双边挂单，验证是否所有 token 都成功挂单
        if requires_both_tokens:
            success_count = sum(1 for k, v in results.items() if v and k.endswith("_BUY"))
            total_tokens = len([t for t in tokens if t.get("token_id")])
            
            if success_count < total_tokens:
                # 只有单边挂单成功，需要取消所有已挂订单
                logger.warning(
                    f"  ⚠️  需要双边挂单，但只有 {success_count}/{total_tokens} 个 token 成功挂单，"
                    f"将取消所有已挂订单以避免损失"
                )
                
                # 取消所有已成功挂单的订单
                cancelled_count = 0
                for token_id, order_id in successful_orders.items():
                    # 查找对应的 outcome 用于日志
                    outcome = "N/A"
                    for token in tokens:
                        if token.get("token_id") == token_id:
                            outcome = token.get("outcome", "N/A")
                            break
                    
                    logger.info(f"  正在取消订单: token={outcome}, token_id={token_id[:30]}..., order_id={order_id}")
                    if self.cancel_order(order_id):
                        cancelled_count += 1
                        logger.info(f"  ✓ 订单已取消: token={outcome}, order_id={order_id}")
                    else:
                        logger.warning(f"  ✗ 订单取消失败: token={outcome}, order_id={order_id}")
                
                logger.warning(
                    f"  已取消 {cancelled_count}/{len(successful_orders)} 个已挂订单。"
                    f"原因：需要双边挂单但只有单边成功，单边挂单既无法获得奖励又可能因市场波动造成损失"
                )
                
                # 更新 results 字典，将所有相关结果设为 False
                for token_id in successful_orders.keys():
                    results[f"{token_id}_BUY"] = False
                    results[token_id] = False
            else:
                logger.info(f"  ✓ 双边挂单完成：所有 {success_count} 个 token 都已成功挂单")
        
        return results
    
    def _query_order_filled_size(self, order_id: str) -> float:
        """
        查询订单的实际成交份额
        
        通过交易历史累计所有相关交易的成交份额
        
        Args:
            order_id: 订单ID
            
        Returns:
            累计成交份额，如果查询失败返回0.0
        """
        try:
            # 查询最近的交易历史
            trades = self.clob_client.get_trades(TradeParams(), next_cursor="MA==")
            
            total_filled = 0.0
            
            # 遍历所有交易，累计该订单的成交份额
            for trade in trades:
                # 检查 taker_order_id
                taker_order_id = trade.get("taker_order_id")
                if taker_order_id == order_id:
                    # 作为 taker 的成交份额
                    size = float(trade.get("size", 0))
                    total_filled += size
                
                # 检查 maker_orders 数组中的 order_id
                maker_orders = trade.get("maker_orders", [])
                if isinstance(maker_orders, list):
                    for maker_order in maker_orders:
                        maker_order_id = maker_order.get("order_id")
                        if maker_order_id == order_id:
                            # 作为 maker 的成交份额
                            matched_amount = float(maker_order.get("matched_amount", 0))
                            total_filled += matched_amount
            
            return total_filled
        except Exception as e:
            logger.warning(f"查询订单 {order_id[:20]}... 的成交份额失败: {e}")
            return 0.0
    
    def _track_partial_filled_order(
        self,
        order_id: str,
        order_info: Dict[str, Any],
        filled_size: float,
        current_position: float
    ) -> None:
        """
        将部分成交订单添加到跟踪列表
        
        Args:
            order_id: 订单ID
            order_info: 订单信息（包含 market_id, token_id, price, size 等）
            filled_size: 已成交份额
            current_position: 当前持仓
        """
        with self.lock:
            self.partial_filled_tracking[order_id] = {
                "original_size": order_info.get("size", 0),
                "filled_size": filled_size,
                "hedged_size": 0.0,  # 已对冲卖出份额（初始为0）
                "last_position": current_position,
                "last_check_time": time.time(),
                "market_id": order_info.get("market_id"),
                "token_id": order_info.get("token_id"),
                "price": order_info.get("price", 0)
            }
            logger.info(
                f"开始跟踪部分成交订单: 订单ID={order_id[:20]}..., "
                f"原始份额={order_info.get('size', 0):.2f}, "
                f"已成交份额={filled_size:.2f}, "
                f"当前持仓={current_position:.2f}"
            )
    
    def check_orders(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        检查订单状态，检测成交
        
        优先使用持仓信息检测订单成交（更可靠），如果持仓检测失败，则使用交易历史查询作为补充
        
        Returns:
            字典 {market_id: [filled_orders]}，包含已成交的订单信息
        """
        filled_orders_by_market = {}
        cancelled_count = 0
        
        try:
            # 第一步：检查部分成交订单跟踪列表，检测剩余部分的成交
            if self.partial_filled_tracking:
                try:
                    # 获取当前持仓
                    positions = self.get_positions(size_threshold=0.1, limit=1000)
                    positions_by_asset = {}
                    for position in positions:
                        asset = position.get("asset")
                        if asset:
                            positions_by_asset[asset] = position
                    
                    # 检查每个被跟踪的部分成交订单
                    tracking_to_remove = []
                    for order_id, tracking_info in list(self.partial_filled_tracking.items()):
                        token_id = tracking_info.get("token_id")
                        last_position = tracking_info.get("last_position", 0.0)
                        filled_size = tracking_info.get("filled_size", 0.0)
                        hedged_size = tracking_info.get("hedged_size", 0.0)
                        original_size = tracking_info.get("original_size", 0.0)
                        market_id = tracking_info.get("market_id")
                        price = tracking_info.get("price", 0)
                        
                        # 获取当前持仓
                        current_position = 0.0
                        if token_id:
                            position = positions_by_asset.get(token_id)
                            if position:
                                current_position = float(position.get("size", 0))
                        
                        # 重新查询交易历史，获取订单的累计成交份额（更可靠）
                        actual_filled_size = self._query_order_filled_size(order_id)
                        
                        # 如果累计成交份额 > 已记录的成交份额，说明有新成交
                        if actual_filled_size > filled_size:
                            # 计算新增成交份额
                            new_filled_size = actual_filled_size - filled_size
                            new_total_filled = actual_filled_size
                            
                            logger.info(
                                f"检测到部分成交订单继续成交: 订单ID={order_id[:20]}..., "
                                f"上次持仓={last_position:.2f}, 当前持仓={current_position:.2f}, "
                                f"已成交份额={filled_size:.2f}, 新增成交份额={new_filled_size:.2f}, "
                                f"累计成交份额={new_total_filled:.2f}"
                            )
                            
                            # 挂出对应对冲卖单（只对冲新增的部分）
                            try:
                                hedge_result = self.place_hedge_sell(
                                    market_id=market_id,
                                    token_id=token_id,
                                    buy_price=price,
                                    filled_size=new_filled_size
                                )
                                if hedge_result:
                                    # 更新跟踪信息（使用锁保护）
                                    with self.lock:
                                        if order_id in self.partial_filled_tracking:
                                            tracking_info = self.partial_filled_tracking[order_id]
                                            tracking_info["filled_size"] = new_total_filled
                                            tracking_info["last_position"] = current_position
                                            tracking_info["last_check_time"] = time.time()
                                            tracking_info["hedged_size"] = hedged_size + new_filled_size
                                    logger.info(
                                        f"部分成交订单新增部分对冲卖出成功: 订单ID={order_id[:20]}..., "
                                        f"新增成交份额={new_filled_size:.2f}"
                                    )
                                else:
                                    # 即使对冲失败，也要更新成交份额
                                    with self.lock:
                                        if order_id in self.partial_filled_tracking:
                                            tracking_info = self.partial_filled_tracking[order_id]
                                            tracking_info["filled_size"] = new_total_filled
                                            tracking_info["last_position"] = current_position
                                            tracking_info["last_check_time"] = time.time()
                                    logger.warning(
                                        f"部分成交订单新增部分对冲卖出失败: 订单ID={order_id[:20]}..."
                                    )
                            except Exception as e:
                                # 即使出错，也要更新成交份额
                                with self.lock:
                                    if order_id in self.partial_filled_tracking:
                                        tracking_info = self.partial_filled_tracking[order_id]
                                        tracking_info["filled_size"] = new_total_filled
                                        tracking_info["last_position"] = current_position
                                        tracking_info["last_check_time"] = time.time()
                                logger.error(f"部分成交订单新增部分对冲卖出时发生错误: {e}")
                            
                            # 检查是否完全成交
                            if new_total_filled >= original_size:
                                logger.info(
                                    f"部分成交订单已完全成交: 订单ID={order_id[:20]}..., "
                                    f"累计成交份额={new_total_filled:.2f}, 原始份额={original_size:.2f}"
                                )
                                tracking_to_remove.append(order_id)
                        else:
                            # 没有新成交（actual_filled_size <= filled_size），更新最后检查时间和持仓（使用锁保护）
                            with self.lock:
                                if order_id in self.partial_filled_tracking:
                                    tracking_info = self.partial_filled_tracking[order_id]
                                    tracking_info["last_position"] = current_position
                                    tracking_info["last_check_time"] = time.time()
                            
                            # 检查是否完全成交（通过累计成交份额判断）
                            if actual_filled_size >= original_size:
                                logger.info(
                                    f"部分成交订单已完全成交（通过累计成交份额判断）: 订单ID={order_id[:20]}..., "
                                    f"累计成交份额={actual_filled_size:.2f}, 原始份额={original_size:.2f}"
                                )
                                tracking_to_remove.append(order_id)
                    
                    # 移除已完全成交的订单跟踪
                    with self.lock:
                        for order_id in tracking_to_remove:
                            if order_id in self.partial_filled_tracking:
                                del self.partial_filled_tracking[order_id]
                                logger.info(f"移除已完全成交的订单跟踪: 订单ID={order_id[:20]}...")
                
                except Exception as e:
                    logger.warning(f"检查部分成交订单跟踪列表时发生错误: {e}")
            
            # 获取所有活跃订单
            try:
                open_orders = self.clob_client.get_open_orders(OpenOrderParams())
                open_order_ids = {order.get("id") for order in open_orders if order.get("id")}
            except Exception as e:
                # 如果获取活跃订单失败（如 API 认证错误），记录错误并返回空结果
                try:
                    error_msg = str(e)
                    if "401" in error_msg or "Unauthorized" in error_msg or "Invalid api key" in error_msg:
                        logger.error(f"获取活跃订单时发生 API 认证错误: {e}，请检查 API 密钥是否有效，跳过本次订单检查")
                    else:
                        logger.error(f"获取活跃订单失败: {e}，跳过本次订单检查")
                except:
                    pass  # 如果日志写入也失败，静默忽略
                return filled_orders_by_market  # 返回空结果，让程序继续运行
            
            # 收集所有不在活跃列表中的订单ID
            # 排除已经在部分成交跟踪列表中的订单（避免重复处理）
            missing_order_ids = []
            missing_order_info = {}  # order_id -> order_info
            
            with self.lock:
                tracked_order_ids = set(self.partial_filled_tracking.keys())
                for market_id, tokens_dict in list(self.active_orders.items()):
                    for token_id, sides_dict in list(tokens_dict.items()):
                        for side, order_info in list(sides_dict.items()):
                            order_id = order_info.get("order_id")
                            if order_id and order_id not in open_order_ids:
                                # 如果订单已经在跟踪列表中，跳过（避免重复处理）
                                if order_id not in tracked_order_ids:
                                    missing_order_ids.append(order_id)
                                    missing_order_info[order_id] = {
                                        "market_id": market_id,
                                        "token_id": token_id,
                                        "side": side,
                                        "order_info": order_info
                                    }
            
            # 如果有没有活跃的订单，使用持仓信息检测是否成交（优先方法）
            filled_order_ids = set()
            filled_order_sizes = {}  # order_id -> filled_size
            
            if missing_order_ids:
                # 方案1：使用持仓信息检测订单成交（更可靠）
                try:
                    logger.info(f"查询持仓信息以检测 {len(missing_order_ids)} 个不在活跃列表中的订单是否成交...")
                    positions = self.get_positions(size_threshold=0.1, limit=1000)  # 如果0.0导致出现很多持仓零头的干扰订单
                    
                    # 构建 token_id -> position 的映射
                    positions_by_asset = {}
                    for position in positions:
                        asset = position.get("asset")
                        if asset:
                            positions_by_asset[asset] = position
                    
                    logger.info(f"查询到 {len(positions)} 个持仓，涉及 {len(positions_by_asset)} 个不同的 asset")
                    
                    # 对于每个不在活跃列表中的订单，检查持仓并查询交易历史
                    for order_id in missing_order_ids:
                        order_info = missing_order_info.get(order_id)
                        if not order_info:
                            continue
                        
                        token_id = order_info.get("token_id")
                        side = order_info.get("side")
                        
                        # 只检查买单（买单成交会增加持仓）
                        if side == "BUY" and token_id:
                            position = positions_by_asset.get(token_id)
                            if position:
                                size = float(position.get("size", 0))
                                if size > 0:
                                    # 找到持仓，说明订单可能已成交
                                    # 但必须查询交易历史获取实际成交份额，不能仅凭持仓判断完全成交
                                    order_original_size = order_info.get("order_info", {}).get("size", 0)
                                    
                                    # 查询交易历史获取实际成交份额
                                    actual_filled_size = self._query_order_filled_size(order_id)
                                    
                                    if actual_filled_size > 0:
                                        # 有成交记录
                                        filled_order_ids.add(order_id)
                                        
                                        # 判断是完全成交还是部分成交
                                        if actual_filled_size >= order_original_size:
                                            # 完全成交
                                            filled_order_sizes[order_id] = order_original_size
                                            logger.info(
                                                f"✓ 通过持仓和交易历史检测到订单完全成交: 订单ID={order_id[:20]}..., "
                                                f"token_id={token_id[:20]}..., 持仓size={size:.2f}, "
                                                f"订单份额={order_original_size:.2f}, 实际成交份额={actual_filled_size:.2f}"
                                            )
                                        else:
                                            # 部分成交
                                            filled_order_sizes[order_id] = actual_filled_size
                                            logger.info(
                                                f"⚠ 通过持仓和交易历史检测到订单部分成交: 订单ID={order_id[:20]}..., "
                                                f"token_id={token_id[:20]}..., 持仓size={size:.2f}, "
                                                f"订单份额={order_original_size:.2f}, 实际成交份额={actual_filled_size:.2f}"
                                            )
                                    else:
                                        # 没有成交记录，但持仓存在，可能是之前的持仓
                                        # 这种情况不应该标记为成交
                                        logger.debug(
                                            f"订单不在活跃列表且有持仓，但交易历史无成交记录: 订单ID={order_id[:20]}..., "
                                            f"token_id={token_id[:20]}..., 持仓size={size:.2f}, "
                                            f"可能订单已取消或持仓来自其他订单"
                                        )
                    
                    logger.info(f"通过持仓检测: 找到 {len(filled_order_ids)} 个已成交订单")
                    
                except Exception as e:
                    logger.warning(f"查询持仓信息失败: {e}，将尝试使用交易历史查询")
                
                # 方案2：如果持仓检测没有找到所有订单，使用交易历史查询作为补充
                # 对于所有不在活跃列表中的订单，都需要查询交易历史确认实际成交份额
                remaining_missing_ids = [oid for oid in missing_order_ids if oid not in filled_order_ids]
                if remaining_missing_ids:
                    try:
                        logger.info(f"对 {len(remaining_missing_ids)} 个订单查询交易历史以确认成交份额...")
                        # 查询最近的交易历史
                        trades = self.clob_client.get_trades(TradeParams(), next_cursor="MA==")
                        
                        # 对于每个剩余订单，累计其成交份额
                        for order_id in remaining_missing_ids:
                            order_info = missing_order_info.get(order_id)
                            if not order_info:
                                continue
                            
                            side = order_info.get("side")
                            # 只处理买单
                            if side != "BUY":
                                continue
                            
                            # 查询该订单的实际成交份额
                            actual_filled_size = self._query_order_filled_size(order_id)
                            
                            if actual_filled_size > 0:
                                filled_order_ids.add(order_id)
                                filled_order_sizes[order_id] = actual_filled_size
                                
                                original_size = order_info.get("order_info", {}).get("size", 0)
                                if actual_filled_size >= original_size:
                                    logger.info(
                                        f"通过交易历史检测到订单完全成交: 订单ID={order_id[:20]}..., "
                                        f"订单份额={original_size:.2f}, 实际成交份额={actual_filled_size:.2f}"
                                    )
                                else:
                                    logger.info(
                                        f"通过交易历史检测到订单部分成交: 订单ID={order_id[:20]}..., "
                                        f"订单份额={original_size:.2f}, 实际成交份额={actual_filled_size:.2f}"
                                    )
                        
                        logger.info(f"交易历史查询补充: 总共找到 {len(filled_order_ids)} 个已成交订单")
                    except Exception as e:
                        logger.warning(f"查询交易历史失败: {e}")
                
                # 如果两种方法都没有找到，记录警告
                if len(filled_order_ids) == 0:
                    logger.warning(
                        f"无法通过持仓或交易历史检测到 {len(missing_order_ids)} 个订单的成交状态，"
                        f"这些订单可能已取消或查询失败"
                    )
            
            # 第一阶段：在持有锁的情况下，收集需要处理的订单信息
            partial_filled_orders = []  # 部分成交订单列表
            fully_filled_orders = []  # 完全成交订单列表（需要处理对冲卖出）
            risk_updates = []  # 风险敞口更新列表（在锁外执行，避免死锁）
            with self.lock:
                for market_id, tokens_dict in list(self.active_orders.items()):
                    filled_orders = []
                    
                    for token_id, sides_dict in list(tokens_dict.items()):
                        for side, order_info in list(sides_dict.items()):
                            order_id = order_info.get("order_id")
                            original_size = order_info.get("size", 0)
                            
                            # 检查订单是否仍在活跃列表中
                            if order_id in open_order_ids:
                                # 订单仍在活跃列表中，检查是否完全成交或部分成交
                                try:
                                    # 从活跃订单列表中查找该订单的详细信息
                                    for open_order in open_orders:
                                        if open_order.get("id") == order_id:
                                            # 获取订单的已成交份额和剩余份额
                                            filled = float(open_order.get("filled", 0))
                                            remaining = float(open_order.get("remaining", 0))
                                            current_size = float(open_order.get("size", original_size))
                                            
                                            # 首先检查是否完全成交（即使仍在活跃列表中，也可能是API延迟）
                                            # 注意：只有当 filled > 0 且 remaining == 0 时才判断为完全成交
                                            # 如果 filled == 0 且 remaining == 0，可能是订单刚创建时的初始状态，不应该判断为完全成交
                                            is_fully_filled = (
                                                (filled > 0 and remaining == 0) or 
                                                filled >= original_size or 
                                                (filled > 0 and filled >= current_size)
                                            )
                                            
                                            if is_fully_filled:
                                                # 完全成交：即使仍在活跃列表中，也要处理
                                                # 为了更可靠，验证交易历史确认成交份额
                                                actual_filled_size = self._query_order_filled_size(order_id)
                                                if actual_filled_size > 0:
                                                    verified_filled_size = actual_filled_size
                                                else:
                                                    # 如果查询失败，使用订单数据中的filled值
                                                    verified_filled_size = max(filled, original_size)
                                                
                                                logger.info(
                                                    f"检测到完全成交订单（仍在活跃列表中）: 订单ID={order_id[:20]}..., "
                                                    f"市场={market_id}, token={token_id[:20]}..., "
                                                    f"原始份额={original_size:.2f}, 已成交={filled:.2f}, "
                                                    f"剩余={remaining:.2f}, 验证成交份额={verified_filled_size:.2f}"
                                                )
                                                
                                                # 确保成交份额不超过原始份额
                                                final_filled_size = min(verified_filled_size, original_size)
                                                
                                                # 从活跃订单中移除
                                                del self.active_orders[market_id][token_id][side]
                                                
                                                # 如果是买单完全成交，收集信息用于后续对冲卖出
                                                if side == "BUY":
                                                    order_info["filled_size"] = final_filled_size
                                                    # 添加到完全成交订单列表（在锁外处理对冲卖出）
                                                    fully_filled_orders.append({
                                                        "market_id": market_id,
                                                        "token_id": token_id,
                                                        "order_info": order_info,
                                                        "filled_size": final_filled_size
                                                    })
                                                    # 在锁内更新 filled_buy_orders（不调用可能获取锁的方法）
                                                    if market_id not in self.filled_buy_orders:
                                                        self.filled_buy_orders[market_id] = []
                                                    self.filled_buy_orders[market_id].append(order_info)
                                                
                                                # 收集需要更新风险敞口的信息（在锁外执行，避免死锁）
                                                if side == "BUY":
                                                    risk_updates.append({
                                                        "type": "buy_filled",
                                                        "market_id": market_id,
                                                        "remove_exposure": order_info.get("exposure", 0),
                                                        "add_filled_exposure": {
                                                            "price": order_info.get("price", 0),
                                                            "size": final_filled_size
                                                        }
                                                    })
                                                else:
                                                    risk_updates.append({
                                                        "type": "sell_filled",
                                                        "market_id": market_id,
                                                        "token_id": token_id,
                                                        "remove_exposure": order_info.get("exposure", 0),
                                                        "filled_size": final_filled_size
                                                    })
                                                
                                                # 添加到 filled_orders 列表（用于返回结果）
                                                filled_order = {
                                                    "market_id": market_id,
                                                    "token_id": token_id,
                                                    "side": side,
                                                    "order_id": order_id,
                                                    "price": order_info.get("price"),
                                                    "size": order_info.get("size"),
                                                    "filled_size": final_filled_size,
                                                    "exposure": order_info.get("exposure"),
                                                    "status": "filled"
                                                }
                                                filled_orders.append(filled_order)
                                            
                                            # 检查是否部分成交（有成交但未完全成交）
                                            elif filled > 0 and remaining > 0:
                                                logger.warning(
                                                    f"检测到部分成交订单: 订单ID={order_id}, "
                                                    f"市场={market_id}, token={token_id[:20]}..., "
                                                    f"原始份额={original_size}, 已成交={filled}, 剩余={remaining}"
                                                )
                                                
                                                # 收集需要处理的部分成交订单信息（避免在持有锁时调用 cancel_order 和 place_order）
                                                partial_filled_orders.append({
                                                    "order_id": order_id,
                                                    "market_id": market_id,
                                                    "token_id": token_id,
                                                    "side": side,
                                                    "order_info": order_info,
                                                    "filled": filled,
                                                    "original_size": original_size
                                                })
                                            break
                                except Exception as e:
                                    logger.error(f"检查订单成交状态时发生错误: {e}")
                            
                            elif order_id not in open_order_ids:
                                # 订单不在活跃列表中，检查是否是成交
                                if order_id in filled_order_ids:
                                    # 订单已成交（可能是完全成交或部分成交）
                                    # 优先使用持仓检测到的成交份额，否则使用订单份额
                                    filled_size = filled_order_sizes.get(order_id)
                                    if not filled_size:
                                        # 如果持仓检测没有提供成交份额，查询交易历史获取
                                        filled_size = self._query_order_filled_size(order_id)
                                        if filled_size <= 0:
                                            # 如果查询失败，使用订单份额作为默认值
                                            filled_size = order_info.get("size", 0)
                                    
                                    # 确保 filled_size 是数字
                                    if not isinstance(filled_size, (int, float)):
                                        filled_size = order_info.get("size", 0)
                                    filled_size = float(filled_size)
                                    original_size = float(order_info.get("size", 0))
                                    
                                    # 判断是完全成交还是部分成交
                                    is_fully_filled = filled_size >= original_size
                                    
                                    if is_fully_filled:
                                        # 完全成交
                                        filled_order = {
                                            "market_id": market_id,
                                            "token_id": token_id,
                                            "side": side,
                                            "order_id": order_id,
                                            "price": order_info.get("price"),
                                            "size": order_info.get("size"),
                                            "filled_size": original_size,  # 完全成交，使用原始份额
                                            "exposure": order_info.get("exposure"),
                                            "status": "filled"
                                        }
                                        filled_orders.append(filled_order)
                                        
                                        # 从活跃订单中移除
                                        del self.active_orders[market_id][token_id][side]
                                        
                                        # 收集需要更新风险敞口的信息（在锁外执行，避免死锁）
                                        if side == "BUY":
                                            # 买单成交：移除挂单敞口，添加已成交订单敞口
                                            risk_updates.append({
                                                "type": "buy_filled",
                                                "market_id": market_id,
                                                "remove_exposure": order_info.get("exposure", 0),
                                                "add_filled_exposure": {
                                                    "price": order_info.get("price", 0),
                                                    "size": original_size
                                                }
                                            })
                                        else:
                                            # 卖单成交：移除挂单敞口，可能还需要移除已成交订单敞口
                                            risk_updates.append({
                                                "type": "sell_filled",
                                                "market_id": market_id,
                                                "token_id": token_id,
                                                "remove_exposure": order_info.get("exposure", 0),
                                                "filled_size": original_size  # 卖单成交份额
                                            })
                                        
                                        # 如果是买单成交，收集信息用于后续对冲卖出（避免在持有锁时调用 place_hedge_sell）
                                        if side == "BUY":
                                            # 更新订单信息，包含实际成交份额
                                            order_info["filled_size"] = original_size
                                            # 收集需要处理对冲卖出的订单信息
                                            fully_filled_orders.append({
                                                "market_id": market_id,
                                                "token_id": token_id,
                                                "order_info": order_info,
                                                "filled_size": original_size
                                            })
                                            # 在锁内更新 filled_buy_orders（不调用可能获取锁的方法）
                                            if market_id not in self.filled_buy_orders:
                                                self.filled_buy_orders[market_id] = []
                                            self.filled_buy_orders[market_id].append(order_info)
                                    else:
                                        # 部分成交：订单不在活跃列表，但实际成交份额 < 原始份额
                                        # 需要跟踪剩余部分，不能立即从 active_orders 中移除
                                        logger.warning(
                                            f"检测到部分成交但订单不在活跃列表: 订单ID={order_id[:20]}..., "
                                            f"市场={market_id}, token={token_id[:20]}..., "
                                            f"原始份额={original_size:.2f}, 已成交份额={filled_size:.2f}"
                                        )
                                        
                                        # 获取当前持仓（用于跟踪）
                                        current_position = 0.0
                                        try:
                                            positions = self.get_positions(size_threshold=0.1, limit=1000)
                                            for position in positions:
                                                if position.get("asset") == token_id:
                                                    current_position = float(position.get("size", 0))
                                                    break
                                        except Exception as e:
                                            logger.warning(f"获取持仓失败: {e}")
                                        
                                        # 添加到部分成交跟踪列表
                                        tracking_order_info = {
                                            "market_id": market_id,
                                            "token_id": token_id,
                                            "price": order_info.get("price"),
                                            "size": original_size
                                        }
                                        self._track_partial_filled_order(
                                            order_id,
                                            tracking_order_info,
                                            filled_size,
                                            current_position
                                        )
                                        
                                        # 如果是买单部分成交，需要将已成交部分的敞口从挂单敞口转为已成交订单敞口
                                        if side == "BUY":
                                            # 移除整个挂单的敞口
                                            self.risk_manager.remove_exposure(market_id, order_info.get("exposure", 0))
                                            # 添加已成交部分的敞口（占用资金，计入总敞口）
                                            self.risk_manager.add_filled_order_exposure(
                                                market_id,
                                                order_info.get("price", 0),
                                                filled_size
                                            )
                                            
                                            # 收集需要处理对冲卖出的订单信息（部分成交）
                                            order_info["filled_size"] = filled_size
                                            fully_filled_orders.append({
                                                "market_id": market_id,
                                                "token_id": token_id,
                                                "order_info": order_info,
                                                "filled_size": filled_size,
                                                "is_partial": True  # 标记为部分成交
                                            })
                                        
                                        # 从 active_orders 中移除（避免重复处理），但保留在跟踪列表中
                                        del self.active_orders[market_id][token_id][side]
                                        
                                        # 收集需要更新风险敞口的信息（在锁外执行，避免死锁）
                                        if side == "BUY":
                                            risk_updates.append({
                                                "type": "buy_partial_filled",
                                                "market_id": market_id,
                                                "remove_exposure": order_info.get("exposure", 0),
                                                "add_filled_exposure": {
                                                    "price": order_info.get("price", 0),
                                                    "size": filled_size
                                                }
                                            })
                                else:
                                    # 订单已取消（不在活跃列表且不在交易历史中）
                                    cancelled_count += 1
                                    logger.info(f"订单已取消: 订单ID={order_id}, 市场={market_id}")
                                    
                                    # 从活跃订单中移除
                                    del self.active_orders[market_id][token_id][side]
                                    
                                    # 收集需要移除敞口的信息（在锁外执行，避免死锁）
                                    risk_updates.append({
                                        "type": "cancelled",
                                        "market_id": market_id,
                                        "remove_exposure": order_info.get("exposure", 0)
                                    })
                    
                    # 清理空的市场和 token
                    if market_id in self.active_orders:
                        self.active_orders[market_id] = {
                            token_id: sides_dict
                            for token_id, sides_dict in self.active_orders[market_id].items()
                            if sides_dict
                        }
                        if not self.active_orders[market_id]:
                            del self.active_orders[market_id]
                    
                    if filled_orders:
                        filled_orders_by_market[market_id] = filled_orders
            
            # 第二阶段：释放锁后，更新风险敞口（避免死锁）
            # 处理在锁内收集的风险敞口更新
            for update in risk_updates:
                try:
                    if update["type"] == "buy_filled":
                        # 买单完全成交：移除挂单敞口，添加已成交订单敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                        self.risk_manager.add_filled_order_exposure(
                            update["market_id"],
                            update["add_filled_exposure"]["price"],
                            update["add_filled_exposure"]["size"]
                        )
                    elif update["type"] == "sell_filled":
                        # 卖单成交：移除挂单敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                        # 如果是对冲卖单成交，移除对应的已成交订单敞口
                        market_id = update["market_id"]
                        token_id = update.get("token_id")
                        sell_filled_size = update.get("filled_size", 0)
                        if token_id and market_id in self.filled_buy_orders:
                            with self.lock:
                                # 只读取数据，不调用可能获取锁的方法
                                filled_buy_orders_copy = list(self.filled_buy_orders[market_id])
                            for filled_buy in filled_buy_orders_copy:
                                if filled_buy.get("token_id") == token_id:
                                    buy_price = filled_buy.get("price", 0)
                                    buy_filled_size = filled_buy.get("filled_size", 0)
                                    # 移除已成交订单的敞口（对冲卖出后，不再占用资金）
                                    self.risk_manager.remove_filled_order_exposure(
                                        market_id,
                                        buy_price,
                                        min(sell_filled_size, buy_filled_size)  # 取较小值
                                    )
                                    logger.info(
                                        f"对冲卖单成交，移除已成交订单敞口: 市场={market_id}, "
                                        f"token={token_id[:20]}..., 份额={min(sell_filled_size, buy_filled_size)}"
                                    )
                                    break
                    elif update["type"] == "buy_partial_filled":
                        # 买单部分成交：移除挂单敞口，添加已成交部分敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                        self.risk_manager.add_filled_order_exposure(
                            update["market_id"],
                            update["add_filled_exposure"]["price"],
                            update["add_filled_exposure"]["size"]
                        )
                    elif update["type"] == "cancelled":
                        # 订单取消：移除挂单敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                except Exception as e:
                    logger.error(f"更新风险敞口时发生错误: {e}")
            
            # 第三阶段：释放锁后，处理完全成交和部分成交订单的对冲卖出（避免死锁）
            for filled_order_data in fully_filled_orders:
                market_id = filled_order_data["market_id"]
                token_id = filled_order_data["token_id"]
                order_info = filled_order_data["order_info"]
                filled_size = filled_order_data["filled_size"]
                is_partial = filled_order_data.get("is_partial", False)
                
                # 立即对冲卖出（使用实际成交份额）
                try:
                    hedge_result = self.place_hedge_sell(
                        market_id=market_id,
                        token_id=token_id,
                        buy_price=order_info.get("price"),
                        filled_size=filled_size
                    )
                    if hedge_result:
                        if is_partial:
                            logger.info(
                                f"部分成交买单对冲卖出成功: 市场={market_id}, token={token_id[:20]}..., "
                                f"已成交份额={filled_size:.2f}"
                            )
                            # 更新跟踪信息中的已对冲份额
                            order_id = order_info.get("order_id")
                            if order_id and order_id in self.partial_filled_tracking:
                                with self.lock:
                                    tracking_info = self.partial_filled_tracking[order_id]
                                    tracking_info["hedged_size"] = tracking_info.get("hedged_size", 0.0) + filled_size
                        else:
                            logger.info(f"买单成交后立即对冲卖出成功: 市场={market_id}, token={token_id[:20]}..., 份额={filled_size}")
                    else:
                        if is_partial:
                            logger.warning(f"部分成交买单对冲卖出失败: 市场={market_id}, token={token_id[:20]}...")
                        else:
                            logger.warning(f"买单成交后对冲卖出失败: 市场={market_id}, token={token_id[:20]}...")
                except Exception as e:
                    logger.error(f"立即对冲卖出时发生错误: {e}")
            
            # 第二阶段：释放锁后，处理部分成交订单（避免死锁）
            for partial_order in partial_filled_orders:
                order_id = partial_order["order_id"]
                market_id = partial_order["market_id"]
                token_id = partial_order["token_id"]
                side = partial_order["side"]
                order_info = partial_order["order_info"]
                filled = partial_order["filled"]
                original_size = partial_order["original_size"]
                
                # 取消剩余部分（取消整个订单）
                if self.cancel_order(order_id):
                    logger.info(f"已取消部分成交订单的剩余部分: 订单ID={order_id}")
                    
                    # 在锁内更新活跃订单（风险敞口更新在锁外执行，避免死锁）
                    with self.lock:
                        # 从活跃订单中移除
                        if market_id in self.active_orders and token_id in self.active_orders[market_id] and side in self.active_orders[market_id][token_id]:
                            del self.active_orders[market_id][token_id][side]
                    
                    # 在锁外更新风险敞口（避免死锁）
                    if side == "BUY" and filled > 0:
                        # 买单部分成交：移除整个挂单的敞口，添加已成交部分的敞口
                        self.risk_manager.remove_exposure(market_id, order_info.get("exposure", 0))
                        self.risk_manager.add_filled_order_exposure(
                            market_id,
                            order_info.get("price", 0),
                            filled
                        )
                    else:
                        # 非买单或未成交，移除挂单敞口
                        self.risk_manager.remove_exposure(market_id, order_info.get("exposure", 0))
                    
                    # 如果是买单部分成交，需要处理已成交部分（在锁外执行，避免死锁）
                    if side == "BUY" and filled > 0:
                        # 记录已成交的买单，用于对冲卖出
                        with self.lock:
                            filled_order_info = {
                                "market_id": market_id,
                                "token_id": token_id,
                                "side": side,
                                "order_id": order_id,
                                "price": order_info.get("price"),
                                "size": original_size,
                                "filled_size": filled,
                                "exposure": order_info.get("exposure", 0),
                                "status": "partially_filled"
                            }
                            if market_id not in self.filled_buy_orders:
                                self.filled_buy_orders[market_id] = []
                            self.filled_buy_orders[market_id].append(filled_order_info)
                        
                        # 立即对冲卖出（使用已成交份额）
                        try:
                            hedge_result = self.place_hedge_sell(
                                market_id=market_id,
                                token_id=token_id,
                                buy_price=order_info.get("price"),
                                filled_size=filled
                            )
                            if hedge_result:
                                logger.info(f"部分成交买单对冲卖出成功: 市场={market_id}, token={token_id[:20]}..., 已成交份额={filled}")
                            else:
                                logger.warning(f"部分成交买单对冲卖出失败: 市场={market_id}, token={token_id[:20]}...")
                        except Exception as e:
                            logger.error(f"部分成交买单对冲卖出时发生错误: {e}")
                    
                    # 重新提交完整订单（满足最小份额要求）
                    try:
                        market = self.market_data_cache.get(market_id)
                        if market:
                            # 获取订单簿数据
                            orderbook = self._get_orderbook(token_id)
                            if orderbook:
                                rewards_max_spread = market.get("rewards_max_spread", 0)
                                if rewards_max_spread > 0:
                                    # 计算新的订单价格和份额
                                    prices = self.strategy.calculate_order_prices(orderbook, rewards_max_spread, market=market)
                                    if prices:
                                        order_size = self.strategy.calculate_order_size(market)
                                        
                                        # 重新提交订单
                                        buy_price = prices.get("buy_price")
                                        sell_price = prices.get("sell_price")
                                        target_price = buy_price if side == "BUY" else prices.get("sell_price")
                                        
                                        if target_price:
                                            # 如果是买单，检查是否可以安全挂单（风险管理）
                                            if side == "BUY":
                                                if buy_price and sell_price:
                                                    # 计算实际挂单价格（买二价或奖励下边界）
                                                    actual_buy_price = self.strategy.calculate_actual_buy_price(orderbook, buy_price)
                                                    
                                                    # 如果没有实际挂单价格（订单簿只有买一价），跳过
                                                    if actual_buy_price is None:
                                                        logger.warning(
                                                            f"部分成交订单重新提交失败: 订单簿只有买一价，跳过重新提交"
                                                        )
                                                        continue
                                                    
                                                    can_place, safety_info = self.strategy.can_place_buy_order_safely(
                                                        orderbook, buy_price, sell_price, order_size, actual_buy_price
                                                    )
                                                    
                                                    if not can_place:
                                                        logger.warning(
                                                            f"部分成交订单重新提交失败（风险管理）: {safety_info['reason']}，"
                                                            f"跳过重新提交以避免风险"
                                                        )
                                                        continue
                                                    else:
                                                        logger.info(f"部分成交订单重新提交检查通过: {safety_info['reason']}")
                                                    
                                                    # 使用实际挂单价格
                                                    target_price = actual_buy_price
                                            
                                            # 买单不进行规范化，直接使用 target_price（原始买二价或奖励下边界）
                                            # 卖单需要进行规范化
                                            if side == "SELL":
                                                target_price = self.strategy.normalize_price(
                                                    target_price, 
                                                    self.strategy.get_order_price_min_tick_size(market)
                                                )
                                            new_order = self.place_order(
                                                market_id=market_id,
                                                token_id=token_id,
                                                side=side,
                                                price=target_price,
                                                size=order_size
                                            )
                                            if new_order:
                                                logger.info(
                                                    f"部分成交订单已重新提交: 市场={market_id}, "
                                                    f"token={token_id[:20]}..., 新订单份额={order_size}"
                                                )
                                    else:
                                        logger.warning(f"无法计算订单价格，跳过重新提交: 市场={market_id}, token={token_id[:20]}...")
                                else:
                                    logger.warning(f"市场无奖励配置，跳过重新提交: 市场={market_id}")
                            else:
                                logger.warning(f"无法获取订单簿，跳过重新提交: 市场={market_id}, token={token_id[:20]}...")
                        else:
                            logger.warning(f"无法获取市场数据，跳过重新提交: 市场={market_id}")
                    except Exception as e:
                        logger.error(f"重新提交部分成交订单时发生错误: {e}")
                else:
                    logger.error(f"取消部分成交订单失败: 订单ID={order_id}")
            
            if filled_orders_by_market:
                total_filled = sum(len(orders) for orders in filled_orders_by_market.values())
                logger.info(f"检测到 {total_filled} 个订单已成交，{cancelled_count} 个订单已取消")
            
        except Exception as e:
            # 使用 try-except 包裹日志记录，防止日志写入失败导致程序卡死
            try:
                error_msg = str(e)
                # 检查是否是 API 认证错误
                if "401" in error_msg or "Unauthorized" in error_msg or "Invalid api key" in error_msg:
                    logger.error(f"检查订单状态时发生 API 认证错误: {e}，请检查 API 密钥是否有效")
                else:
                    logger.error(f"检查订单状态失败: {e}")
                import traceback
                traceback.print_exc()
            except Exception as log_error:
                # 如果日志写入也失败，至少尝试输出到 stderr
                try:
                    import sys
                    print(f"[错误] 检查订单状态失败: {e}", file=sys.stderr)
                    if log_error:
                        print(f"[错误] 日志写入也失败: {log_error}", file=sys.stderr)
                except:
                    pass  # 如果连 stderr 都无法写入，静默忽略
        
        return filled_orders_by_market
    
        """
        检查订单状态，检测成交并清理已取消/成交的订单记录
        
        简化版本：主要功能是清理订单记录和更新风险敞口
        对冲卖出逻辑已由 check_positions_and_hedge() 处理，不再在此处处理
        
        Returns:
            字典 {market_id: [filled_orders]}，包含已成交的订单信息（用于补单逻辑）
        """
        filled_orders_by_market = {}
        cancelled_count = 0
        
        try:
            # 获取所有活跃订单
            open_orders = self.clob_client.get_open_orders(OpenOrderParams())
            open_order_ids = {order.get("id") for order in open_orders if order.get("id")}
            
            # 收集所有不在活跃列表中的订单ID
            missing_order_ids = []
            missing_order_info = {}  # order_id -> order_info
            
            with self.lock:
                for market_id, tokens_dict in list(self.active_orders.items()):
                    for token_id, sides_dict in list(tokens_dict.items()):
                        for side, order_info in list(sides_dict.items()):
                            order_id = order_info.get("order_id")
                            if order_id and order_id not in open_order_ids:
                                missing_order_ids.append(order_id)
                                missing_order_info[order_id] = {
                                    "market_id": market_id,
                                    "token_id": token_id,
                                    "side": side,
                                    "order_info": order_info
                                }
            
            # 检测不在活跃列表中的订单是否成交
            filled_order_ids = set()
            filled_order_sizes = {}  # order_id -> filled_size
            
            if missing_order_ids:
                # 使用交易历史查询检测订单成交
                try:
                    logger.info(f"查询交易历史以检测 {len(missing_order_ids)} 个不在活跃列表中的订单是否成交...")
                    
                    for order_id in missing_order_ids:
                        order_info = missing_order_info.get(order_id)
                        if not order_info:
                            continue
                        
                        side = order_info.get("side")
                        # 只处理买单（卖单成交由 check_positions_and_hedge() 处理）
                        if side != "BUY":
                            continue
                        
                        # 查询该订单的实际成交份额
                        actual_filled_size = self._query_order_filled_size(order_id)
                        
                        if actual_filled_size > 0:
                            filled_order_ids.add(order_id)
                            filled_order_sizes[order_id] = actual_filled_size
                            
                            original_size = order_info.get("order_info", {}).get("size", 0)
                            if actual_filled_size >= original_size:
                                logger.info(
                                    f"检测到订单完全成交: 订单ID={order_id[:20]}..., "
                                    f"订单份额={original_size:.2f}, 实际成交份额={actual_filled_size:.2f}"
                                )
                            else:
                                logger.info(
                                    f"检测到订单部分成交: 订单ID={order_id[:20]}..., "
                                    f"订单份额={original_size:.2f}, 实际成交份额={actual_filled_size:.2f}"
                                )
                    
                    logger.info(f"交易历史查询: 找到 {len(filled_order_ids)} 个已成交订单")
                except Exception as e:
                    logger.warning(f"查询交易历史失败: {e}")
            
            # 收集需要处理的订单信息和风险敞口更新
            risk_updates = []  # 风险敞口更新列表（在锁外执行，避免死锁）
            
            with self.lock:
                for market_id, tokens_dict in list(self.active_orders.items()):
                    filled_orders = []
                    
                    for token_id, sides_dict in list(tokens_dict.items()):
                        for side, order_info in list(sides_dict.items()):
                            order_id = order_info.get("order_id")
                            original_size = order_info.get("size", 0)
                            
                            # 检查订单是否仍在活跃列表中
                            if order_id in open_order_ids:
                                # 订单仍在活跃列表中，检查是否完全成交
                                try:
                                    # 从活跃订单列表中查找该订单的详细信息
                                    for open_order in open_orders:
                                        if open_order.get("id") == order_id:
                                            # 获取订单的已成交份额和剩余份额
                                            filled = float(open_order.get("filled", 0))
                                            remaining = float(open_order.get("remaining", 0))
                                            current_size = float(open_order.get("size", original_size))
                                            
                                            # 检查是否完全成交（即使仍在活跃列表中，也可能是API延迟）
                                            is_fully_filled = (
                                                (filled > 0 and remaining == 0) or 
                                                filled >= original_size or 
                                                (filled > 0 and filled >= current_size)
                                            )
                                            
                                            if is_fully_filled:
                                                # 完全成交：验证交易历史确认成交份额
                                                actual_filled_size = self._query_order_filled_size(order_id)
                                                if actual_filled_size > 0:
                                                    verified_filled_size = actual_filled_size
                                                else:
                                                    verified_filled_size = max(filled, original_size)
                                                
                                                logger.info(
                                                    f"检测到完全成交订单（仍在活跃列表中）: 订单ID={order_id[:20]}..., "
                                                    f"市场={market_id}, token={token_id[:20]}..., "
                                                    f"原始份额={original_size:.2f}, 验证成交份额={verified_filled_size:.2f}"
                                                )
                                                
                                                # 确保成交份额不超过原始份额
                                                final_filled_size = min(verified_filled_size, original_size)
                                                
                                                # 从活跃订单中移除
                                                del self.active_orders[market_id][token_id][side]
                                                
                                                # 如果是买单完全成交，记录到 filled_buy_orders（用于风险敞口管理）
                                                if side == "BUY":
                                                    order_info["filled_size"] = final_filled_size
                                                    if market_id not in self.filled_buy_orders:
                                                        self.filled_buy_orders[market_id] = []
                                                    self.filled_buy_orders[market_id].append(order_info)
                                                
                                                # 收集需要更新风险敞口的信息（在锁外执行，避免死锁）
                                                if side == "BUY":
                                                    risk_updates.append({
                                                        "type": "buy_filled",
                                                        "market_id": market_id,
                                                        "remove_exposure": order_info.get("exposure", 0),
                                                        "add_filled_exposure": {
                                                            "price": order_info.get("price", 0),
                                                            "size": final_filled_size
                                                        }
                                                    })
                                                else:
                                                    risk_updates.append({
                                                        "type": "sell_filled",
                                                        "market_id": market_id,
                                                        "token_id": token_id,
                                                        "remove_exposure": order_info.get("exposure", 0),
                                                        "filled_size": final_filled_size
                                                    })
                                                
                                                # 添加到 filled_orders 列表（用于返回结果，供补单逻辑使用）
                                                filled_order = {
                                                    "market_id": market_id,
                                                    "token_id": token_id,
                                                    "side": side,
                                                    "order_id": order_id,
                                                    "price": order_info.get("price"),
                                                    "size": order_info.get("size"),
                                                    "filled_size": final_filled_size,
                                                    "exposure": order_info.get("exposure"),
                                                    "status": "filled"
                                                }
                                                filled_orders.append(filled_order)
                                            break
                                except Exception as e:
                                    logger.error(f"检查订单成交状态时发生错误: {e}")
                            
                            elif order_id not in open_order_ids:
                                # 订单不在活跃列表中，检查是否是成交
                                if order_id in filled_order_ids:
                                    # 订单已成交
                                    filled_size = filled_order_sizes.get(order_id)
                                    if not filled_size:
                                        filled_size = self._query_order_filled_size(order_id)
                                        if filled_size <= 0:
                                            filled_size = order_info.get("size", 0)
                                    
                                    filled_size = float(filled_size)
                                    original_size = float(order_info.get("size", 0))
                                    
                                    # 判断是完全成交还是部分成交
                                    is_fully_filled = filled_size >= original_size
                                    final_filled_size = original_size if is_fully_filled else filled_size
                                    
                                    if is_fully_filled:
                                        logger.info(
                                            f"检测到订单完全成交: 订单ID={order_id[:20]}..., "
                                            f"市场={market_id}, token={token_id[:20]}..., "
                                            f"原始份额={original_size:.2f}, 成交份额={final_filled_size:.2f}"
                                        )
                                    else:
                                        logger.info(
                                            f"检测到订单部分成交: 订单ID={order_id[:20]}..., "
                                            f"市场={market_id}, token={token_id[:20]}..., "
                                            f"原始份额={original_size:.2f}, 成交份额={final_filled_size:.2f}"
                                        )
                                    
                                    # 从活跃订单中移除
                                    del self.active_orders[market_id][token_id][side]
                                    
                                    # 如果是买单成交，记录到 filled_buy_orders（用于风险敞口管理）
                                    if side == "BUY":
                                        order_info["filled_size"] = final_filled_size
                                        if market_id not in self.filled_buy_orders:
                                            self.filled_buy_orders[market_id] = []
                                        self.filled_buy_orders[market_id].append(order_info)
                                    
                                    # 收集需要更新风险敞口的信息（在锁外执行，避免死锁）
                                    if side == "BUY":
                                        risk_updates.append({
                                            "type": "buy_filled" if is_fully_filled else "buy_partial_filled",
                                            "market_id": market_id,
                                            "remove_exposure": order_info.get("exposure", 0),
                                            "add_filled_exposure": {
                                                "price": order_info.get("price", 0),
                                                "size": final_filled_size
                                            }
                                        })
                                    else:
                                        risk_updates.append({
                                            "type": "sell_filled",
                                            "market_id": market_id,
                                            "token_id": token_id,
                                            "remove_exposure": order_info.get("exposure", 0),
                                            "filled_size": final_filled_size
                                        })
                                    
                                    # 添加到 filled_orders 列表（用于返回结果，供补单逻辑使用）
                                    filled_order = {
                                        "market_id": market_id,
                                        "token_id": token_id,
                                        "side": side,
                                        "order_id": order_id,
                                        "price": order_info.get("price"),
                                        "size": order_info.get("size"),
                                        "filled_size": final_filled_size,
                                        "exposure": order_info.get("exposure"),
                                        "status": "filled" if is_fully_filled else "partially_filled"
                                    }
                                    filled_orders.append(filled_order)
                                else:
                                    # 订单已取消（不在活跃列表且不在交易历史中）
                                    cancelled_count += 1
                                    logger.info(f"订单已取消: 订单ID={order_id[:20]}..., 市场={market_id}")
                                    
                                    # 从活跃订单中移除
                                    del self.active_orders[market_id][token_id][side]
                                    
                                    # 收集需要移除敞口的信息（在锁外执行，避免死锁）
                                    risk_updates.append({
                                        "type": "cancelled",
                                        "market_id": market_id,
                                        "remove_exposure": order_info.get("exposure", 0)
                                    })
                    
                    # 清理空的市场和 token
                    if market_id in self.active_orders:
                        self.active_orders[market_id] = {
                            token_id: sides_dict
                            for token_id, sides_dict in self.active_orders[market_id].items()
                            if sides_dict
                        }
                        if not self.active_orders[market_id]:
                            del self.active_orders[market_id]
                    
                    if filled_orders:
                        filled_orders_by_market[market_id] = filled_orders
            
            # 第二阶段：释放锁后，更新风险敞口（避免死锁）
            # 处理在锁内收集的风险敞口更新
            for update in risk_updates:
                try:
                    if update["type"] == "buy_filled" or update["type"] == "buy_partial_filled":
                        # 买单成交（完全或部分）：移除挂单敞口，添加已成交订单敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                        self.risk_manager.add_filled_order_exposure(
                            update["market_id"],
                            update["add_filled_exposure"]["price"],
                            update["add_filled_exposure"]["size"]
                        )
                    elif update["type"] == "sell_filled":
                        # 卖单成交：移除挂单敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                        # 如果是对冲卖单成交，移除对应的已成交订单敞口
                        market_id = update["market_id"]
                        token_id = update.get("token_id")
                        sell_filled_size = update.get("filled_size", 0)
                        if token_id and market_id in self.filled_buy_orders:
                            with self.lock:
                                # 只读取数据，不调用可能获取锁的方法
                                filled_buy_orders_copy = list(self.filled_buy_orders[market_id])
                            for filled_buy in filled_buy_orders_copy:
                                if filled_buy.get("token_id") == token_id:
                                    buy_price = filled_buy.get("price", 0)
                                    buy_filled_size = filled_buy.get("filled_size", 0)
                                    # 移除已成交订单的敞口（对冲卖出后，不再占用资金）
                                    self.risk_manager.remove_filled_order_exposure(
                                        market_id,
                                        buy_price,
                                        min(sell_filled_size, buy_filled_size)  # 取较小值
                                    )
                                    logger.info(
                                        f"对冲卖单成交，移除已成交订单敞口: 市场={market_id}, "
                                        f"token={token_id[:20]}..., 份额={min(sell_filled_size, buy_filled_size)}"
                                    )
                                    break
                    elif update["type"] == "cancelled":
                        # 订单取消：移除挂单敞口
                        self.risk_manager.remove_exposure(update["market_id"], update["remove_exposure"])
                except Exception as e:
                    logger.error(f"更新风险敞口时发生错误: {e}")
            
            # 注意：对冲卖出逻辑已由 check_positions_and_hedge() 方法处理，不再在此处处理
            # 补单逻辑由主循环根据返回的 filled_orders_by_market 处理
            
            if filled_orders_by_market:
                total_filled = sum(len(orders) for orders in filled_orders_by_market.values())
                logger.info(f"检测到 {total_filled} 个订单已成交，{cancelled_count} 个订单已取消")
            
            # 检查需要双边挂单的市场：如果只有一边有活跃订单，取消这些订单
            self._check_and_cancel_single_side_orders()
            
        except Exception as e:
            logger.error(f"检查订单状态失败: {e}")
        
        return filled_orders_by_market
    
    def _check_and_cancel_single_side_orders(self) -> None:
        """
        检查需要双边挂单的市场，如果只有一边有活跃订单，取消这些订单
        
        对于中间价 <= 0.10 的市场，必须同时在 Yes 和 No 上挂单才能获得奖励。
        如果只有一边有活跃订单，单边挂单既无法获得奖励又可能因市场波动造成损失。
        """
        try:
            with self.lock:
                # 复制 active_orders 以避免在迭代时修改
                active_orders_copy = {
                    market_id: {
                        token_id: sides_dict.copy()
                        for token_id, sides_dict in tokens_dict.items()
                    }
                    for market_id, tokens_dict in self.active_orders.items()
                }
            
            # 遍历所有有活跃订单的市场
            for market_id, tokens_dict in active_orders_copy.items():
                # 获取市场数据
                market = self.market_data_cache.get(market_id)
                if not market:
                    continue
                
                tokens = market.get("tokens", [])
                if len(tokens) < 2:
                    # 如果市场少于2个token，不需要检查双边挂单
                    continue
                
                # 检查是否需要双边挂单：计算所有token的中间价
                requires_both_tokens = False
                token_mid_prices = {}  # {token_id: mid_price}
                
                for token in tokens:
                    token_id = token.get("token_id")
                    outcome = token.get("outcome", "N/A")
                    
                    if not token_id:
                        continue
                    
                    # 获取订单簿数据用于计算中间价
                    orderbook = self._get_orderbook(token_id)
                    if orderbook:
                        mid_price = self.strategy.calculate_mid_price(orderbook)
                        if mid_price is not None:
                            token_mid_prices[token_id] = mid_price
                            # 如果任何 token 的中间价 <= 0.10，需要双边挂单
                            if mid_price <= 0.10:
                                requires_both_tokens = True
                
                # 如果不需要双边挂单，跳过
                if not requires_both_tokens:
                    continue
                
                # 检查该市场的所有token是否都有活跃的买单
                active_buy_tokens = set()  # 有活跃买单的token_id集合
                
                for token_id, sides_dict in tokens_dict.items():
                    if "BUY" in sides_dict:
                        active_buy_tokens.add(token_id)
                
                # 获取该市场的所有token_id
                all_token_ids = {token.get("token_id") for token in tokens if token.get("token_id")}
                
                # 如果只有部分token有活跃订单，需要取消这些订单
                if len(active_buy_tokens) > 0 and len(active_buy_tokens) < len(all_token_ids):
                    logger.warning(
                        f"⚠️  检测到需要双边挂单的市场 {market_id} 只有单边有活跃订单: "
                        f"{len(active_buy_tokens)}/{len(all_token_ids)} 个token有活跃订单，"
                        f"将取消所有活跃订单以避免损失"
                    )
                    
                    # 取消所有活跃的买单
                    # 第一阶段：在持有锁的情况下，收集需要取消的订单ID（避免死锁）
                    orders_to_cancel = []  # [(token_id, order_id, outcome)]
                    
                    with self.lock:
                        for token_id in active_buy_tokens:
                            if market_id in self.active_orders:
                                if token_id in self.active_orders[market_id]:
                                    if "BUY" in self.active_orders[market_id][token_id]:
                                        order_info = self.active_orders[market_id][token_id]["BUY"]
                                        order_id = order_info.get("order_id")
                                        
                                        if order_id:
                                            # 查找对应的 outcome 用于日志
                                            outcome = "N/A"
                                            for token in tokens:
                                                if token.get("token_id") == token_id:
                                                    outcome = token.get("outcome", "N/A")
                                                    break
                                            
                                            orders_to_cancel.append((token_id, order_id, outcome))
                    
                    # 第二阶段：释放锁后，执行取消操作（避免死锁）
                    # cancel_order 方法内部会获取锁并更新 active_orders 和风险敞口
                    cancelled_count = 0
                    for token_id, order_id, outcome in orders_to_cancel:
                        logger.info(
                            f"  正在取消单边订单: token={outcome}, "
                            f"token_id={token_id[:30]}..., order_id={order_id}"
                        )
                        
                        if self.cancel_order(order_id):
                            cancelled_count += 1
                            logger.info(
                                f"  ✓ 订单已取消: token={outcome}, order_id={order_id}"
                            )
                        else:
                            logger.warning(
                                f"  ✗ 订单取消失败: token={outcome}, order_id={order_id}"
                            )
                    
                    logger.warning(
                        f"  已取消 {cancelled_count}/{len(active_buy_tokens)} 个单边订单。"
                        f"原因：需要双边挂单但只有单边有订单，单边挂单既无法获得奖励又可能因市场波动造成损失"
                    )
        
        except Exception as e:
            logger.error(f"检查并取消单边订单时发生错误: {e}")
    
    def _get_market_id_from_token_id(self, token_id: str) -> Optional[str]:
        """
        通过 token_id 获取 market_id
        
        优先从 active_orders 中查找，如果找不到则从 filled_buy_orders、market_data_cache 和 Redis 中查找
        
        Args:
            token_id: Token ID
            
        Returns:
            market_id，如果找不到返回 None
        """
        # 方法1：从 active_orders 中查找（最快，因为我们挂过单的 token 肯定在这里）
        with self.lock:
            for market_id, tokens_dict in self.active_orders.items():
                if token_id in tokens_dict:
                    return market_id
            
            # 方法2：从 filled_buy_orders 中查找（已成交的订单也可能有持仓）
            # 注意：filled_buy_orders 也在锁保护下，所以在这里读取是安全的
            filled_buy_orders_copy = {}
            for market_id, filled_orders in self.filled_buy_orders.items():
                filled_buy_orders_copy[market_id] = list(filled_orders)  # 复制列表避免在锁外修改
        
        # 在锁外继续查找（避免在锁内进行可能耗时的操作）
        for market_id, filled_orders in filled_buy_orders_copy.items():
            for filled_order in filled_orders:
                if filled_order.get("token_id") == token_id:
                    return market_id
        
        # 方法3：从 market_data_cache 中查找
        for market_id, market_data in self.market_data_cache.items():
            tokens = market_data.get("tokens", [])
            for token in tokens:
                if token.get("token_id") == token_id:
                    return market_id
        
        # 方法4：从 Redis 中查找（如果启用）
        try:
            from redis_orderbook_client import RedisOrderbookClient
            from config import config
            
            storage_config = config.orderbook_service.get("storage", {})
            redis_client = RedisOrderbookClient(
                orderbook_ttl=storage_config.get("orderbook_ttl", 300),
                db_path=storage_config.get("db_path"),
            )
            
            try:
                # 从 markets:list 中查找
                markets = redis_client.get_markets()
                for market in markets:
                    market_id = market.get("market_id")
                    tokens = market.get("tokens", [])
                    for token in tokens:
                        if token.get("token_id") == token_id:
                            return market_id
            finally:
                redis_client.close()
        except Exception:
            pass  # Redis 查询失败，忽略
        
        return None
    
    def check_positions_and_hedge(self) -> Dict[str, bool]:
        """
        检查持仓并挂出对冲卖单
        
        简化逻辑：直接根据持仓数据挂出卖单，不需要复杂的订单成交检测
        如果持仓份额与已挂卖单份额不一致，取消旧的卖出订单并重新挂出全仓卖出订单
        
        Returns:
            字典 {token_id: success}，记录每个token的对冲卖出结果
        """
        results = {}
        
        try:
            # 1. 获取所有持仓
            positions = self.get_positions(size_threshold=0.1, limit=1000)
            if not positions:
                logger.debug("没有持仓，跳过对冲卖出检查")
                return results
            
            logger.info(f"开始检查持仓对冲: 共 {len(positions)} 个持仓")
            
            # 2. 获取当前活跃订单（用于检查是否已有对冲卖单）
            open_orders = self.clob_client.get_open_orders(OpenOrderParams())
            open_order_ids = {order.get("id") for order in open_orders if order.get("id")}
            
            # 3. 对于每个持仓，检查是否需要挂出对冲卖单
            for position in positions:
                token_id = position.get("asset")
                position_size = float(position.get("size", 0))
                avg_price = float(position.get("avgPrice", 0))
                
                if not token_id or position_size <= 0:
                    continue
                
                # 通过 token_id 获取 market_id（持仓数据中没有 market_id，只有 conditionId）
                market_id = self._get_market_id_from_token_id(token_id)
                
                # 如果无法获取 market_id，跳过并记录警告
                if not market_id:
                    logger.warning(
                        f"无法通过 token_id 获取 market_id，跳过对冲卖出: "
                        f"token={token_id[:20]}..., 持仓份额={position_size:.2f}, "
                        f"conditionId={position.get('conditionId', 'N/A')[:30]}..."
                    )
                    continue
                
                # 检查是否已有对冲卖单
                existing_sell_order_id = None
                existing_sell_size = 0.0
                exposure_to_remove = 0.0  # 需要移除的敞口（在锁外处理）
                
                with self.lock:
                    if market_id in self.active_orders:
                        if token_id in self.active_orders[market_id]:
                            sell_order_info = self.active_orders[market_id][token_id].get("SELL")
                            if sell_order_info:
                                existing_sell_order_id = sell_order_info.get("order_id")
                                # 检查订单是否仍在活跃列表中
                                if existing_sell_order_id and existing_sell_order_id in open_order_ids:
                                    existing_sell_size = float(sell_order_info.get("size", 0))
                                else:
                                    # 订单不在活跃列表中，说明已取消或成交，清理 active_orders
                                    if existing_sell_order_id:
                                        logger.debug(
                                            f"发现已取消/成交的卖单，清理 active_orders: "
                                            f"token={token_id[:20]}..., order_id={existing_sell_order_id[:20]}..."
                                        )
                                    # 记录需要移除的敞口（在锁外处理，避免死锁）
                                    exposure_to_remove = sell_order_info.get("exposure", 0)
                                    # 删除SELL订单记录
                                    if "SELL" in self.active_orders[market_id][token_id]:
                                        del self.active_orders[market_id][token_id]["SELL"]
                                    # 清理空结构
                                    if not self.active_orders[market_id][token_id]:
                                        del self.active_orders[market_id][token_id]
                                    if market_id in self.active_orders and not self.active_orders[market_id]:
                                        del self.active_orders[market_id]
                
                # 在锁外移除敞口（避免死锁）
                if exposure_to_remove > 0:
                    self.risk_manager.remove_exposure(market_id, exposure_to_remove)
                
                # 持仓变化处理：如果持仓份额 != 已挂卖单份额，取消旧的并重新挂单
                if abs(position_size - existing_sell_size) > 0.01:  # 允许0.01的浮点数误差
                    # 检查是否在冷却期内（避免频繁重试失败的挂单）
                    current_time = time.time()
                    cooldown_period = 30.0  # 冷却期30秒
                    with self.lock:
                        last_failure_time = self.hedge_sell_failures.get(token_id, 0)
                        if current_time - last_failure_time < cooldown_period:
                            remaining_cooldown = cooldown_period - (current_time - last_failure_time)
                            logger.debug(
                                f"持仓对冲卖出跳过（冷却期）: token={token_id[:20]}..., "
                                f"上次失败时间={time.strftime('%H:%M:%S', time.localtime(last_failure_time))}, "
                                f"剩余冷却时间={remaining_cooldown:.1f}秒"
                            )
                            results[token_id] = False
                            continue
                    
                    # 如果已有卖单且份额不一致，先取消旧订单
                    if existing_sell_order_id and existing_sell_order_id in open_order_ids:
                        logger.info(
                            f"持仓变化，取消旧的卖出订单: token={token_id[:20]}..., "
                            f"持仓份额={position_size:.2f}, 已挂卖单份额={existing_sell_size:.2f}, "
                            f"订单ID={existing_sell_order_id[:20]}..."
                        )
                        if self.cancel_order(existing_sell_order_id):
                            logger.info(f"成功取消旧的卖出订单: token={token_id[:20]}...")
                        else:
                            logger.warning(f"取消旧的卖出订单失败: token={token_id[:20]}...")
                    
                    # 计算对冲卖出价格（使用持仓的avgPrice作为买入价）
                    market = self.market_data_cache.get(market_id)
                    
                    # 获取订单簿以计算买一价
                    best_bid_price = None
                    orderbook = self._get_orderbook(token_id)
                    if orderbook:
                        best_bid_price = normalize_orderbook(orderbook).best_bid
                    
                    max_bid_gap = config.hedge_sell_max_bid_gap
                    
                    # 计算对冲卖出价格（在允许范围内优先使用买一价）
                    sell_price = self.strategy.calculate_hedge_sell_price(
                        avg_price,
                        market=market,
                        best_bid_price=best_bid_price,
                        max_bid_gap=max_bid_gap
                    )
                    
                    # 记录价格策略信息
                    if best_bid_price is not None and abs(avg_price - best_bid_price) <= max_bid_gap:
                        logger.info(
                            f"持仓对冲卖出价格策略: 使用买一价 {best_bid_price:.4f} (与买入价差 {abs(avg_price - best_bid_price):.4f} ≤ 阈值 {max_bid_gap:.4f})"
                        )
                    elif best_bid_price is not None:
                        logger.info(
                            f"持仓对冲卖出价格策略: 买一价 {best_bid_price:.4f} 与买入价差 {abs(avg_price - best_bid_price):.4f} 超过阈值 {max_bid_gap:.4f}，回退到原价逻辑"
                        )
                    else:
                        logger.info("持仓对冲卖出价格策略: 无法获取买一价，使用原价逻辑计算卖出价格")
                    
                    # 挂出全仓卖出订单
                    sell_result = self.place_order(
                        market_id=market_id,
                        token_id=token_id,
                        side="SELL",
                        price=sell_price,
                        size=position_size  # 使用全部持仓份额
                    )
                    
                    if sell_result:
                        # 订单提交成功，清除失败记录
                        with self.lock:
                            if token_id in self.hedge_sell_failures:
                                del self.hedge_sell_failures[token_id]
                        
                        # 订单提交成功
                        order_id = sell_result.get("id") or sell_result.get("orderID")
                        order_status = sell_result.get("status", "unknown")
                        
                        logger.info(
                            f"持仓对冲卖出成功: token={token_id[:20]}..., "
                            f"持仓份额={position_size:.2f}, 买入价={avg_price:.4f}, 卖出价={sell_price:.4f}, "
                            f"订单ID={order_id[:20] if order_id else 'N/A'}..., 状态={order_status}"
                        )
                        results[token_id] = True
                    else:
                        # place_order 返回 None，可能是验证失败
                        # 但对于卖单，可能是立即成交了（matched），所以不在活跃列表中
                        # 只检查一次持仓变化（避免长时间阻塞主线程）
                        logger.warning(
                            f"持仓对冲卖出订单提交后验证失败: token={token_id[:20]}..., "
                            f"持仓份额={position_size:.2f}, 卖出价={sell_price:.4f}。"
                            f"可能是订单被立即成交（matched），将检查持仓变化（最多等待2秒）..."
                        )
                        
                        # 只检查一次，等待2秒（避免长时间阻塞）
                        try:
                            time.sleep(2.0)
                            updated_positions = self.get_positions(size_threshold=0.1, limit=1000)
                            updated_position_size = 0.0
                            for pos in updated_positions:
                                if pos.get("asset") == token_id:
                                    updated_position_size = float(pos.get("size", 0))
                                    break
                            
                            if updated_position_size < position_size:
                                # 持仓减少了，说明订单可能被成交了，清除失败记录
                                with self.lock:
                                    if token_id in self.hedge_sell_failures:
                                        del self.hedge_sell_failures[token_id]
                                
                                logger.info(
                                    f"持仓对冲卖出已确认成交: token={token_id[:20]}..., "
                                    f"原持仓={position_size:.2f}, 当前持仓={updated_position_size:.2f}, "
                                    f"减少={position_size - updated_position_size:.2f}"
                                )
                                results[token_id] = True
                            else:
                                # 持仓未减少，记录失败时间（进入冷却期），避免频繁重试
                                with self.lock:
                                    self.hedge_sell_failures[token_id] = time.time()
                                
                                logger.warning(
                                    f"持仓对冲卖出失败（进入冷却期）: token={token_id[:20]}..., "
                                    f"持仓份额={position_size:.2f}, 当前持仓={updated_position_size:.2f}。"
                                    f"持仓未变化，订单可能失败。将在30秒后再次尝试"
                                )
                                results[token_id] = False
                        except Exception as e:
                            # 检查出错，记录失败时间（进入冷却期）
                            with self.lock:
                                self.hedge_sell_failures[token_id] = time.time()
                            
                            logger.warning(
                                f"检查持仓变化时出错: {e}，无法确认订单是否成交。"
                                f"记录失败时间（进入冷却期），将在30秒后再次尝试: token={token_id[:20]}..."
                            )
                            results[token_id] = False
                elif existing_sell_size > 0:
                    # 持仓份额与已挂卖单份额一致，无需操作
                    logger.debug(
                        f"持仓与卖单份额一致，无需操作: token={token_id[:20]}..., "
                        f"持仓份额={position_size:.2f}, 已挂卖单份额={existing_sell_size:.2f}"
                    )
                    results[token_id] = True
                    
        except Exception as e:
            # 使用 try-except 包裹日志记录，防止日志写入失败导致程序卡死
            try:
                error_msg = str(e)
                # 检查是否是 API 认证错误
                if "401" in error_msg or "Unauthorized" in error_msg or "Invalid api key" in error_msg:
                    logger.error(f"检查持仓对冲时发生 API 认证错误: {e}，请检查 API 密钥是否有效")
                else:
                    logger.error(f"检查持仓对冲时发生错误: {e}")
                import traceback
                traceback.print_exc()
            except Exception as log_error:
                # 如果日志写入也失败，至少尝试输出到 stderr
                try:
                    print(f"[错误] 检查持仓对冲时发生错误: {e}", file=sys.stderr)
                    if log_error:
                        print(f"[错误] 日志写入也失败: {log_error}", file=sys.stderr)
                except:
                    pass  # 如果连 stderr 都无法写入，静默忽略
        
        return results
    
    def replace_filled_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        rewards_max_spread: float
    ) -> Optional[Dict[str, Any]]:
        """
        订单被成交后重新挂单
        
        Args:
            market_id: 市场ID
            token_id: Token ID
            side: 订单方向
            rewards_max_spread: 奖励最大价差（美分）
            
        Returns:
            新订单响应，如果失败返回 None
        """
        # 获取实时订单簿数据
        orderbook = self._get_orderbook(token_id)
        if not orderbook:
            logger.warning(f"无法获取订单簿数据，跳过补单")
            return None
        
        # 计算新的订单价格（奖励区间边界）
        prices = self.strategy.calculate_order_prices(orderbook, rewards_max_spread)
        if not prices:
            logger.warning(f"无法计算新订单价格，跳过补单")
            return None
        
        # 获取市场数据以计算订单份额（基于市场最小奖励份额的倍数）
        market = self.market_data_cache.get(market_id)
        if market:
            order_size = self.strategy.calculate_order_size(market)
        else:
            # 如果无法获取市场数据，使用默认值（50）
            order_size = 50
        
        # 确定价格
        if side == "BUY":
            buy_price = prices.get("buy_price")
            sell_price = prices.get("sell_price")
            
            # 如果是买单，检查是否可以安全挂单（风险管理）
            if buy_price and sell_price:
                # 计算实际挂单价格（买二价或奖励下边界）
                actual_buy_price = self.strategy.calculate_actual_buy_price(orderbook, buy_price)
                
                # 如果没有实际挂单价格（订单簿只有买一价），跳过
                if actual_buy_price is None:
                    logger.warning(
                        f"订单被成交后重新挂单失败: 订单簿只有买一价，跳过补单"
                    )
                    return None
                
                can_place, safety_info = self.strategy.can_place_buy_order_safely(
                    orderbook, buy_price, sell_price, order_size, actual_buy_price
                )
                
                if not can_place:
                    logger.warning(
                        f"订单被成交后重新挂单失败（风险管理）: {safety_info['reason']}，"
                        f"跳过补单以避免风险"
                    )
                    return None
                else:
                    logger.info(f"订单被成交后重新挂单检查通过: {safety_info['reason']}")
                
                # 使用实际挂单价格
                price = actual_buy_price
            else:
                price = None
        else:
            price = prices.get("sell_price")
        
        if not price:
            logger.warning(f"无法获取 {side} 订单价格，跳过补单")
            return None
        
        # 规范化价格（确保符合 Polymarket 规范）
        # 买单不进行规范化，卖单需要进行规范化
        if side == "SELL":
            market = self.market_data_cache.get(market_id)
            price = self.strategy.normalize_price(
                price,
                self.strategy.get_order_price_min_tick_size(market)
            )
        
        # 重新挂单
        return self.place_order(
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=order_size
        )
    
    def place_hedge_sell(
        self,
        market_id: str,
        token_id: str,
        buy_price: float,
        filled_size: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        买单成交后挂出卖单（对冲卖出）
        
        Args:
            market_id: 市场ID
            token_id: Token ID
            buy_price: 买入价格
            filled_size: 实际成交份额（如果为None，使用配置的默认份额）
            
        Returns:
            卖单响应，如果失败返回 None
        """
        # 获取市场数据（用于计算订单份额和价格规范化）
        market = self.market_data_cache.get(market_id)
        
        # 获取订单簿以计算买一价
        best_bid_price = None
        orderbook = self._get_orderbook(token_id)
        if orderbook:
            best_bid_price = normalize_orderbook(orderbook).best_bid
        
        max_bid_gap = config.hedge_sell_max_bid_gap
        use_best_bid = (
            best_bid_price is not None and
            max_bid_gap is not None and
            abs(buy_price - best_bid_price) <= max_bid_gap
        )
        
        # 计算对冲卖出价格（在允许范围内优先使用买一价）
        sell_price = self.strategy.calculate_hedge_sell_price(
            buy_price,
            market=market,
            best_bid_price=best_bid_price,
            max_bid_gap=max_bid_gap
        )
        
        # 使用实际成交份额，如果没有则从市场数据计算
        if filled_size is not None:
            order_size = filled_size
        else:
            # 从市场数据计算订单份额（基于市场最小奖励份额的倍数）
            if market:
                order_size = self.strategy.calculate_order_size(market)
            else:
                # 如果无法获取市场数据，使用默认值（50）
                order_size = 50
        
        # 检查实际持仓，确保有足够的份额可以卖出
        # 1. 查询实际持仓
        positions = self.get_positions(size_threshold=0.1, limit=1000)
        actual_position = 0.0
        for position in positions:
            # asset 字段直接是 token_id 字符串
            position_token_id = position.get("asset")
            if position_token_id == token_id:
                actual_position = float(position.get("size", 0))
                break
        
        # 如果持仓查询为0，但filled_size有值（订单已成交），使用filled_size作为实际持仓
        # 这是因为持仓API可能有延迟，而交易历史是实时的
        if actual_position == 0.0 and filled_size is not None and filled_size > 0:
            logger.info(
                f"持仓查询为0但订单已成交，使用成交份额作为实际持仓: "
                f"token={token_id[:20]}..., 成交份额={filled_size:.2f}"
            )
            actual_position = filled_size
        
        # 2. 计算已挂出的对冲卖单份额（从 active_orders 中查找该 token 的 SELL 订单）
        pending_sell_size = 0.0
        with self.lock:
            # 遍历所有市场，查找该 token_id 的 SELL 订单
            for m_id, tokens_dict in self.active_orders.items():
                if token_id in tokens_dict:
                    sell_order_info = tokens_dict[token_id].get("SELL")
                    if sell_order_info:
                        pending_sell_size += float(sell_order_info.get("size", 0))
        
        # 3. 计算可用持仓
        available_size = actual_position - pending_sell_size
        
        # 4. 记录持仓检查信息
        logger.info(
            f"对冲卖出持仓检查: token={token_id[:20]}..., "
            f"实际持仓={actual_position:.2f}, "
            f"已挂出卖单份额={pending_sell_size:.2f}, "
            f"可用持仓={available_size:.2f}, "
            f"请求卖出份额={order_size:.2f}"
        )
        
        # 5. 检查可用持仓是否足够
        if available_size <= 0:
            logger.warning(
                f"对冲卖出失败: 可用持仓不足或为0, "
                f"token={token_id[:20]}..., "
                f"实际持仓={actual_position:.2f}, "
                f"已挂出卖单份额={pending_sell_size:.2f}, "
                f"可用持仓={available_size:.2f}"
            )
            return None
        
        # 6. 使用可用持仓和请求份额的较小值作为最终卖出份额
        final_sell_size = min(available_size, order_size)
        
        if final_sell_size < order_size:
            logger.warning(
                f"对冲卖出份额调整: 可用持仓不足, "
                f"token={token_id[:20]}..., "
                f"请求份额={order_size:.2f}, "
                f"实际可用={available_size:.2f}, "
                f"最终卖出份额={final_sell_size:.2f}"
            )
        else:
            logger.info(
                f"对冲卖出持仓检查通过: token={token_id[:20]}..., "
                f"最终卖出份额={final_sell_size:.2f}"
            )
        
        if use_best_bid and best_bid_price is not None:
            logger.info(
                f"对冲卖出价格策略: 使用买一价 {best_bid_price:.4f} (与买入价差 {abs(buy_price - best_bid_price):.4f} ≤ 阈值 {max_bid_gap:.4f})"
            )
        elif best_bid_price is not None:
            logger.info(
                f"对冲卖出价格策略: 买一价 {best_bid_price:.4f} 与买入价差 {abs(buy_price - best_bid_price):.4f} 超过阈值 {max_bid_gap:.4f}，回退到原价逻辑"
            )
        else:
            logger.info("对冲卖出价格策略: 无法获取买一价，使用原价逻辑计算卖出价格")
        
        # 挂出卖单
        # 注意：对冲卖单挂出时不移除已成交订单的敞口，因为卖单还未成交
        # 只有当对冲卖单成交后，才会在 check_orders() 中移除已成交订单的敞口
        logger.info(
            f"准备挂出对冲卖单: token={token_id[:20]}..., "
            f"价格={sell_price:.4f}, 份额={final_sell_size:.2f}, "
            f"买入价={buy_price:.4f}"
        )
        
        sell_order_result = self.place_order(
            market_id=market_id,
            token_id=token_id,
            side="SELL",
            price=sell_price,
            size=final_sell_size
        )
        
        if not sell_order_result:
            logger.warning(
                f"对冲卖出订单提交失败: token={token_id[:20]}..., "
                f"价格={sell_price:.4f}, 份额={final_sell_size:.2f}, "
                f"买入价={buy_price:.4f}, "
                f"买一价={best_bid_price if best_bid_price is not None else 'N/A'}, "
                f"使用买一价策略={'是' if use_best_bid else '否'}"
            )
            return None
        
        # 额外验证：确保对冲卖出订单真正挂单
        if sell_order_result:
            order_id = sell_order_result.get("id") or sell_order_result.get("orderID")
            if order_id:
                try:
                    # 再次验证订单是否在活跃列表中（place_order 中已验证，这里作为双重检查）
                    time.sleep(0.3)  # 短暂等待
                    open_orders = self.clob_client.get_open_orders(OpenOrderParams())
                    open_order_ids = {order.get("id") for order in open_orders if order.get("id")}
                    
                    if order_id not in open_order_ids:
                        logger.error(
                            f"对冲卖出订单验证失败: 订单ID={order_id} 不在活跃订单列表中，"
                            f"对冲卖出可能失败。市场={market_id}, token={token_id[:20]}..., "
                            f"价格={sell_price:.2f}, 份额={final_sell_size:.2f}"
                        )
                        return None
                    else:
                        logger.debug(
                            f"对冲卖出订单验证成功: 订单ID={order_id} 已在活跃订单列表中"
                        )
                except Exception as e:
                    logger.warning(
                        f"对冲卖出订单验证过程出错: {e}, 订单ID={order_id}, "
                        f"无法确认订单是否真正挂单"
                    )
                    # 继续返回结果，因为 place_order 已返回成功
        
        return sell_order_result
    
    def cancel_order(self, order_id: str) -> bool:
        """
        取消单个订单
        
        Args:
            order_id: 订单ID
            
        Returns:
            是否成功
        """
        try:
            self.clob_client.cancel_order(OrderPayload(orderID=order_id))
            logger.info(f"订单 {order_id} 已取消")
            
            # 从活跃订单中移除并更新敞口
            # 第一阶段：在持有锁的情况下，收集需要移除的订单信息和敞口（避免死锁）
            order_info_to_remove = None
            market_id_to_update = None
            exposure_to_remove = 0.0
            
            with self.lock:
                for market_id, tokens_dict in list(self.active_orders.items()):
                    for token_id, sides_dict in list(tokens_dict.items()):
                        for side, order_info in list(sides_dict.items()):
                            if order_info.get("order_id") == order_id:
                                # 收集需要移除的信息（在锁外处理，避免死锁）
                                market_id_to_update = market_id
                                exposure_to_remove = order_info.get("exposure", 0)
                                order_info_to_remove = (market_id, token_id, side)
                                
                                # 从活跃订单中移除
                                del self.active_orders[market_id][token_id][side]
                                
                                # 清理空结构：如果 sides_dict 变空了，删除 token_id
                                if not self.active_orders[market_id][token_id]:
                                    del self.active_orders[market_id][token_id]
                                
                                # 清理空结构：如果 tokens_dict 变空了，删除 market_id
                                if not self.active_orders[market_id]:
                                    del self.active_orders[market_id]
                                
                                break
                        if order_info_to_remove:
                            break
                    if order_info_to_remove:
                        break
            
            # 第二阶段：释放锁后，移除敞口（避免死锁）
            # risk_manager.remove_exposure 内部会获取 risk_manager 的锁，但这是不同的锁，不会死锁
            # 但为了保持一致性，我们在锁外调用
            if order_info_to_remove and exposure_to_remove > 0:
                self.risk_manager.remove_exposure(market_id_to_update, exposure_to_remove)
            
            return True
        except Exception as e:
            logger.error(f"取消订单 {order_id} 失败: {e}")
            return False
    
    def cancel_market_orders(self, market_id: str) -> int:
        """
        取消指定市场的所有订单，并取消订阅该市场的 token
        
        Args:
            market_id: 市场ID
            
        Returns:
            取消的订单数量
        """
        cancelled_count = 0
        
        # 第一阶段：在持有锁的情况下，收集需要取消的订单ID
        # 注意：不能在这里调用 cancel_order，因为它内部也会获取锁，会导致死锁
        order_ids_to_cancel = []
        with self.lock:
            if market_id in self.active_orders:
                tokens_dict = self.active_orders[market_id].copy()
                for token_id, sides_dict in tokens_dict.items():
                    for side, order_info in sides_dict.items():
                        order_id = order_info.get("order_id")
                        if order_id:
                            order_ids_to_cancel.append(order_id)
        
        # 第二阶段：释放锁后，执行取消操作（避免死锁）
        # cancel_order 方法内部会获取锁并更新 active_orders 和风险敞口
        for order_id in order_ids_to_cancel:
            if self.cancel_order(order_id):
                cancelled_count += 1
        
        # 第三阶段：在锁内清理已取消的订单状态
        # cancel_order 已经移除了订单并清理了空结构，这里做最后的检查确保市场被完全清理
        with self.lock:
            if market_id in self.active_orders:
                # 再次清理空的市场结构（双重保险，确保市场被完全清理）
                # 清理所有空的 token_id
                if self.active_orders[market_id]:
                    self.active_orders[market_id] = {
                        token_id: sides_dict
                        for token_id, sides_dict in self.active_orders[market_id].items()
                        if sides_dict  # 只保留非空的 sides_dict
                    }
                # 如果市场变空了，删除它
                if not self.active_orders[market_id]:
                    del self.active_orders[market_id]
        
        # 取消订阅该市场的 token
        self._unsubscribe_market_tokens(market_id)
        
        logger.info(f"市场 {market_id} 已取消 {cancelled_count} 个订单")
        return cancelled_count
    
    def cancel_all_buy_orders(self) -> int:
        """
        取消所有购买挂单（BUY订单）
        
        用于程序启动和停止时清理所有购买挂单
        
        Returns:
            取消的订单数量
        """
        cancelled_count = 0
        
        try:
            # 从 API 获取所有活跃订单
            open_orders = self.clob_client.get_open_orders(OpenOrderParams())
            
            # 筛选出所有 BUY 订单并取消
            for order in open_orders:
                order_id = order.get("id")
                side = order.get("side", "").upper()
                
                # 只取消 BUY 订单
                if side == "BUY" and order_id:
                    try:
                        self.clob_client.cancel_order(OrderPayload(orderID=order_id))
                        cancelled_count += 1
                        logger.info(f"已取消购买挂单: 订单ID={order_id}")
                    except Exception as e:
                        logger.warning(f"取消购买挂单失败: 订单ID={order_id}, 错误={e}")
            
            # 清理内部状态中的 BUY 订单
            with self.lock:
                for market_id, tokens_dict in list(self.active_orders.items()):
                    for token_id, sides_dict in list(tokens_dict.items()):
                        if "BUY" in sides_dict:
                            order_info = sides_dict["BUY"]
                            order_id = order_info.get("order_id")
                            
                            # 移除敞口
                            self.risk_manager.remove_exposure(
                                market_id,
                                order_info.get("exposure", 0)
                            )
                            
                            # 从活跃订单中移除
                            del sides_dict["BUY"]
                            
                            # 如果该 token 没有其他订单了，清理
                            if not sides_dict:
                                del tokens_dict[token_id]
                    
                    # 如果该市场没有其他订单了，清理
                    if not tokens_dict:
                        del self.active_orders[market_id]
            
            logger.info(f"已取消所有购买挂单: 共取消 {cancelled_count} 个订单")
            
        except Exception as e:
            logger.error(f"取消所有购买挂单失败: {e}")
        
        return cancelled_count
    
    def get_active_orders(self, market_id: Optional[str] = None) -> Dict[str, Any]:
        """
        获取活跃订单
        
        Args:
            market_id: 市场ID（如果为None，返回所有市场的订单）
            
        Returns:
            订单字典
        """
        with self.lock:
            if market_id:
                return self.active_orders.get(market_id, {}).copy()
            else:
                return {k: v.copy() for k, v in self.active_orders.items()}
    
    def adjust_orders_to_reward_boundaries(
        self,
        markets: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """
        实时监控订单簿变化，调整订单价格以保持在奖励区间边界
        
        通过 HTTP 接口获取实时订单簿数据，如果当前订单价格偏离新的奖励区间边界
        超过阈值，则取消旧订单并重新挂单。
        
        Args:
            markets: 市场数据列表（包含 rewards_max_spread 等信息）
            
        Returns:
            字典 {market_id: adjusted_count}，表示每个市场调整的订单数量
        """
        # 注意：不在这里调用 check_orders()，因为：
        # 1. check_orders() 会查询所有订单状态和交易历史，非常耗时
        # 2. 价格调整不需要实时订单状态，只需要订单簿数据
        # 3. 订单状态检查应该由主循环定期执行，而不是每次价格调整都执行
        
        adjusted_counts = {}
        
        # 更新市场数据缓存
        for market in markets:
            market_id = market.get("market_id")
            if market_id:
                self.market_data_cache[market_id] = market
        
        # 收集所有需要获取订单簿的 token_id（批量获取优化）
        token_ids_to_fetch = []
        token_market_info = {}  # token_id -> (market_id, rewards_max_spread, sides_dict)
        
        with self.lock:
            # 1. 收集 active_orders 中的 token
            for market_id, tokens_dict in list(self.active_orders.items()):
                market = self.market_data_cache.get(market_id)
                if not market:
                    continue
                
                rewards_max_spread = market.get("rewards_max_spread", 0)
                if not rewards_max_spread:
                    continue
                
                for token_id, sides_dict in list(tokens_dict.items()):
                    if token_id not in token_ids_to_fetch:
                        token_ids_to_fetch.append(token_id)
                    token_market_info[token_id] = (market_id, rewards_max_spread, sides_dict)
            
            # 2. 收集 pending_reorder_tokens 中的 token（这些是之前因风险管理而跳过的 token）
            pending_count = len(self.pending_reorder_tokens)
            if pending_count > 0:
                logger.info(f"发现 {pending_count} 个待重新挂单的 token，将尝试重新检查是否可以挂单")
            
            for token_id, pending_info in list(self.pending_reorder_tokens.items()):
                market_id = pending_info.get("market_id")
                if not market_id:
                    continue
                
                market = self.market_data_cache.get(market_id)
                if not market:
                    continue
                
                rewards_max_spread = market.get("rewards_max_spread", 0)
                if not rewards_max_spread:
                    continue
                
                # 如果 token_id 不在 token_ids_to_fetch 中，添加它
                if token_id not in token_ids_to_fetch:
                    token_ids_to_fetch.append(token_id)
                
                # 如果 token_id 已经在 token_market_info 中（来自 active_orders），保留 active_orders 的信息
                # 否则，使用 pending_reorder_tokens 的信息（创建一个空的 sides_dict，因为订单已被取消）
                if token_id not in token_market_info:
                    token_market_info[token_id] = (market_id, rewards_max_spread, {})
        
        # 批量获取订单簿数据（使用 HTTP 接口批量获取，提高效率）
        orderbooks_dict = {}
        if token_ids_to_fetch:
            try:
                from http_orderbook_client import HTTPOrderbookClient
                client = HTTPOrderbookClient()
                try:
                    # 批量获取订单簿数据
                    orderbooks_list = client.get_orderbooks(token_ids_to_fetch)
                    
                    # 将返回的列表转换为字典，以 token_id 为键
                    # 重要：使用 asset_id 字段匹配 token_id，而不是依赖索引顺序
                    # 因为 API 返回的顺序可能与请求顺序不一致
                    for orderbook in orderbooks_list:
                        # 订单簿数据中的 asset_id 就是 token_id
                        asset_id = orderbook.get("asset_id")
                        if asset_id and asset_id in token_ids_to_fetch:
                            # 添加数据源标记
                            orderbook['_source'] = 'HTTP'
                            orderbooks_dict[asset_id] = orderbook
                        else:
                            # 如果 asset_id 不在请求列表中，记录警告
                            logger.warning(f"批量获取订单簿: 返回的 asset_id {asset_id} 不在请求列表中")
                    
                    logger.info(f"批量获取订单簿: 请求 {len(token_ids_to_fetch)} 个，获取到 {len(orderbooks_dict)} 个")
                    
                    # 如果批量获取不完整，对缺失的 token_id 逐个获取
                    missing_token_ids = [tid for tid in token_ids_to_fetch if tid not in orderbooks_dict]
                    if missing_token_ids:
                        logger.info(f"批量获取订单簿不完整，对 {len(missing_token_ids)} 个 token 逐个获取")
                        for token_id in missing_token_ids:
                            orderbook = self.api_client.get_orderbook(token_id)
                            if orderbook:
                                orderbooks_dict[token_id] = orderbook
                finally:
                    client.close()
            except Exception as e:
                logger.warning(f"批量获取订单簿失败，回退到逐个获取: {e}")
                # 回退到逐个获取
                for token_id in token_ids_to_fetch:
                    orderbook = self.api_client.get_orderbook(token_id)
                    if orderbook:
                        orderbooks_dict[token_id] = orderbook
        
        # 第一阶段：在持有锁的情况下，收集需要调整的订单信息
        orders_to_adjust = []  # 存储需要调整的订单信息
        pending_tokens_to_check = []  # 存储待重新挂单的 token 信息
        
        with self.lock:
            for token_id, (market_id, rewards_max_spread, sides_dict) in token_market_info.items():
                orderbook = orderbooks_dict.get(token_id)
                if not orderbook:
                    # 如果没有数据，跳过（不操作）
                    continue
                
                # 计算新的奖励区间边界价格
                prices = self.strategy.calculate_order_prices(orderbook, rewards_max_spread)
                if not prices:
                    continue
                
                # 检查该 token 是否在 pending_reorder_tokens 中（不在 active_orders 中）
                is_pending_token = token_id in self.pending_reorder_tokens
                
                # 如果 sides_dict 为空（说明订单已被取消，来自 pending_reorder_tokens），
                # 且该 token 在 pending_reorder_tokens 中，则尝试重新挂单
                if is_pending_token and not sides_dict:
                    pending_info = self.pending_reorder_tokens[token_id]
                    side = pending_info.get("side", "BUY")
                    
                    # 确定目标价格
                    if side == "BUY":
                        target_price = prices.get("buy_price")
                    else:
                        target_price = prices.get("sell_price")
                    
                    if target_price:
                        # 收集待重新挂单的 token 信息
                        pending_tokens_to_check.append({
                            "market_id": market_id,
                            "token_id": token_id,
                            "side": side,
                            "target_price": target_price,
                            "order_size": pending_info.get("order_size", 50),
                            "last_attempt_time": pending_info.get("last_attempt_time", 0),
                            "prices": prices
                        })
                
                # 检查每个方向的订单（来自 active_orders）
                for side, order_info in list(sides_dict.items()):
                    current_price = order_info.get("price")
                    order_id = order_info.get("order_id")
                    
                    # 确定目标价格
                    if side == "BUY":
                        target_price = prices.get("buy_price")
                    else:
                        target_price = prices.get("sell_price")
                    
                    if not target_price:
                        continue
                    
                    # 检查当前订单是否处于买一价或买二价位置（风险管理）
                    is_currently_best_bid = False
                    is_currently_second_bid = False
                    if side == "BUY":
                        normalized_book = normalize_orderbook(orderbook)
                        best_bid = normalized_book.best_bid
                        second_bid = normalized_book.second_bid

                        if best_bid is not None:
                            # 如果当前订单价格等于或接近买一价（考虑浮点数精度），认为是买一价
                            # 使用 0.0001 的容差来判断是否相等
                            if abs(current_price - best_bid) < 0.0001:
                                is_currently_best_bid = True
                            # 如果当前订单价格等于或接近买二价，认为是买二价
                            elif second_bid is not None and abs(current_price - second_bid) < 0.0001:
                                is_currently_second_bid = True
                        else:
                            # 如果订单簿中没有买单，当前订单就是买一价
                            is_currently_best_bid = True
                    
                    # 卖单不应该进行价格调整，否则会导致永远卖不出
                    # 卖单通常是对冲单，应该保持其原始价格
                    if side == "SELL":
                        continue
                    
                    # 对于买单，需要计算实际挂单价格（买二价或奖励下边界）
                    actual_buy_price_for_check = None
                    safety_violation = False
                    safety_info = {}
                    order_size_for_check = order_info.get("size", 0)
                    if side == "BUY":
                        buy_price_for_check = target_price  # target_price 就是 buy_price
                        actual_buy_price_for_check = self.strategy.calculate_actual_buy_price(orderbook, buy_price_for_check)
                        
                        # 如果没有实际挂单价格（订单簿只有买一价），不触发调整（无法挂买二价）
                        if actual_buy_price_for_check is None:
                            should_adjust = False
                            continue
                        
                        if not order_size_for_check:
                            market = self.market_data_cache.get(market_id)
                            if market:
                                order_size_for_check = self.strategy.calculate_order_size(market)
                            else:
                                order_size_for_check = 50  # 默认值
                        
                        buy_price_boundary = prices.get("buy_price")
                        sell_price_boundary = prices.get("sell_price")
                        if buy_price_boundary and sell_price_boundary:
                            can_stay, safety_info = self.strategy.can_place_buy_order_safely(
                                orderbook,
                                buy_price_boundary,
                                sell_price_boundary,
                                order_size_for_check,
                                actual_buy_price_for_check
                            )
                            if not can_stay:
                                safety_violation = True
                        else:
                            safety_info = {}
                        
                        # 判断当前订单价格是否等于实际挂单价格（买二价或奖励下边界）
                        # 买单不进行规范化，直接比较原始价格
                        # 使用较小的容差处理浮点数精度问题
                        if abs(current_price - actual_buy_price_for_check) < 0.0001 and not safety_violation:
                            # 当前订单价格等于实际挂单价格，不触发调整
                            should_adjust = False
                            continue
                    
                    # 计算价格偏离（基点）
                    # 对于买单，比较当前价格和实际挂单价格（买二价或奖励下边界），而不是奖励区间边界
                    if side == "BUY" and actual_buy_price_for_check is not None:
                        price_diff = abs(current_price - actual_buy_price_for_check)
                    else:
                        price_diff = abs(current_price - target_price)
                    price_diff_bps = int(price_diff * 10000)  # 转换为基点
                    
                    # 如果价格偏离超过阈值，或者当前订单是买一价，收集订单信息用于后续调整
                    # 注意：如果订单处于买二价位置，即使价格偏离也不调整，因为我们的策略是"只挂买二价"
                    threshold_bps = config.price_deviation_threshold_bps
                    # 如果处于买二价位置，不触发调整（即使价格偏离）
                    if is_currently_second_bid and not safety_violation:
                        should_adjust = False
                    else:
                        # 如果价格偏离超过阈值，或者是买一价，触发调整
                        should_adjust = safety_violation or price_diff_bps > threshold_bps or is_currently_best_bid
                    
                    if should_adjust:
                        # 获取当前订单详细信息
                        current_size = order_info.get("size", 0)
                        current_exposure = order_info.get("exposure", 0)
                        current_created_at = order_info.get("created_at", 0)
                        
                        # 获取市场信息用于显示
                        market = self.market_data_cache.get(market_id, {})
                        question = market.get("question", "N/A")
                        outcome = None
                        for token in market.get("tokens", []):
                            if token.get("token_id") == token_id:
                                outcome = token.get("outcome", "N/A")
                                break
                        
                        # 收集需要调整的订单信息（不在这里执行操作，避免死锁）
                        # 记录调整原因：如果是因为买一价位置，标记为买一价原因；否则标记为价格偏离原因
                        adjust_reason = None
                        if safety_violation:
                            adjust_reason = "safety_violation"
                        elif is_currently_best_bid:
                            adjust_reason = "best_bid"  # 因为买一价位置而调整
                        elif price_diff_bps > threshold_bps:
                            adjust_reason = "price_deviation"  # 因为价格偏离而调整
                        
                        orders_to_adjust.append({
                            "market_id": market_id,
                            "token_id": token_id,
                            "side": side,
                            "order_id": order_id,
                            "current_price": current_price,
                            "target_price": target_price,
                            "actual_buy_price": actual_buy_price_for_check if side == "BUY" else None,  # 实际挂单价格（买二价或奖励下边界）
                            "current_size": current_size,
                            "current_exposure": current_exposure,
                            "current_created_at": current_created_at,
                            "price_diff": price_diff,
                            "price_diff_bps": price_diff_bps,
                            "question": question,
                            "outcome": outcome,
                            "prices": prices,
                            "is_currently_best_bid": is_currently_best_bid,  # 标记当前订单是否是买一价
                            "is_currently_second_bid": is_currently_second_bid,  # 标记当前订单是否是买二价（仅用于信息展示）
                            "adjust_reason": adjust_reason,  # 调整原因
                            "safety_info": safety_info or {},
                            "order_size_for_check": order_size_for_check
                        })
        
        # 第二阶段：释放锁后，执行取消和挂单操作
        for order_data in orders_to_adjust:
            market_id = order_data["market_id"]
            token_id = order_data["token_id"]
            side = order_data["side"]
            order_id = order_data["order_id"]
            current_price = order_data["current_price"]
            target_price = order_data["target_price"]  # 初始值是奖励区间边界，后续会被 actual_buy_price 替换
            current_size = order_data["current_size"]
            current_exposure = order_data["current_exposure"]
            current_created_at = order_data["current_created_at"]
            price_diff = order_data["price_diff"]
            price_diff_bps = order_data["price_diff_bps"]
            question = order_data["question"]
            outcome = order_data["outcome"]
            prices = order_data["prices"]
            is_currently_best_bid = order_data.get("is_currently_best_bid", False)
            is_currently_second_bid = order_data.get("is_currently_second_bid", False)
            adjust_reason = order_data.get("adjust_reason", "price_deviation")
            
            # 打印订单调整信息
            logger.info("=" * 80)
            if adjust_reason == "safety_violation":
                logger.info(f"⚠️  检测到订单触发安全检查（价格断层/保护不足），需要立即取消:")
            elif adjust_reason == "best_bid":
                logger.info(f"⚠️  检测到订单处于买一价位置，需要调整（风险管理）:")
            elif adjust_reason == "price_deviation":
                logger.info(f"检测到订单价格偏离，准备调整:")
            else:
                logger.info(f"检测到订单需要调整:")
            logger.info(f"  市场ID: {market_id}")
            logger.info(f"  问题: {question[:60]}...")
            logger.info(f"  Outcome: {outcome}")
            logger.info(f"  Token ID: {token_id[:30]}...")
            logger.info(f"  订单方向: {side}")
            logger.info("-" * 80)
            logger.info(f"【当前订单信息】")
            logger.info(f"  订单ID: {order_id}")
            logger.info(f"  价格: {current_price:.4f}")
            if is_currently_best_bid and side == "BUY":
                logger.info(f"  ⚠️  状态: 当前处于买一价位置（极容易被吃单）")
            elif is_currently_second_bid and side == "BUY":
                logger.info(f"  ⚠️  状态: 当前处于买二价位置（容易被吃单）")
            logger.info(f"  份额: {current_size:.2f}")
            logger.info(f"  敞口: {current_exposure:.2f} USDC")
            logger.info(f"  创建时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_created_at))}")
            logger.info("-" * 80)
            logger.info(f"【目标订单信息】")
            actual_buy_price = order_data.get("actual_buy_price")
            if side == "BUY" and actual_buy_price is not None:
                logger.info(f"  实际挂单价格: {actual_buy_price:.4f} (买二价或奖励下边界)")
                logger.info(f"  奖励区间边界: {target_price:.4f}")
            else:
                logger.info(f"  目标价格: {target_price:.4f} (奖励区间边界)")
            if adjust_reason == "safety_violation":
                logger.info(f"  调整原因: 现有挂单已不满足价格断层/保护要求")
                safety_info = order_data.get("safety_info") or {}
                reason = safety_info.get("reason") or safety_info.get("price_cliff_reason")
                if reason:
                    logger.info(f"  详细原因: {reason}")
            elif adjust_reason == "best_bid":
                logger.info(f"  调整原因: 当前订单处于买一价位置（风险管理）")
            elif adjust_reason == "price_deviation":
                logger.info(f"  价格偏离: {price_diff:.4f} ({price_diff_bps} bps, 阈值={config.price_deviation_threshold_bps} bps)")
                if side == "BUY" and actual_buy_price is not None:
                    logger.info(f"  （当前价格 {current_price:.4f} vs 实际挂单价格 {actual_buy_price:.4f}）")
                if is_currently_second_bid:
                    logger.info(f"  （注：订单当前处于买二价位置，但调整原因是价格偏离）")
            else:
                logger.info(f"  调整原因: 未知")
            logger.info(f"  中间价: {prices.get('mid_price', 0):.4f}")
            logger.info(f"  奖励区间: [{prices.get('buy_price', 0):.4f}, {prices.get('sell_price', 0):.4f}]")
            logger.info("=" * 80)
            
            # 取消旧订单（此时已释放锁，不会死锁）
            if self.cancel_order(order_id):
                # 重新挂单前，如果是买单，检查是否可以安全挂单（风险管理）
                if side == "BUY":
                    # 使用已经批量获取的订单簿数据（避免重复获取）
                    current_orderbook = orderbooks_dict.get(token_id)
                    if not current_orderbook:
                        # 如果批量获取的数据中没有，才单独获取
                        current_orderbook = self._get_orderbook(token_id)
                    if current_orderbook:
                        # 获取奖励区间边界
                        buy_price = prices.get("buy_price")
                        sell_price = prices.get("sell_price")
                        
                        # 计算订单份额（优先使用原订单份额，否则从市场数据计算）
                        order_size_for_check = order_data.get("order_size_for_check") or current_size
                        if not order_size_for_check:
                            market = self.market_data_cache.get(market_id)
                            if market:
                                order_size_for_check = self.strategy.calculate_order_size(market)
                            else:
                                order_size_for_check = 50  # 默认值
                        
                        if buy_price and sell_price:
                            # 计算实际挂单价格（买二价或奖励下边界）
                            actual_buy_price = self.strategy.calculate_actual_buy_price(current_orderbook, buy_price)
                            
                            # 如果没有实际挂单价格（订单簿只有买一价），跳过重新挂单
                            if actual_buy_price is None:
                                logger.warning(
                                    f"  ⚠️  风险管理：跳过重新挂单 - 订单簿只有买一价。"
                                    f"保持订单取消状态，等待下次调整。"
                                )
                                
                                # 将 token 信息添加到 pending_reorder_tokens，以便下次调整时重新检查
                                with self.lock:
                                    self.pending_reorder_tokens[token_id] = {
                                        "market_id": market_id,
                                        "side": side,
                                        "last_attempt_time": time.time(),
                                        "target_price": buy_price,
                                        "order_size": current_size if current_size else (self.strategy.calculate_order_size(self.market_data_cache.get(market_id)) if self.market_data_cache.get(market_id) else 50),
                                        "safety_info": {"reason": "订单簿只有买一价"}
                                    }
                                    logger.info(
                                        f"  已记录到待重新挂单列表: token={token_id[:30]}..., "
                                        f"目标价格={buy_price:.4f}, 原因=订单簿只有买一价"
                                    )
                                continue
                            
                            # 使用安全挂单检查
                            can_place, safety_info = self.strategy.can_place_buy_order_safely(
                                current_orderbook, buy_price, sell_price, order_size_for_check, actual_buy_price
                            )
                            
                            # 买单不进行规范化，直接使用 actual_buy_price（原始买二价或奖励下边界）
                            # 因为原始买二价一定是能下单的价格，奖励下边界是两位小数也一定能下单
                            target_price = actual_buy_price
                            logger.info(f"  使用实际挂单价格（不规范化）: {target_price:.4f} (原始买二价或奖励下边界: {actual_buy_price:.4f})")
                            logger.info(f"  计算的实际挂单价格（规范化后）: {target_price:.4f} (原始买二价: {actual_buy_price:.4f})")
                            
                            # 如果不能安全挂单，跳过重新挂单，但记录到 pending_reorder_tokens 中
                            if not can_place:
                                logger.warning(
                                    f"  ⚠️  风险管理：跳过重新挂单 - {safety_info['reason']}。"
                                    f"保持订单取消状态，等待下次调整。"
                                )
                                
                                # 将 token 信息添加到 pending_reorder_tokens，以便下次调整时重新检查
                                with self.lock:
                                    self.pending_reorder_tokens[token_id] = {
                                        "market_id": market_id,
                                        "side": side,
                                        "last_attempt_time": time.time(),
                                        "target_price": target_price,
                                        "order_size": current_size if current_size else (self.strategy.calculate_order_size(self.market_data_cache.get(market_id)) if self.market_data_cache.get(market_id) else 50),
                                        "safety_info": safety_info
                                    }
                                    logger.info(
                                        f"  已记录到待重新挂单列表: token={token_id[:30]}..., "
                                        f"目标价格={target_price:.4f}, 原因={safety_info['reason']}"
                                    )
                                
                                continue  # 跳过重新挂单，不增加调整计数
                            else:
                                logger.info(f"  风险管理检查通过: {safety_info['reason']}")
                                logger.info(f"  将使用实际挂单价格重新挂单: {target_price:.4f} (来自买二价或奖励下边界)")
                                # target_price 已经在上面计算好了（使用 actual_buy_price），继续使用它
                                # 注意：target_price 现在已经是规范化后的 actual_buy_price（买二价或奖励下边界）
                        else:
                            # 无法获取奖励区间，使用目标价格
                            # 买单不进行规范化，卖单需要进行规范化
                            if side == "SELL":
                                market = self.market_data_cache.get(market_id)
                                target_price = self.strategy.normalize_price(
                                    target_price,
                                    self.strategy.get_order_price_min_tick_size(market)
                                )
                            logger.warning(
                                f"  警告：无法获取奖励区间边界，使用目标价格 {target_price:.4f} 重新挂单。"
                            )
                    else:
                        # 如果无法获取订单簿，记录警告但继续挂单（使用保守策略）
                        logger.warning(
                            f"  警告：无法获取最新订单簿数据，无法进行安全挂单检查。"
                            f"将使用目标价格 {target_price:.4f} 重新挂单。"
                        )
                        # 买单不进行规范化，卖单需要进行规范化
                        if side == "SELL":
                            market = self.market_data_cache.get(market_id)
                            target_price = self.strategy.normalize_price(
                                target_price,
                                self.strategy.get_order_price_min_tick_size(market)
                            )
                else:
                    # 卖单不需要检查，直接使用目标价格（需要规范化）
                    market = self.market_data_cache.get(market_id)
                    target_price = self.strategy.normalize_price(
                        target_price,
                        self.strategy.get_order_price_min_tick_size(market)
                    )
                
                # 重新挂单
                # 获取订单份额（优先使用原订单份额，否则从市场数据计算）
                order_size = current_size
                if not order_size:
                    market = self.market_data_cache.get(market_id)
                    if market:
                        order_size = self.strategy.calculate_order_size(market)
                    else:
                        order_size = 50  # 默认值
                
                # 如果是买单，确保使用实际挂单价格（买二价或奖励下边界），而不是奖励区间边界
                # 注意：target_price 应该已经在 if buy_price and sell_price: 分支中被设置为 actual_buy_price 的规范化值
                # 直接使用 target_price，不要从 order_data 重新计算，避免多次规范化导致价格错误
                if side == "BUY":
                    # target_price 已经在 if buy_price and sell_price: 分支中被正确设置
                    # 直接使用它，不要重新规范化
                    logger.info(f"正在重新挂单: 价格={target_price:.4f} (使用实际挂单价格), 份额={order_size:.2f}")
                else:
                    logger.info(f"正在重新挂单: 价格={target_price:.4f}, 份额={order_size:.2f}")
                
                # 重新挂单（此时已释放锁，不会死锁）
                new_order = self.place_order(
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    price=target_price,
                    size=order_size
                )
                
                if new_order:
                    # 更新调整计数
                    if market_id not in adjusted_counts:
                        adjusted_counts[market_id] = 0
                    adjusted_counts[market_id] += 1
                    
                    new_order_id = new_order.get("id", "N/A")
                    logger.info("-" * 80)
                    logger.info(f"【订单调整完成】")
                    logger.info(f"  新订单ID: {new_order_id}")
                    logger.info(f"  价格变化: {current_price:.4f} → {target_price:.4f} (变化: {price_diff:.4f})")
                    logger.info(f"  份额: {order_size:.2f} (保持不变)")
                    logger.info(f"  状态: {new_order.get('status', 'unknown')}")
                    logger.info("=" * 80)
                else:
                    logger.warning(
                        f"订单价格调整失败: 市场={market_id}, token={token_id[:20]}..., "
                        f"方向={side}, 目标价格={target_price:.4f}"
                    )
        
        if adjusted_counts:
            total_adjusted = sum(adjusted_counts.values())
            logger.info(f"订单价格调整完成: 共调整 {total_adjusted} 个订单")
        
        # 第三阶段：处理 pending_reorder_tokens 中的 token，尝试重新挂单
        if pending_tokens_to_check:
            logger.info("-" * 60)
            logger.info(f"检查 {len(pending_tokens_to_check)} 个待重新挂单的 token（之前因风险管理而跳过）...")
            logger.info("-" * 60)
            
            for pending_data in pending_tokens_to_check:
                market_id = pending_data["market_id"]
                token_id = pending_data["token_id"]
                side = pending_data["side"]
                target_price = pending_data["target_price"]
                order_size = pending_data["order_size"]
                last_attempt_time = pending_data["last_attempt_time"]
                prices = pending_data["prices"]
                
                # 获取市场信息用于显示
                market = self.market_data_cache.get(market_id, {})
                question = market.get("question", "N/A")
                outcome = None
                for token in market.get("tokens", []):
                    if token.get("token_id") == token_id:
                        outcome = token.get("outcome", "N/A")
                        break
                
                logger.info("=" * 80)
                logger.info(f"尝试重新挂单（来自待重新挂单列表）:")
                logger.info(f"  市场ID: {market_id}")
                logger.info(f"  问题: {question[:60]}...")
                logger.info(f"  Outcome: {outcome}")
                logger.info(f"  Token ID: {token_id[:30]}...")
                logger.info(f"  订单方向: {side}")
                logger.info(f"  目标价格: {target_price:.4f}")
                logger.info(f"  上次尝试时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_attempt_time))}")
                logger.info("=" * 80)
                
                # 如果是买单，检查是否可以安全挂单（风险管理）
                if side == "BUY":
                    # 使用已经批量获取的订单簿数据（避免重复获取）
                    current_orderbook = orderbooks_dict.get(token_id)
                    if not current_orderbook:
                        # 如果批量获取的数据中没有，才单独获取
                        current_orderbook = self._get_orderbook(token_id)
                    if current_orderbook:
                        # 获取奖励区间边界
                        buy_price = prices.get("buy_price") if prices else target_price
                        sell_price = prices.get("sell_price") if prices else None
                        
                        # 计算订单份额（从 pending_data 中获取）
                        order_size_for_check = order_size
                        
                        if buy_price and sell_price:
                            # 计算实际挂单价格（买二价或奖励下边界）
                            actual_buy_price = self.strategy.calculate_actual_buy_price(current_orderbook, buy_price)
                            
                            # 如果没有实际挂单价格（订单簿只有买一价），更新 last_attempt_time，继续保留在列表中
                            if actual_buy_price is None:
                                logger.warning(
                                    f"  ⚠️  风险管理：仍然跳过重新挂单 - 订单簿只有买一价。"
                                    f"继续等待下次调整。"
                                )
                                
                                # 更新 last_attempt_time 和 safety_info
                                with self.lock:
                                    if token_id in self.pending_reorder_tokens:
                                        self.pending_reorder_tokens[token_id]["last_attempt_time"] = time.time()
                                        self.pending_reorder_tokens[token_id]["safety_info"] = {"reason": "订单簿只有买一价"}
                                
                                continue  # 跳过重新挂单
                            
                            # 使用安全挂单检查
                            can_place, safety_info = self.strategy.can_place_buy_order_safely(
                                current_orderbook, buy_price, sell_price, order_size_for_check, actual_buy_price
                            )
                            
                            # 买单不进行规范化，直接使用 actual_buy_price（原始买二价或奖励下边界）
                            target_price = actual_buy_price
                            
                            # 如果不能安全挂单，更新 last_attempt_time，继续保留在列表中
                            if not can_place:
                                logger.warning(
                                    f"  ⚠️  风险管理：仍然跳过重新挂单 - {safety_info['reason']}。"
                                    f"继续等待下次调整。"
                                )
                                
                                # 更新 last_attempt_time 和 safety_info
                                with self.lock:
                                    if token_id in self.pending_reorder_tokens:
                                        self.pending_reorder_tokens[token_id]["last_attempt_time"] = time.time()
                                        self.pending_reorder_tokens[token_id]["safety_info"] = safety_info
                                
                                continue  # 跳过重新挂单
                            else:
                                logger.info(f"  风险管理检查通过: {safety_info['reason']}，可以重新挂单")
                        else:
                            # 无法获取奖励区间，使用目标价格
                            # 买单不进行规范化，卖单需要进行规范化
                            if side == "SELL":
                                market = self.market_data_cache.get(market_id)
                                target_price = self.strategy.normalize_price(
                                    target_price,
                                    self.strategy.get_order_price_min_tick_size(market)
                                )
                            logger.warning(
                                f"  警告：无法获取奖励区间边界，使用目标价格 {target_price:.4f} 重新挂单。"
                            )
                    else:
                        # 如果无法获取订单簿，记录警告但继续挂单（使用保守策略）
                        logger.warning(
                            f"  警告：无法获取最新订单簿数据，无法进行安全挂单检查。"
                            f"将使用目标价格 {target_price:.4f} 重新挂单。"
                        )
                        # 买单不进行规范化，卖单需要进行规范化
                        if side == "SELL":
                            market = self.market_data_cache.get(market_id)
                            target_price = self.strategy.normalize_price(
                                target_price,
                                self.strategy.get_order_price_min_tick_size(market)
                            )
                else:
                    # 卖单不需要检查买一价，需要规范化
                    market = self.market_data_cache.get(market_id)
                    target_price = self.strategy.normalize_price(
                        target_price,
                        self.strategy.get_order_price_min_tick_size(market)
                    )
                
                # 尝试重新挂单
                logger.info(f"正在重新挂单: 价格={target_price:.4f}, 份额={order_size:.2f}")
                
                new_order = self.place_order(
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    price=target_price,
                    size=order_size
                )
                
                if new_order:
                    # 从 pending_reorder_tokens 中移除（成功挂单）
                    with self.lock:
                        if token_id in self.pending_reorder_tokens:
                            del self.pending_reorder_tokens[token_id]
                    
                    # 更新调整计数
                    if market_id not in adjusted_counts:
                        adjusted_counts[market_id] = 0
                    adjusted_counts[market_id] += 1
                    
                    new_order_id = new_order.get("id", "N/A")
                    logger.info("-" * 80)
                    logger.info(f"【待重新挂单的 token 成功挂单】")
                    logger.info(f"  新订单ID: {new_order_id}")
                    logger.info(f"  价格: {target_price:.4f}")
                    logger.info(f"  份额: {order_size:.2f}")
                    logger.info(f"  状态: {new_order.get('status', 'unknown')}")
                    logger.info("=" * 80)
                else:
                    logger.warning(
                        f"待重新挂单的 token 挂单失败: 市场={market_id}, token={token_id[:20]}..., "
                        f"方向={side}, 目标价格={target_price:.4f}"
                    )
        
        # 检查需要双边挂单的市场：如果只有一边有活跃订单，取消这些订单
        # 在订单调整后，可能某些订单被取消或调整失败，导致只有单边有订单
        self._check_and_cancel_single_side_orders()
        
        return adjusted_counts
    
    def get_order_statistics(self) -> Dict[str, Any]:
        """
        获取订单状态统计信息
        
        Returns:
            包含订单统计信息的字典：
            {
                "active_orders_count": 活跃订单数,
                "active_markets_count": 活跃市场数,
                "total_exposure_usdc": 总敞口（USDC）,
                "filled_buy_orders_count": 已成交买单数,
                "subscribed_tokens_count": 订阅的 token 数（HTTP 方式为 0）,
                "markets": {
                    market_id: {
                        "orders_count": 订单数,
                        "exposure_usdc": 敞口（USDC）,
                        "tokens_count": token 数
                    }
                }
            }
        """
        stats = {
            "active_orders_count": 0,
            "active_markets_count": 0,
            "total_exposure_usdc": 0.0,
            "filled_buy_orders_count": 0,
            "subscribed_tokens_count": 0,  # HTTP 方式不需要订阅
            "markets": {}
        }
        
        with self.lock:
            # 统计活跃订单
            for market_id, tokens_dict in self.active_orders.items():
                market_orders_count = 0
                market_exposure = 0.0
                market_tokens = set()
                
                for token_id, sides_dict in tokens_dict.items():
                    market_tokens.add(token_id)
                    for side, order_info in sides_dict.items():
                        market_orders_count += 1
                        market_exposure += order_info.get("exposure", 0)
                
                stats["active_orders_count"] += market_orders_count
                stats["total_exposure_usdc"] += market_exposure
                stats["markets"][market_id] = {
                    "orders_count": market_orders_count,
                    "exposure_usdc": market_exposure,
                    "tokens_count": len(market_tokens)
                }
            
            stats["active_markets_count"] = len(self.active_orders)
            
            # 统计已成交买单
            for market_id, filled_orders in self.filled_buy_orders.items():
                stats["filled_buy_orders_count"] += len(filled_orders)
        
        return stats

