"""
市场数据管理模块
"""
from typing import List, Dict, Any, Optional
from api_client import PolymarketAPIClient
from config import config
from logger import setup_logger
from market_making_strategy import normalize_orderbook

logger = setup_logger("market_manager")


class MarketManager:
    """市场管理器"""
    
    def __init__(self, api_client: PolymarketAPIClient):
        """
        初始化市场管理器
        
        Args:
            api_client: Polymarket API 客户端
        """
        self.api_client = api_client
        self.markets: List[Dict[str, Any]] = []
        self.selected_markets: List[Dict[str, Any]] = []
    
    def scan_rewards_markets(self) -> List[Dict[str, Any]]:
        """
        扫描所有拥有流动性奖励的市场
        
        优先从 Redis 缓存读取，如果缓存不存在，则调用 API
        
        Returns:
            市场列表
        """
        logger.info("开始扫描所有拥有流动性奖励的市场...")
        
        try:
            # 优先从 Redis 读取（通过 api_client，它会自动处理缓存）
            markets = self.api_client.get_all_rewards_markets(use_cache=True)
            self.markets = markets
            logger.info(f"成功扫描到 {len(markets)} 个有流动性奖励的市场")
            return markets
        except Exception as e:
            logger.error(f"扫描市场失败: {e}")
            raise
    
    def calculate_reward_ratio(
        self, 
        market: Dict[str, Any],
        orderbooks_dict: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> float:
        """
        计算奖励比值，使用官方奖励计算公式
        
        根据 Polymarket 官方文档实现真实的奖励计算：
        - Equation 1: S(v,s) = ((v-s)/v)² · b
        - Equation 2 & 3: Q_one 和 Q_two（双边流动性）
        - Equation 4: Q_min（考虑单边/双边流动性要求）
        - Equation 5-7: 归一化过程
        
        Args:
            market: 市场数据
            orderbooks_dict: 订单簿数据字典 {token_id: orderbook}，如果提供则考虑竞争
            
        Returns:
            收益比值，如果无法计算则返回 0
        """
        from reward_calculator import (
            calculate_q_one_q_two,
            calculate_q_min,
            estimate_our_score,
            estimate_competitor_total_score
        )
        from market_making_strategy import MarketMakingStrategy
        
        rewards_config = market.get("rewards_config", [])
        if not rewards_config:
            return 0.0
        
        # 获取每日奖励率
        rate_per_day = rewards_config[0].get("rate_per_day", 0)
        if rate_per_day <= 0:
            return 0.0
        
        # 获取最小下单份额
        rewards_min_size = market.get("rewards_min_size", 0)
        if rewards_min_size <= 0:
            return 0.0
        
        # 如果没有提供订单簿数据，返回 0（拒绝使用简单计算，因为不准确）
        if orderbooks_dict is None or len(orderbooks_dict) == 0:
            logger.debug(f"市场 {market.get('market_id')} 没有订单簿数据，无法计算收益比值")
            return 0.0
        
        rewards_max_spread = market.get("rewards_max_spread", 0)
        if rewards_max_spread <= 0:
            # 如果没有奖励区间信息，使用简单计算
            ratio = rate_per_day / rewards_min_size
            return ratio
        
        tokens = market.get("tokens", [])
        if not tokens or len(tokens) < 2:
            # 如果 token 数量少于2个，无法计算互补关系，使用简单计算
            ratio = rate_per_day / rewards_min_size
            return ratio
        
        # 识别互补 token（同一市场下的两个 token）
        # 假设第一个 token 是 m，第二个 token 是 m'（互补市场）
        token_m = tokens[0]
        token_m_prime = tokens[1] if len(tokens) > 1 else None
        
        token_id_m = token_m.get("token_id")
        token_id_m_prime = token_m_prime.get("token_id") if token_m_prime else None
        
        if not token_id_m:
            return 0.0
        
        # 获取订单簿数据
        orderbook_m = orderbooks_dict.get(token_id_m)
        orderbook_m_prime = orderbooks_dict.get(token_id_m_prime) if token_id_m_prime else None
        
        if not orderbook_m:
            return 0.0
        
        # 检查订单簿是否为空
        bids_m = orderbook_m.get("bids", [])
        asks_m = orderbook_m.get("asks", [])
        if not bids_m and not asks_m:
            logger.debug(f"市场 {market.get('market_id')} token {token_id_m[:20]}... 订单簿为空")
            return 0.0
        
        # 计算中间价（使用第一个 token 的订单簿）
        strategy = MarketMakingStrategy()
        mid_price = strategy.calculate_mid_price(orderbook_m)
        if mid_price is None:
            logger.debug(f"市场 {market.get('market_id')} 无法计算中间价")
            return 0.0
        
        # 计算奖励区间边界（用于确定我们的挂单位置）
        buy_price, sell_price = strategy.calculate_reward_range(
            mid_price, rewards_max_spread, market=market
        )
        
        # 计算实际挂单价格（买二价或奖励下边界）
        actual_buy_price = strategy.calculate_actual_buy_price(orderbook_m, buy_price)
        if actual_buy_price is None:
            # 如果订单簿只有买一价，无法挂买二价，返回0
            logger.debug(f"市场 {market.get('market_id')} 订单簿只有买一价，无法挂买二价")
            return 0.0
        
        # 参数设置
        v = float(rewards_max_spread)  # max spread (in cents)
        b = 1.0  # in-game multiplier (默认值)
        c = 3.0  # scaling factor (固定值)
        our_size = float(rewards_min_size)
        
        # 估算竞争者的总评分（基于订单簿中的所有订单）
        competitor_q_one, competitor_q_two = estimate_competitor_total_score(
            orderbook_m,
            orderbook_m_prime,
            mid_price,
            v,
            b,
            rewards_max_spread
        )
        
        # 计算竞争者的 Q_min
        competitor_q_min = calculate_q_min(
            competitor_q_one,
            competitor_q_two,
            mid_price,
            c
        )
        
        # 计算我们的评分（基于实际挂单的位置和份额）
        # 使用实际挂单价格（买二价或奖励下边界）而不是奖励区间边界
        # 考虑两个互补 token 的订单（根据文档，Q_one 和 Q_two 是跨两个互补市场的）
        our_q_min = estimate_our_score(
            our_buy_price=actual_buy_price,  # 使用实际挂单价格（买二价或奖励下边界）
            our_sell_price=sell_price,
            our_size=our_size,
            mid_price=mid_price,
            v=v,
            b=b,
            rewards_max_spread=rewards_max_spread,
            orderbook_m_prime=orderbook_m_prime
        )
        
        # 计算我们的份额占比（Equation 5: Q_normal）
        # 我们的 Q_normal = 我们的 Q_min / (我们的 Q_min + 竞争者总 Q_min)
        total_q_min = our_q_min + competitor_q_min
        if total_q_min <= 0:
            # 如果总评分为0，说明没有有效订单，我们可以获得全部奖励
            our_share_ratio = 1.0
        else:
            our_share_ratio = our_q_min / total_q_min
        
        # 计算预期奖励 = 总奖励 × 我们的份额占比
        expected_reward = rate_per_day * our_share_ratio
        
        # 计算收益比值 = 预期奖励 / 我们的投入份额
        ratio = expected_reward / our_size if our_size > 0 else 0.0
        
        logger.debug(
            f"奖励计算详情: 市场={market.get('market_id')}, "
            f"中间价={mid_price:.4f}, "
            f"我们的Q_min={our_q_min:.2f}, "
            f"竞争者Q_min={competitor_q_min:.2f}, "
            f"我们的份额占比={our_share_ratio:.4f}, "
            f"预期奖励={expected_reward:.4f}, "
            f"收益比值={ratio:.6f}"
        )
        
        return ratio
    
    def filter_markets(
        self, 
        markets: Optional[List[Dict[str, Any]]] = None,
        min_reward_ratio: Optional[float] = None,
        max_markets: Optional[int] = None,
        spread_range: Optional[Dict[str, Optional[float]]] = None,
        volume_24hr_range: Optional[Dict[str, Optional[float]]] = None,
        rewards_min_size_range: Optional[Dict[str, Optional[int]]] = None
    ) -> List[Dict[str, Any]]:
        """
        筛选市场：计算收益比值，选择收益最大化的市场
        
        Args:
            markets: 市场列表（如果为None，使用self.markets）
            min_reward_ratio: 最小奖励比值阈值（如果为None，使用配置值）
            max_markets: 最大选择市场数量（如果为None，使用配置值）
            spread_range: 价差过滤范围 {"min": float|None, "max": float|None}（如果为None，使用配置值）
            volume_24hr_range: 24小时交易量过滤范围 {"min": float|None, "max": float|None}（如果为None，使用配置值）
            rewards_min_size_range: 最小奖励份额过滤范围 {"min": int|None, "max": int|None}（如果为None，使用配置值）
            
        Returns:
            筛选后的市场列表（按收益比值降序排列）
        """
        if markets is None:
            markets = self.markets
        
        if min_reward_ratio is None:
            min_reward_ratio = config.min_reward_ratio
        
        if max_markets is None:
            max_markets = config.max_markets
        
        # 获取范围配置（确保是属性访问，不是方法调用）
        if spread_range is None:
            spread_range = config.spread_range
        if volume_24hr_range is None:
            volume_24hr_range = config.volume_24hr_range
        if rewards_min_size_range is None:
            rewards_min_size_range = config.rewards_min_size_range
        
        # 确保范围配置是字典类型
        if not isinstance(spread_range, dict):
            logger.warning("spread_range 配置格式错误，使用默认值")
            spread_range = {"min": None, "max": 0.05}
        if not isinstance(volume_24hr_range, dict):
            logger.warning("volume_24hr_range 配置格式错误，使用默认值")
            volume_24hr_range = {"min": 0.0, "max": None}
        if not isinstance(rewards_min_size_range, dict):
            logger.warning("rewards_min_size_range 配置格式错误，使用默认值")
            rewards_min_size_range = {"min": None, "max": 0}
        
        spread_min = spread_range.get("min")
        spread_max = spread_range.get("max")
        volume_min = volume_24hr_range.get("min")
        volume_max = volume_24hr_range.get("max")
        size_min = rewards_min_size_range.get("min")
        size_max = rewards_min_size_range.get("max")
        
        # 获取时间限制配置
        min_days_until_end = config.min_days_until_end
        
        logger.info(
            f"开始筛选市场，最小奖励比值: {min_reward_ratio}, "
            f"最大市场数: {max_markets}, "
            f"价差范围: [{spread_min if spread_min is not None else '无下限'}, {spread_max if spread_max is not None else '无上限'}], "
            f"24小时交易量范围: [{volume_min if volume_min is not None else '无下限'}, {volume_max if volume_max is not None else '无上限'}] USDC, "
            f"最小奖励份额范围: [{size_min if size_min is not None else '无下限'}, {size_max if size_max is not None else '无上限'}], "
            f"最小剩余天数: {min_days_until_end if min_days_until_end is not None else '无限制'}"
        )
        
        # 首先过滤掉交易量不在范围内的市场
        if volume_min is not None or volume_max is not None:
            volume_filtered_markets = []
            volume_filtered_count = 0
            for market in markets:
                volume_24hr = float(market.get("volume_24hr", 0))
                # 检查是否在范围内
                if volume_min is not None and volume_24hr < volume_min:
                    volume_filtered_count += 1
                    continue
                if volume_max is not None and volume_24hr > volume_max:
                    volume_filtered_count += 1
                    continue
                volume_filtered_markets.append(market)
            
            if volume_filtered_count > 0:
                logger.info(f"过滤掉 {volume_filtered_count} 个24小时交易量不在范围内的市场")
            
            markets = volume_filtered_markets
        
        # 过滤掉最小奖励份额不在范围内的市场
        if size_min is not None or size_max is not None:
            size_filtered_markets = []
            size_filtered_count = 0
            for market in markets:
                rewards_min_size = market.get("rewards_min_size", 0)
                # 检查是否在范围内
                if size_min is not None and rewards_min_size < size_min:
                    size_filtered_count += 1
                    continue
                if size_max is not None and rewards_min_size > size_max:
                    size_filtered_count += 1
                    continue
                size_filtered_markets.append(market)
            
            if size_filtered_count > 0:
                logger.info(f"过滤掉 {size_filtered_count} 个最小奖励份额不在范围内的市场")
            
            markets = size_filtered_markets
        
        # 批量获取订单簿数据（使用 HTTP 接口）
        # 注意：价差过滤将在获取订单簿后，使用实时价格计算
        orderbooks_dict = None
        if markets:
            # 获取订单簿数据（优先使用 Redis 缓存，用于市场初筛）
            try:
                orderbooks_dict = self.api_client.get_markets_orderbooks(markets, use_cache=True)
                logger.info(f"获取到 {len(orderbooks_dict)} 个订单簿用于计算竞争情况")
                
                # 检查：如果没有订单簿数据，拒绝筛选
                if not orderbooks_dict or len(orderbooks_dict) == 0:
                    logger.error("无法获取任何订单簿数据，无法进行准确筛选")
                    raise Exception("无法获取订单簿数据，筛选已取消")
            except Exception as e:
                logger.warning(f"获取订单簿失败，将使用简单计算: {e}")
        
        # 使用实时订单簿数据计算价差并过滤
        from market_making_strategy import MarketMakingStrategy
        strategy = MarketMakingStrategy()
        
        filtered_markets = []
        filtered_count = 0
        
        for market in markets:
            # 尝试从订单簿计算实时价差
            market_spread = None
            tokens = market.get("tokens", [])
            
            # 对每个 token 计算价差，取平均值
            spreads = []
            for token in tokens:
                token_id = token.get("token_id")
                if not token_id:
                    continue
                
                orderbook = orderbooks_dict.get(token_id) if orderbooks_dict else None
                if not orderbook:
                    continue
                
                # 计算中间价
                mid_price = strategy.calculate_mid_price(orderbook)
                if mid_price is None:
                    continue
                
                normalized = normalize_orderbook(orderbook)

                if normalized.is_empty or normalized.is_one_sided:
                    continue
                
                best_bid = normalized.best_bid
                best_ask = normalized.best_ask
                
                if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0:
                    # 计算价差 = (best_ask - best_bid) / mid_price
                    spread = best_ask - best_bid
                    spreads.append(spread)
            
            # 如果有计算出的价差，使用平均值；否则使用 API 返回的 spread
            if spreads:
                market_spread = sum(spreads) / len(spreads)
            else:
                # 回退到 API 数据
                market_spread = float(market.get("spread", 0))
                if market_spread == 0:
                    # 如果 API 也没有，跳过该市场
                    filtered_count += 1
                    continue
            
            # 过滤价差不在范围内的市场
            if spread_min is not None and market_spread < spread_min:
                filtered_count += 1
                continue
            if spread_max is not None and market_spread > spread_max:
                filtered_count += 1
                continue
            
            # 保存计算出的实时价差
            market_copy = market.copy()
            market_copy["realtime_spread"] = market_spread
            filtered_markets.append(market_copy)
        
        if filtered_count > 0:
            logger.info(f"过滤掉 {filtered_count} 个实时价差不在范围内的市场")
        
        markets = filtered_markets
        
        # 时间过滤：筛选掉在指定天数内结束的市场
        if min_days_until_end is not None and min_days_until_end > 0:
            try:
                from redis_orderbook_client import RedisOrderbookClient
                from datetime import datetime
                import time
                
                storage_config = config.orderbook_service.get("storage", {})
                redis_client = RedisOrderbookClient(
                    orderbook_ttl=storage_config.get("orderbook_ttl", 300),
                    db_path=storage_config.get("db_path"),
                )
                
                # 批量获取完整市场详情
                market_ids = [m.get("market_id") for m in markets if m.get("market_id")]
                market_details = redis_client.get_markets_detail_batch(market_ids)
                
                current_time = time.time()
                time_filtered_markets = []
                time_filtered_count = 0
                
                for market in markets:
                    market_id = market.get("market_id")
                    if not market_id:
                        time_filtered_markets.append(market)
                        continue
                    
                    # 获取完整市场详情
                    market_detail = market_details.get(market_id)
                    
                    if not market_detail:
                        # 如果没有完整详情，记录警告但保留该市场（避免过度过滤）
                        logger.debug(f"市场 {market_id} 没有完整详情，无法进行时间过滤，保留该市场")
                        time_filtered_markets.append(market)
                        continue
                    
                    # 获取结束时间
                    end_date_str = market_detail.get("endDate")
                    if not end_date_str:
                        # 如果没有结束时间，记录警告但保留该市场
                        logger.debug(f"市场 {market_id} 没有结束时间信息，无法进行时间过滤，保留该市场")
                        time_filtered_markets.append(market)
                        continue
                    
                    # 解析 ISO 8601 格式的时间字符串
                    try:
                        # 解析格式：'2025-11-21T00:00:00Z'
                        end_datetime = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                        end_timestamp = end_datetime.timestamp()
                    except Exception as e:
                        logger.warning(f"解析市场 {market_id} 的结束时间失败: {end_date_str}, 错误: {e}，保留该市场")
                        time_filtered_markets.append(market)
                        continue
                    
                    # 计算距离结束时间的天数
                    time_until_end = end_timestamp - current_time
                    days_until_end = time_until_end / (24 * 3600)
                    
                    # 如果已经过期，过滤掉
                    if days_until_end <= 0:
                        time_filtered_count += 1
                        logger.debug(f"市场 {market_id} 已过期（结束时间: {end_date_str}），已过滤")
                        continue
                    
                    # 如果剩余天数小于配置值，过滤掉
                    if days_until_end < min_days_until_end:
                        time_filtered_count += 1
                        logger.debug(
                            f"市场 {market_id} 剩余天数 {days_until_end:.2f} 天小于限制 {min_days_until_end} 天"
                            f"（结束时间: {end_date_str}），已过滤"
                        )
                        continue
                    
                    # 保留该市场
                    time_filtered_markets.append(market)
                
                if time_filtered_count > 0:
                    logger.info(f"过滤掉 {time_filtered_count} 个剩余天数小于 {min_days_until_end} 天的市场")
                
                markets = time_filtered_markets
                
                # 关闭 Redis 客户端连接
                redis_client.close()
                
            except Exception as e:
                logger.warning(f"时间过滤失败: {e}，跳过时间过滤继续筛选")
                import traceback
                traceback.print_exc()
        
        # 计算每个市场的收益比值
        markets_with_ratio = []
        for market in markets:
            ratio = self.calculate_reward_ratio(market, orderbooks_dict)
            if ratio >= min_reward_ratio:
                market_copy = market.copy()
                market_copy["reward_ratio"] = ratio
                markets_with_ratio.append(market_copy)
        
        # 在筛选阶段进行挂单检查（只有通过检查的市场才会被选中）
        # 初次筛选时使用 Redis 缓存的订单簿数据，提高筛选速度
        from market_making_strategy import MarketMakingStrategy
        strategy = MarketMakingStrategy()
        
        # 批量获取订单簿数据用于挂单检查（使用缓存）
        logger.info("批量获取订单簿数据用于挂单检查（使用 Redis 缓存）...")
        check_orderbooks_dict = {}
        try:
            # 优先使用已有的 orderbooks_dict（来自 Redis 缓存）
            if orderbooks_dict:
                check_orderbooks_dict = orderbooks_dict.copy()
                logger.info(f"使用已有的订单簿数据（{len(check_orderbooks_dict)} 个）")
            
            # 对于缺失的订单簿，尝试从缓存获取
            missing_token_ids = []
            for market in markets_with_ratio:
                tokens = market.get("tokens", [])
                for token in tokens:
                    token_id = token.get("token_id")
                    if token_id and token_id not in check_orderbooks_dict:
                        missing_token_ids.append(token_id)
            
            if missing_token_ids:
                logger.info(f"尝试从缓存获取 {len(missing_token_ids)} 个缺失的订单簿...")
                try:
                    # 尝试批量获取（使用缓存）
                    missing_orderbooks = self.api_client.get_markets_orderbooks(
                        [m for m in markets_with_ratio if any(t.get("token_id") in missing_token_ids for t in m.get("tokens", []))],
                        use_cache=True
                    )
                    check_orderbooks_dict.update(missing_orderbooks)
                    logger.info(f"从缓存获取到 {len(missing_orderbooks)} 个订单簿")
                except Exception as e:
                    logger.warning(f"从缓存批量获取订单簿失败: {e}")
            
            # 如果仍有缺失，记录警告但不影响筛选（使用已有的数据）
            if len(check_orderbooks_dict) < len(markets_with_ratio) * 2:  # 粗略估计（每个市场约2个token）
                logger.warning(f"部分订单簿数据缺失（获取到 {len(check_orderbooks_dict)} 个），将使用已有数据进行筛选")
        except Exception as e:
            logger.warning(f"获取订单簿数据失败: {e}，将使用已有的 orderbooks_dict")
            check_orderbooks_dict = orderbooks_dict if orderbooks_dict else {}
        
        # 进行挂单检查
        markets_passed_check = []
        filtered_by_order_check = 0
        filtered_reasons = {}  # {reason: count}
        all_token_failure_reasons = {}  # 汇总所有token级别的失败原因 {reason: count}
        requires_both_tokens_count = 0  # 需要双边挂单的市场数
        all_tokens_failed_count = 0  # 所有token都失败的市场数
        
        for market in markets_with_ratio:
            can_place, reason, token_failure_reasons = self._check_market_can_place_orders(
                market, check_orderbooks_dict, strategy
            )
            
            # 汇总token级别的失败原因
            if token_failure_reasons:
                for failure_reason, count in token_failure_reasons.items():
                    all_token_failure_reasons[failure_reason] = all_token_failure_reasons.get(failure_reason, 0) + count
            
            if can_place:
                markets_passed_check.append(market)
            else:
                filtered_by_order_check += 1
                # 统计过滤原因
                reason_key = reason.split("，")[0] if "，" in reason else reason[:50]
                filtered_reasons[reason_key] = filtered_reasons.get(reason_key, 0) + 1
                
                # 统计需要双边挂单和所有token都失败的情况
                if "需要双边挂单" in reason:
                    requires_both_tokens_count += 1
                if "所有" in reason and "都未通过检查" in reason:
                    all_tokens_failed_count += 1
        
        if filtered_by_order_check > 0:
            logger.info(f"挂单检查过滤掉 {filtered_by_order_check} 个市场")
            logger.info(f"  - 需要双边挂单: {requires_both_tokens_count} 个市场")
            logger.info(f"  - 所有 token 都未通过检查: {all_tokens_failed_count} 个市场")
            # 显示前5个最常见的过滤原因
            sorted_reasons = sorted(filtered_reasons.items(), key=lambda x: x[1], reverse=True)
            for reason_key, count in sorted_reasons[:5]:
                logger.info(f"  - {reason_key}: {count} 个市场")
            
            # 显示token级别的失败原因统计（前10个最常见的）
            if all_token_failure_reasons:
                logger.info(f"Token级别失败原因统计（前10个）:")
                sorted_token_reasons = sorted(all_token_failure_reasons.items(), key=lambda x: x[1], reverse=True)
                for reason_key, count in sorted_token_reasons[:10]:
                    logger.info(f"  - {reason_key}: {count} 次")
        
        # 使用通过检查的市场列表，然后按收益比值降序排序
        markets_with_ratio = markets_passed_check
        markets_with_ratio.sort(key=lambda x: x.get("reward_ratio", 0), reverse=True)
        
        # 选择前N个市场
        selected = markets_with_ratio[:max_markets]
        self.selected_markets = selected
        
        logger.info(f"筛选完成，选择了 {len(selected)} 个市场")
        if filtered_by_order_check > 0:
            logger.info(f"  - 挂单检查过滤掉: {filtered_by_order_check} 个市场")
        
        # 注意：现在使用 HTTP 接口获取订单簿数据
        
        # 记录前5个市场的信息
        if selected:
            logger.info("前5个市场（按收益比值排序）:")
            for i, market in enumerate(selected[:5], 1):
                logger.info(
                    f"  {i}. {market.get('question', 'N/A')[:50]}... | "
                    f"比值: {market.get('reward_ratio', 0):.6f} | "
                    f"奖励率: {market.get('rewards_config', [{}])[0].get('rate_per_day', 'N/A')} | "
                    f"最小份额: {market.get('rewards_min_size', 'N/A')}"
                )
        
        return selected
    
    def get_market_info(self, market_id: str) -> Optional[Dict[str, Any]]:
        """
        获取指定市场的信息
        
        Args:
            market_id: 市场ID
            
        Returns:
            市场数据，如果不存在则返回None
        """
        for market in self.markets:
            if market.get("market_id") == market_id:
                return market
        return None
    
    def _check_market_can_place_orders(
        self,
        market: Dict[str, Any],
        orderbooks_dict: Dict[str, Dict[str, Any]],
        strategy: Any
    ) -> tuple[bool, str, Dict[str, int]]:
        """
        检查市场是否可以挂单（包括双边挂单判断和风险管理检查）
        
        Args:
            market: 市场数据
            orderbooks_dict: 订单簿数据字典 {token_id: orderbook}
            strategy: 做市策略对象（MarketMakingStrategy）
            
        Returns:
            (can_place, reason, token_failure_reasons) 
            - can_place: 表示市场是否可以通过检查
            - reason: 原因说明
            - token_failure_reasons: token级别的失败原因统计 {reason: count}
        """
        tokens = market.get("tokens", [])
        rewards_max_spread = market.get("rewards_max_spread", 0)
        market_id = market.get("market_id", "N/A")
        
        if not tokens or not rewards_max_spread:
            return False, "市场缺少 tokens 或 rewards_max_spread", {}
        
        # 统计各种失败原因
        failure_reasons = {}  # {reason: count}
        
        # 1. 判断是否需要双边挂单（任何 token 的中间价 <= 0.10）
        requires_both_tokens = False
        token_mid_prices = {}  # {token_id: mid_price}
        
        for token in tokens:
            token_id = token.get("token_id")
            if not token_id:
                continue
            
            orderbook = orderbooks_dict.get(token_id)
            if not orderbook:
                continue
            
            # 尝试计算中间价，如果失败则跳过该 token（不记录警告，因为可能是订单簿数据不完整）
            mid_price = strategy.calculate_mid_price(orderbook)
            if mid_price is not None:
                token_mid_prices[token_id] = mid_price
                if mid_price <= 0.10:
                    requires_both_tokens = True
        
        # 2. 对于每个 token 进行挂单检查
        token_check_results = {}  # {token_id: (can_place, reason)}
        
        for token in tokens:
            token_id = token.get("token_id")
            outcome = token.get("outcome", "N/A")
            
            if not token_id:
                continue
            
            # 获取订单簿数据
            orderbook = orderbooks_dict.get(token_id)
            if not orderbook:
                # 跳过该 token，继续检查其他 token
                reason = "无法获取订单簿数据"
                token_check_results[token_id] = (False, reason)
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                continue
            
            # 先检查是否能计算中间价（如果不能，说明订单簿数据不完整，跳过该 token）
            mid_price = strategy.calculate_mid_price(orderbook)
            if mid_price is None:
                # 跳过该 token，继续检查其他 token（不记录警告，因为可能是订单簿数据不完整）
                reason = "无法计算中间价格（订单簿数据不完整）"
                token_check_results[token_id] = (False, reason)
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                continue
            
            # 计算订单价格（奖励区间边界）
            prices = strategy.calculate_order_prices(orderbook, rewards_max_spread, market=market)
            if not prices:
                # 跳过该 token，继续检查其他 token
                reason = "无法计算订单价格"
                token_check_results[token_id] = (False, reason)
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                continue
            
            buy_price = prices.get("buy_price")
            sell_price = prices.get("sell_price")
            
            if not buy_price or not sell_price:
                # 跳过该 token，继续检查其他 token
                reason = "无法获取奖励区间边界"
                token_check_results[token_id] = (False, reason)
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                continue
            
            # 计算订单份额
            order_size = strategy.calculate_order_size(market)
            
            # 计算实际挂单价格（买二价或奖励下边界）
            actual_buy_price = strategy.calculate_actual_buy_price(orderbook, buy_price)
            
            # 如果没有实际挂单价格（订单簿只有买一价），跳过
            if actual_buy_price is None:
                reason = "订单簿只有买一价"
                token_check_results[token_id] = (False, reason)
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                continue
            
            # 调用风险管理检查
            can_place, safety_info = strategy.can_place_buy_order_safely(
                orderbook, buy_price, sell_price, order_size, actual_buy_price
            )
            
            reason = safety_info.get("reason", "未知原因")
            token_check_results[token_id] = (can_place, reason)
            if not can_place:
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        
        # 3. 根据是否需要双边挂单决定筛选标准
        passed_tokens = [token_id for token_id, (can_place, _) in token_check_results.items() if can_place]
        total_tokens = len([t for t in tokens if t.get("token_id")])
        
        # 输出详细的检查统计信息（仅在前20个市场输出详细信息，避免日志过多）
        # 但失败原因统计会汇总到全局统计中
        if len(failure_reasons) > 0:
            # 将失败原因添加到全局统计中（通过返回额外的信息）
            # 这里我们通过 logger 输出，但只在需要时输出
            pass  # 失败原因已经在方法内部统计，会在调用处汇总
        
        if requires_both_tokens:
            # 需要双边挂单：所有 token 都必须通过检查
            if len(passed_tokens) == total_tokens and total_tokens > 0:
                return True, f"所有 {total_tokens} 个 token 都通过检查（需要双边挂单）", failure_reasons
            else:
                failed_tokens = [token_id for token_id, (can_place, _) in token_check_results.items() if not can_place]
                return False, f"需要双边挂单，但只有 {len(passed_tokens)}/{total_tokens} 个 token 通过检查，失败的 token: {failed_tokens[:2]}", failure_reasons
        else:
            # 正常市场：至少有一个 token 通过检查即可
            if len(passed_tokens) > 0:
                return True, f"{len(passed_tokens)}/{total_tokens} 个 token 通过检查", failure_reasons
            else:
                return False, f"所有 {total_tokens} 个 token 都未通过检查", failure_reasons
    
    def get_selected_markets(self) -> List[Dict[str, Any]]:
        """
        获取筛选后的市场列表
        
        Returns:
            筛选后的市场列表
        """
        return self.selected_markets
    
    def refresh_markets(self) -> List[Dict[str, Any]]:
        """
        刷新市场数据并重新筛选
        
        Returns:
            筛选后的市场列表
        """
        # 重新扫描市场
        self.scan_rewards_markets()
        
        # 重新筛选（会自动订阅新选中的市场）
        return self.filter_markets()
