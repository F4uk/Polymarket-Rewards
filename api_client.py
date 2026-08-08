"""
Polymarket API 客户端
基于官方文档: https://docs.polymarket.com/quickstart/introduction/main
"""
import requests
import base64
from typing import Optional, Dict, List, Any
import time
from logger import setup_logger

logger = setup_logger("api_client")

# Polymarket 分页结束游标（base64 编码的 "-1"），与 py_clob_client.constants.END_CURSOR 一致
END_CURSOR = "LTE="


class PolymarketAPIClient:
    """Polymarket API 客户端"""
    
    def __init__(self, base_url: str = "https://polymarket.com"):
        """
        初始化 API 客户端
        
        Args:
            base_url: API 基础 URL
        """
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Polymarket-MarketMaking-Bot/1.0',
            'Accept': 'application/json'
        })
    
    def _make_request(
        self, 
        method: str, 
        endpoint: str, 
        params: Optional[Dict] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求，带重试机制
        
        Args:
            method: HTTP 方法 (GET, POST, etc.)
            endpoint: API 端点路径
            params: 查询参数
            max_retries: 最大重试次数
            
        Returns:
            API 响应数据（JSON）
        """
        url = f"{self.base_url}{endpoint}"
        
        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, params=params, timeout=30)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 指数退避
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"API 请求失败: {e}")
    
    def get_rewards_markets(
        self,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        query: str = "",
        tag_slug: str = "all",
        next_cursor: Optional[str] = None,
        only_mergeable: bool = False,
        no_competition: bool = False,
        only_open_orders: bool = False,
        only_positions: bool = False
    ) -> Dict[str, Any]:
        """
        获取有流动性奖励的市场列表
        
        Args:
            order_by: 排序字段（默认: rate_per_day）
            position: 排序方向，ASC 或 DESC（默认: DESC）
            query: 搜索查询字符串（默认: 空字符串）
            tag_slug: 标签筛选（默认: all）
            next_cursor: 分页游标，base64编码的offset（默认: None，从第一页开始）
            only_mergeable: 只返回可合并的市场（默认: False）
            no_competition: 只返回无竞争的市场（默认: False）
            only_open_orders: 只返回有开放订单的市场（默认: False）
            only_positions: 只返回有持仓的市场（默认: False）
            
        Returns:
            包含以下字段的字典：
            - data: 市场列表
            - next_cursor: 下一个分页游标（base64编码）
            - limit: 每页限制（100）
            - count: 当前页数量
            - total_count: 总数量
            
        示例响应结构：
        {
            "data": [
                {
                    "market_id": "570361",
                    "condition_id": "0xcb11...",
                    "question": "...",
                    "tokens": [...],
                    "rewards_config": [...],
                    "rewards_max_spread": 3.5,
                    "rewards_min_size": 200,
                    ...
                }
            ],
            "next_cursor": "MTAw",
            "limit": 100,
            "count": 100,
            "total_count": 3075
        }
        """
        endpoint = "/api/rewards/markets"
        
        params = {
            "orderBy": order_by,
            "position": position,
            "query": query,
            "tagSlug": tag_slug,
            "onlyMergeable": str(only_mergeable).lower(),
            "noCompetition": str(no_competition).lower(),
            "onlyOpenOrders": str(only_open_orders).lower(),
            "onlyPositions": str(only_positions).lower()
        }
        
        # 如果有 next_cursor，添加到参数中
        if next_cursor:
            params["nextCursor"] = next_cursor
        
        return self._make_request("GET", endpoint, params=params)
    
    def get_all_rewards_markets(
        self,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        query: str = "",
        tag_slug: str = "all",
        only_mergeable: bool = False,
        no_competition: bool = False,
        only_open_orders: bool = False,
        only_positions: bool = False,
        max_markets: Optional[int] = None,
        use_cache: bool = True
    ) -> List[Dict[str, Any]]:
        """
        获取所有有流动性奖励的市场（自动处理分页）
        
        优先从 Redis 缓存读取，如果缓存不存在或过期，则调用 API
        
        Args:
            order_by: 排序字段（默认: rate_per_day）
            position: 排序方向，ASC 或 DESC（默认: DESC）
            query: 搜索查询字符串（默认: 空字符串）
            tag_slug: 标签筛选（默认: all）
            only_mergeable: 只返回可合并的市场（默认: False）
            no_competition: 只返回无竞争的市场（默认: False）
            only_open_orders: 只返回有开放订单的市场（默认: False）
            only_positions: 只返回有持仓的市场（默认: False）
            max_markets: 最大返回市场数量（None 表示返回所有，默认: None）
            use_cache: 是否使用 Redis 缓存（默认: True）
            
        Returns:
            所有市场的列表
        """
        # 优先从 Redis 读取（如果启用缓存且没有特殊筛选条件）
        if use_cache and not any([query, only_mergeable, no_competition, only_open_orders, only_positions]):
            try:
                from redis_orderbook_client import RedisOrderbookClient
                from config import config
                
                storage_config = config.orderbook_service.get("storage", {})
                redis_client = RedisOrderbookClient(
                    orderbook_ttl=storage_config.get("orderbook_ttl", 300),
                    db_path=storage_config.get("db_path"),
                )
                
                try:
                    cached_markets = redis_client.get_markets()
                    if cached_markets:
                        logger.debug(f"从 Redis 缓存读取到 {len(cached_markets)} 个市场")
                        # 如果设置了最大数量限制，截取
                        if max_markets and len(cached_markets) > max_markets:
                            return cached_markets[:max_markets]
                        return cached_markets
                except Exception as e:
                    logger.debug(f"从 Redis 读取市场数据失败: {e}")
                finally:
                    redis_client.close()
            except ImportError:
                logger.debug("Redis 客户端未安装，跳过缓存读取")
            except Exception as e:
                logger.debug(f"初始化 Redis 客户端失败: {e}")
        
        # 如果缓存中没有数据或启用特殊筛选，调用 API
        all_markets = []
        next_cursor = None
        
        while True:
            response = self.get_rewards_markets(
                order_by=order_by,
                position=position,
                query=query,
                tag_slug=tag_slug,
                next_cursor=next_cursor,
                only_mergeable=only_mergeable,
                no_competition=no_competition,
                only_open_orders=only_open_orders,
                only_positions=only_positions
            )
            
            # 处理不同的响应格式
            if isinstance(response, list):
                # 如果响应直接是列表，直接使用
                logger.debug(f"收到列表格式响应，包含 {len(response)} 个市场")
                markets = response
                next_cursor = None  # 列表格式通常没有分页
            elif isinstance(response, dict):
                # 如果是字典格式，提取 data 字段
                markets = response.get("data", [])
                next_cursor = response.get("next_cursor")
                logger.debug(f"收到字典格式响应，包含 {len(markets)} 个市场，next_cursor: {next_cursor}")
            else:
                logger.error(f"意外的响应格式: {type(response)}, 响应内容: {response}")
                break
            
            all_markets.extend(markets)
            
            # 如果设置了最大数量限制，检查是否达到
            if max_markets and len(all_markets) >= max_markets:
                result = all_markets[:max_markets]
                # 如果是从 API 获取的且没有特殊筛选，存入 Redis（由订单簿服务统一管理，这里不存储）
                return result
            
            # 检查是否还有下一页（LTE= 表示已到最后一页，不能再请求）
            if not next_cursor or next_cursor == END_CURSOR:
                break
            
            # 避免请求过快
            time.sleep(0.1)
        
        # 如果是从 API 获取的且没有特殊筛选，存入 Redis（由订单簿服务统一管理，这里不存储）
        return all_markets
    
    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单个 token 的订单簿数据（使用 HTTP 接口）
        
        Args:
            token_id: token ID
            
        Returns:
            订单簿数据，如果失败则返回 None
        """
        try:
            from http_orderbook_client import HTTPOrderbookClient
            client = HTTPOrderbookClient()
            try:
                orderbook = client.get_orderbook(token_id)
                if orderbook:
                    # 添加数据源标记
                    orderbook['_source'] = 'HTTP'
                    return orderbook
            finally:
                client.close()
        except Exception as e:
            logger.debug(f"HTTP 获取订单簿失败: {e}")
        
        return None
    
    def get_markets_orderbooks(
        self, 
        markets: List[Dict[str, Any]],
        use_cache: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多个市场的所有 token 订单簿数据
        
        如果 use_cache=True，优先从 Redis 读取（用于市场初筛）
        如果 use_cache=False，使用 HTTP 接口实时获取（用于挂单决策，实时性要求高）
        
        Args:
            markets: 市场列表，每个市场包含 tokens 数组
            use_cache: 是否使用 Redis 缓存（默认 False，实时获取）
            
        Returns:
            字典，key 为 token_id，value 为订单簿数据
            格式: {token_id: orderbook_data}
        """
        # 收集所有 token_id
        all_token_ids = []
        for market in markets:
            tokens = market.get("tokens", [])
            for token in tokens:
                token_id = token.get("token_id")
                if token_id:
                    all_token_ids.append(token_id)
        
        if not all_token_ids:
            return {}
        
        orderbooks_dict = {}
        
        # 如果启用缓存，优先从 Redis 读取（用于市场初筛）
        if use_cache:
            try:
                from redis_orderbook_client import RedisOrderbookClient
                from config import config
                
                storage_config = config.orderbook_service.get("storage", {})
                redis_client = RedisOrderbookClient(
                    orderbook_ttl=storage_config.get("orderbook_ttl", 300),
                    db_path=storage_config.get("db_path"),
                )
                
                try:
                    # 从本地缓存批量获取订单簿
                    cached_orderbooks = redis_client.get_orderbooks_batch(all_token_ids)
                    if cached_orderbooks:
                        for token_id, orderbook in cached_orderbooks.items():
                            orderbook['_source'] = 'Redis'
                            orderbooks_dict[token_id] = orderbook
                        
                        logger.debug(f"从 Redis 缓存读取到 {len(orderbooks_dict)}/{len(all_token_ids)} 个订单簿")
                        
                        # 如果 Redis 中有大部分数据，直接返回（缺失的用 HTTP 补充）
                        if len(orderbooks_dict) >= len(all_token_ids) * 0.8:
                            missing_token_ids = set(all_token_ids) - set(orderbooks_dict.keys())
                            if missing_token_ids:
                                # 补充缺失的数据
                                logger.debug(f"Redis 中缺失 {len(missing_token_ids)} 个订单簿，使用 HTTP 补充")
                                from http_orderbook_client import HTTPOrderbookClient
                                http_client = HTTPOrderbookClient()
                                try:
                                    http_orderbooks = http_client.get_orderbooks(list(missing_token_ids))
                                    for orderbook in http_orderbooks:
                                        asset_id = orderbook.get("asset_id")
                                        if asset_id:
                                            orderbook['_source'] = 'HTTP'
                                            orderbooks_dict[asset_id] = orderbook
                                finally:
                                    http_client.close()
                            
                            return orderbooks_dict
                except Exception as e:
                    logger.debug(f"从 Redis 读取订单簿失败: {e}")
                finally:
                    redis_client.close()
            except ImportError:
                logger.debug("Redis 客户端未安装，跳过缓存读取")
            except Exception as e:
                logger.debug(f"初始化 Redis 客户端失败: {e}")
        
        # 使用 HTTP 接口批量获取订单簿（实时获取，用于挂单决策）
        try:
            from http_orderbook_client import HTTPOrderbookClient
            client = HTTPOrderbookClient()
            try:
                # 批量获取所有订单簿
                orderbooks_list = client.get_orderbooks(all_token_ids)
                
                # 转换为字典格式，key 为 asset_id
                for orderbook in orderbooks_list:
                    asset_id = orderbook.get("asset_id")
                    if asset_id:
                        orderbook['_source'] = 'HTTP'
                        orderbooks_dict[asset_id] = orderbook
                
                if orderbooks_dict:
                    logger.info(f"HTTP 获取到 {len(orderbooks_dict)}/{len(all_token_ids)} 个订单簿")
                
                # 检查缺失的数据
                missing_count = len(all_token_ids) - len(orderbooks_dict)
                if missing_count > 0:
                    logger.warning(f"缺失 {missing_count} 个订单簿数据（可能是不活跃市场）")
            finally:
                client.close()
        except Exception as e:
            logger.error(f"HTTP 批量获取订单簿失败: {e}")
        
        return orderbooks_dict
    
    def get_markets_detail(self, market_ids: List[str]) -> List[Dict[str, Any]]:
        """
        获取完整市场详情信息
        
        调用 gamma-api.polymarket.com/markets API 获取最完整的市场信息
        
        Args:
            market_ids: 市场 ID 列表
            
        Returns:
            完整的市场信息列表，如果失败返回空列表
        """
        if not market_ids:
            return []
        
        try:
            # 构建请求 URL（gamma API）
            gamma_api_url = "https://gamma-api.polymarket.com/markets"
            
            # 构建查询参数（多个 id 参数）
            # requests 库支持使用列表作为参数值，会自动转换为多个同名参数
            response_params = {"id": market_ids}
            
            # 发送请求
            response = self.session.get(gamma_api_url, params=response_params, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            
            # 响应应该是一个列表
            if isinstance(result, list):
                logger.debug(f"成功获取 {len(result)} 个市场的完整详情")
                return result
            else:
                logger.warning(f"gamma API 返回了意外的响应格式: {type(result)}")
                return []
                
        except requests.exceptions.RequestException as e:
            logger.error(f"获取市场详情失败: {e}")
            return []
        except Exception as e:
            logger.error(f"获取市场详情时发生错误: {e}")
            return []