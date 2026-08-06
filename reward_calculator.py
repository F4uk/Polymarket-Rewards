"""
Polymarket 奖励计算模块
根据官方文档实现真实的奖励计算公式
参考: https://docs.polymarket.com/developers/rewards/overview
"""
from typing import Dict, List, Optional, Tuple, Any
from logger import setup_logger
from market_making_strategy import reward_spread_decimal
from config import config

logger = setup_logger("reward_calculator")


def calculate_order_score(v: float, s: float, b: float = 1.0) -> float:
    """
    Equation 1: 计算订单位置评分函数
    
    S(v,s) = ((v-s)/v)² · b
    
    Args:
        v: max spread from midpoint (in cents) - rewards_max_spread（美分）
        s: spread from midpoint (in cents) - 订单距离中间价的价差（美分）
        b: in-game multiplier（游戏内倍数，默认1）
        
    Returns:
        订单评分
    """
    if v <= 0:
        return 0.0
    
    # 如果价差超过最大价差，评分为0
    if s > v:
        return 0.0
    
    # 计算评分: ((v-s)/v)² · b
    score = ((v - s) / v) ** 2 * b
    return score


def calculate_spread_cents(order_price: float, mid_price: float) -> float:
    """
    计算订单距离中间价的价差（美分）
    
    Args:
        order_price: 订单价格（0-1之间）
        mid_price: 中间价格（0-1之间）
        
    Returns:
        价差（美分）
    """
    # 计算价差的绝对值，然后转换为美分
    spread = abs(order_price - mid_price) * 100
    return spread


def calculate_q_one_q_two(
    orderbook_m: Dict[str, Any],
    orderbook_m_prime: Optional[Dict[str, Any]],
    mid_price: float,
    v: float,
    b: float,
    rewards_max_spread: float
) -> Tuple[float, float]:
    """
    Equation 2 & 3: 计算双边流动性分数
    
    Q_one = m 的所有 bids 的评分总和 + m' 的所有 asks 的评分总和
    Q_two = m 的所有 asks 的评分总和 + m' 的所有 bids 的评分总和
    
    Args:
        orderbook_m: 市场 m 的订单簿（如 YES token）
        orderbook_m_prime: 市场 m' 的订单簿（如 NO token，互补市场）
        mid_price: 中间价格（0-1之间）
        v: max spread from midpoint (in cents) - rewards_max_spread
        b: in-game multiplier（默认1）
        rewards_max_spread: 奖励最大价差（美分）
        
    Returns:
        (Q_one, Q_two) 元组
    """
    q_one = 0.0
    q_two = 0.0
    
    # 计算奖励区间边界（用于过滤订单）
    # 奖励区间半宽：美分转小数，唯一缩进来自配置（tick 默认为 0.01，与
    # MarketMakingStrategy.calculate_reward_range 保持一致；不再使用固定减一美分）
    spread_decimal = reward_spread_decimal(
        rewards_max_spread,
        config.reward_boundary_inset_ticks,
        0.01,
    )
    buy_price = max(0.0, mid_price - spread_decimal)
    sell_price = min(1.0, mid_price + spread_decimal)
    
    # 处理市场 m 的订单
    bids_m = orderbook_m.get("bids", [])
    asks_m = orderbook_m.get("asks", [])
    
    # Q_one: m 的 bids
    for bid in bids_m:
        bid_price = float(bid.get("price", 0))
        bid_size = float(bid.get("size", 0))
        
        # 只计算奖励区间范围内的订单
        if buy_price <= bid_price <= sell_price:
            s = calculate_spread_cents(bid_price, mid_price)
            if s <= v:  # 价差在允许范围内
                score = calculate_order_score(v, s, b)
                q_one += score * bid_size
    
    # Q_two: m 的 asks
    for ask in asks_m:
        ask_price = float(ask.get("price", 0))
        ask_size = float(ask.get("size", 0))
        
        # 只计算奖励区间范围内的订单
        if buy_price <= ask_price <= sell_price:
            s = calculate_spread_cents(ask_price, mid_price)
            if s <= v:  # 价差在允许范围内
                score = calculate_order_score(v, s, b)
                q_two += score * ask_size
    
    # 处理市场 m' 的订单（互补市场）
    if orderbook_m_prime:
        bids_m_prime = orderbook_m_prime.get("bids", [])
        asks_m_prime = orderbook_m_prime.get("asks", [])
        
        # Q_one: m' 的 asks
        for ask in asks_m_prime:
            ask_price = float(ask.get("price", 0))
            ask_size = float(ask.get("size", 0))
            
            # 只计算奖励区间范围内的订单
            if buy_price <= ask_price <= sell_price:
                s = calculate_spread_cents(ask_price, mid_price)
                if s <= v:  # 价差在允许范围内
                    score = calculate_order_score(v, s, b)
                    q_one += score * ask_size
        
        # Q_two: m' 的 bids
        for bid in bids_m_prime:
            bid_price = float(bid.get("price", 0))
            bid_size = float(bid.get("size", 0))
            
            # 只计算奖励区间范围内的订单
            if buy_price <= bid_price <= sell_price:
                s = calculate_spread_cents(bid_price, mid_price)
                if s <= v:  # 价差在允许范围内
                    score = calculate_order_score(v, s, b)
                    q_two += score * bid_size
    
    return q_one, q_two


