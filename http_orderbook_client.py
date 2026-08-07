#!/usr/bin/env python3
"""
HTTP 订单簿客户端
通过 HTTP POST 请求批量获取 Polymarket 订单簿数据
"""
import json
import time
from typing import List, Dict, Any, Optional
import requests


class HTTPOrderbookClient:
    """
    HTTP 订单簿客户端
    
    通过 HTTP POST 请求批量获取订单簿数据，比 WebSocket 更简单可靠
    """
    
    def __init__(self, base_url: str = "https://clob.polymarket.com", timeout: float = 10.0):
        """
        初始化客户端
        
        Args:
            base_url: API 基础 URL，默认 https://clob.polymarket.com
            timeout: 请求超时时间（秒），默认10秒
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        # 设置请求头
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
    
    def get_orderbooks(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """
        批量获取订单簿数据
        
        Args:
            token_ids: token ID 列表
        
        Returns:
            List[Dict]: 订单簿数据列表，每个元素包含：
            {
                "market": "0x...",
                "asset_id": "...",
                "timestamp": "...",
                "hash": "...",
                "bids": [{"price": "0.08", "size": "1600"}, ...],
                "asks": [{"price": "0.99", "size": "200"}, ...],
                "min_order_size": "5",
                "tick_size": "0.01",
                "neg_risk": true
            }
        """
        if not token_ids:
            return []
        
        # 构建请求体
        request_body = [{"token_id": token_id} for token_id in token_ids]
        
        # 构建请求 URL
        url = f"{self.base_url}/books?token_ids"
        
        try:
            # 发送 POST 请求
            response = self.session.post(
                url,
                json=request_body,
                timeout=self.timeout
            )
            
            # 检查响应状态
            response.raise_for_status()
            
            # 解析 JSON 响应
            orderbooks = response.json()
            
            if not isinstance(orderbooks, list):
                print(f"[HTTP订单簿] 警告：响应格式不是列表: {type(orderbooks)}")
                return []

            for orderbook in orderbooks:
                if isinstance(orderbook, dict) and "_received_at" not in orderbook:
                    orderbook["_received_at"] = time.monotonic()
            
            return orderbooks
            
        except requests.exceptions.Timeout:
            print(f"[HTTP订单簿] 请求超时: {url}")
            return []
        except requests.exceptions.RequestException as e:
            print(f"[HTTP订单簿] 请求失败: {e}")
            return []
        except json.JSONDecodeError as e:
            print(f"[HTTP订单簿] JSON 解析失败: {e}")
            return []
        except Exception as e:
            print(f"[HTTP订单簿] 获取订单簿时发生错误: {e}")
            return []
    
    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单个 token 的订单簿数据
        
        Args:
            token_id: token ID
        
        Returns:
            Dict|None: 订单簿数据，如果未找到则返回 None
        """
        orderbooks = self.get_orderbooks([token_id])
        if orderbooks and len(orderbooks) > 0:
            return orderbooks[0]
        return None
    
    def get_orderbooks_by_market(self, token_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取订单簿数据，按 asset_id 索引返回
        
        Args:
            token_ids: token ID 列表
        
        Returns:
            Dict[str, Dict]: 以 asset_id 为键的订单簿字典
        """
        orderbooks = self.get_orderbooks(token_ids)
        result = {}
        
        for orderbook in orderbooks:
            asset_id = orderbook.get('asset_id')
            if asset_id:
                result[asset_id] = orderbook
        
        return result
    
    def extract_prices(self, orderbook: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """
        从订单簿中提取 bestBid 和 bestAsk
        
        注意：根据实际数据，bids 可能是从低到高排序，asks 可能是从高到低排序
        需要找到最高买价和最低卖价
        
        Args:
            orderbook: 订单簿数据
        
        Returns:
            Dict: 包含 bestBid 和 bestAsk 的字典
            {
                'bestBid': 0.081,   # 最高买价（float）
                'bestAsk': 0.14     # 最低卖价（float）
            }
        """
        best_bid = None
        best_ask = None
        
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        
        # 提取所有价格并找到最高买价和最低卖价
        if bids:
            try:
                bid_prices = [float(b.get('price', 0)) for b in bids if b.get('price')]
                if bid_prices:
                    best_bid = max(bid_prices)  # 最高买价
            except (ValueError, TypeError):
                pass
        
        if asks:
            try:
                ask_prices = [float(a.get('price', 0)) for a in asks if a.get('price')]
                if ask_prices:
                    best_ask = min(ask_prices)  # 最低卖价
            except (ValueError, TypeError):
                pass
        
        return {
            'bestBid': best_bid,
            'bestAsk': best_ask
        }
    
    def get_prices(self, token_ids: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
        """
        批量获取价格（bestBid/bestAsk）
        
        Args:
            token_ids: token ID 列表
        
        Returns:
            Dict[str, Dict]: 以 asset_id 为键的价格字典
            {
                "asset_id_1": {"bestBid": 0.08, "bestAsk": 0.99},
                "asset_id_2": {"bestBid": 0.02, "bestAsk": 0.97},
                ...
            }
        """
        orderbooks = self.get_orderbooks(token_ids)
        result = {}
        
        for orderbook in orderbooks:
            asset_id = orderbook.get('asset_id')
            if asset_id:
                prices = self.extract_prices(orderbook)
                result[asset_id] = prices
        
        return result
    
    def close(self):
        """关闭客户端，释放资源"""
        if self.session:
            self.session.close()


# 便捷函数
def get_orderbooks(token_ids: List[str], base_url: str = "https://clob.polymarket.com") -> List[Dict[str, Any]]:
    """
    便捷函数：批量获取订单簿
    
    Args:
        token_ids: token ID 列表
        base_url: API 基础 URL
    
    Returns:
        List[Dict]: 订单簿数据列表
    """
    client = HTTPOrderbookClient(base_url=base_url)
    try:
        return client.get_orderbooks(token_ids)
    finally:
        client.close()


def get_orderbook(token_id: str, base_url: str = "https://clob.polymarket.com") -> Optional[Dict[str, Any]]:
    """
    便捷函数：获取单个订单簿
    
    Args:
        token_id: token ID
        base_url: API 基础 URL
    
    Returns:
        Dict|None: 订单簿数据
    """
    client = HTTPOrderbookClient(base_url=base_url)
    try:
        return client.get_orderbook(token_id)
    finally:
        client.close()


def get_prices(token_ids: List[str], base_url: str = "https://clob.polymarket.com") -> Dict[str, Dict[str, Optional[float]]]:
    """
    便捷函数：批量获取价格
    
    Args:
        token_ids: token ID 列表
        base_url: API 基础 URL
    
    Returns:
        Dict[str, Dict]: 以 asset_id 为键的价格字典
    """
    client = HTTPOrderbookClient(base_url=base_url)
    try:
        return client.get_prices(token_ids)
    finally:
        client.close()


# 测试代码
if __name__ == "__main__":
    # 测试用例
    test_token_ids = [
        "105252308404183333296039263658468148171781852975694183978776848993567272817690",
        "51373989849727838079459362125164191453214872286008090086677209806665688994350",
        "67242180752617609947386938803672868307547934160418479597359326866763742492464"
    ]
    
    print("=" * 80)
    print("HTTP 订单簿客户端测试")
    print("=" * 80)
    
    # 创建客户端
    client = HTTPOrderbookClient()
    
    try:
        # 测试批量获取订单簿
        print("\n1. 批量获取订单簿:")
        print(f"   请求 {len(test_token_ids)} 个 token...")
        orderbooks = client.get_orderbooks(test_token_ids)
        print(f"   获取到 {len(orderbooks)} 个订单簿")
        
        # 显示每个订单簿的基本信息
        for orderbook in orderbooks:
            asset_id = orderbook.get('asset_id', 'N/A')
            bids_count = len(orderbook.get('bids', []))
            asks_count = len(orderbook.get('asks', []))
            prices = client.extract_prices(orderbook)
            print(f"   - asset_id: {asset_id[:20]}...")
            print(f"     买单数量: {bids_count}, 卖单数量: {asks_count}")
            print(f"     bestBid: {prices['bestBid']}, bestAsk: {prices['bestAsk']}")
        
        # 测试获取单个订单簿
        print("\n2. 获取单个订单簿:")
        if test_token_ids:
            orderbook = client.get_orderbook(test_token_ids[0])
            if orderbook:
                print(f"   成功获取 token {test_token_ids[0][:20]}... 的订单簿")
                print(f"   market: {orderbook.get('market', 'N/A')}")
        
        # 测试批量获取价格
        print("\n3. 批量获取价格:")
        prices = client.get_prices(test_token_ids)
        print(f"   获取到 {len(prices)} 个价格")
        for asset_id, price_info in prices.items():
            print(f"   - {asset_id[:20]}...: bid={price_info['bestBid']}, ask={price_info['bestAsk']}")
        
        # 测试便捷函数
        print("\n4. 测试便捷函数:")
        orderbooks_func = get_orderbooks(test_token_ids[:2])
        print(f"   便捷函数获取到 {len(orderbooks_func)} 个订单簿")
        
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        client.close()
        print("\n测试完成")