def calculate_q_min(q_one: float, q_two: float, mid_price: float, c: float = 3.0) -> float:
    """
    Equation 4: 计算 Q_min，考虑单边/双边流动性要求
    
    Equation 4a: 如果中间价在 [0.10, 0.90]，允许单边流动性
        Q_min = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))
    
    Equation 4b: 如果中间价在 [0, 0.10) 或 (0.90, 1.0]，要求双边流动性
        Q_min = min(Q_one, Q_two)
    
    Args:
        q_one: Q_one 分数
        q_two: Q_two 分数
        mid_price: 中间价格（0-1之间）
        c: scaling factor（固定为3.0）
        
    Returns:
        Q_min 分数
    """
    if 0.10 <= mid_price <= 0.90:
        # Equation 4a: 允许单边流动性
        q_min = max(
            min(q_one, q_two),
            max(q_one / c, q_two / c)
        )
    else:
        # Equation 4b: 要求双边流动性
        q_min = min(q_one, q_two)
    
    return q_min


def estimate_our_score(
    our_buy_price: float,
    our_sell_price: float,
    our_size: float,
    mid_price: float,
    v: float,
    b: float,
    rewards_max_spread: float,
    orderbook_m_prime: Optional[Dict[str, Any]] = None
) -> float:
    """
    估算我们的评分（基于计划挂单的位置和份额）
    
    根据文档，Q_one 和 Q_two 是跨两个互补市场的：
    - Q_one = m 的所有 bids 的评分 + m' 的所有 asks 的评分
    - Q_two = m 的所有 asks 的评分 + m' 的所有 bids 的评分
    
    假设我们在奖励区间边界挂单：
    - 买单在 buy_price（奖励区间下边界）
    - 卖单在 sell_price（奖励区间上边界）
    
    Args:
        our_buy_price: 我们的买单价格（token m）
        our_sell_price: 我们的卖单价格（token m）
        our_size: 我们的订单份额
        mid_price: 中间价格（0-1之间）
        v: max spread from midpoint (in cents)
        b: in-game multiplier（默认1）
        rewards_max_spread: 奖励最大价差（美分）
        orderbook_m_prime: 互补市场 m' 的订单簿（可选，如果提供则考虑两个 token 的订单）
        
    Returns:
        我们的 Q_min 分数
    """
    # 计算我们的买单和卖单的价差（token m）
    buy_spread = calculate_spread_cents(our_buy_price, mid_price)
    sell_spread = calculate_spread_cents(our_sell_price, mid_price)
    
    # 计算我们的 Q_one 和 Q_two（token m）
    # 买单贡献到 Q_one，卖单贡献到 Q_two
    buy_score = calculate_order_score(v, buy_spread, b) if buy_spread <= v else 0.0
    sell_score = calculate_order_score(v, sell_spread, b) if sell_spread <= v else 0.0
    
    # 我们的 Q_one = m 的买单评分 × 份额
    our_q_one = buy_score * our_size
    
    # 我们的 Q_two = m 的卖单评分 × 份额
    our_q_two = sell_score * our_size
    
    # 如果提供了互补市场 m' 的订单簿，考虑我们在 m' 上的订单
    # 根据文档，m 和 m' 是互补的，价格关系：m_price + m'_price = 1
    # 如果我们在 m 上挂买单，相当于在 m' 上挂卖单（互补关系）
    # 如果我们在 m 上挂卖单，相当于在 m' 上挂买单（互补关系）
    if orderbook_m_prime:
        # 计算互补市场的价格
        # m' 的买价 = 1 - m 的卖价（互补关系）
        # m' 的卖价 = 1 - m 的买价（互补关系）
        m_prime_buy_price = 1.0 - our_sell_price  # m 的卖单对应 m' 的买单
        m_prime_sell_price = 1.0 - our_buy_price  # m 的买单对应 m' 的卖单
        
        # 计算 m' 的价差（使用相同的 mid_price）
        m_prime_buy_spread = calculate_spread_cents(m_prime_buy_price, mid_price)
        m_prime_sell_spread = calculate_spread_cents(m_prime_sell_price, mid_price)
        
        # 计算 m' 的评分
        m_prime_buy_score = calculate_order_score(v, m_prime_buy_spread, b) if m_prime_buy_spread <= v else 0.0
        m_prime_sell_score = calculate_order_score(v, m_prime_sell_spread, b) if m_prime_sell_spread <= v else 0.0
        
        # 根据文档：
        # Q_one = m 的 bids + m' 的 asks
        # Q_two = m 的 asks + m' 的 bids
        # 所以：
        # m' 的卖单（asks）贡献到 Q_one
        # m' 的买单（bids）贡献到 Q_two
        our_q_one += m_prime_sell_score * our_size
        our_q_two += m_prime_buy_score * our_size
    
    # 计算我们的 Q_min
    our_q_min = calculate_q_min(our_q_one, our_q_two, mid_price)
    
    return our_q_min


def estimate_competitor_total_score(
    orderbook_m: Dict[str, Any],
    orderbook_m_prime: Optional[Dict[str, Any]],
    mid_price: float,
    v: float,
    b: float,
    rewards_max_spread: float
) -> Tuple[float, float]:
    """
    估算所有竞争者的总评分
    
    基于订单簿中奖励区间范围内的所有订单计算竞争者总评分
    
    Args:
        orderbook_m: 市场 m 的订单簿
        orderbook_m_prime: 市场 m' 的订单簿（互补市场）
        mid_price: 中间价格（0-1之间）
        v: max spread from midpoint (in cents)
        b: in-game multiplier（默认1）
        rewards_max_spread: 奖励最大价差（美分）
        
    Returns:
        (competitor_q_one, competitor_q_two) 元组
    """
    # 直接使用 calculate_q_one_q_two 计算所有订单的总评分
    # 这包括了订单簿中所有竞争者的订单
    competitor_q_one, competitor_q_two = calculate_q_one_q_two(
        orderbook_m,
        orderbook_m_prime,
        mid_price,
        v,
        b,
        rewards_max_spread
    )
    
    return competitor_q_one, competitor_q_two

