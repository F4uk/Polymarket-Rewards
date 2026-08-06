"""
配置管理模块

所有配置项均从环境变量 / .env 读取（不再使用 config.yaml）。
对外的属性访问器与 orderbook_service 嵌套结构保持不变，因此其它模块无需改动。
"""
import os
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from logger import setup_logger
from runtime_paths import app_base_dir

# 加载环境变量：
# 1) 先按默认行为从当前工作目录向上查找 .env（兼容源码运行）
# 2) 再加载可执行文件/项目目录下的 .env（兼容打包后从任意目录启动），不覆盖已存在的变量
load_dotenv()
load_dotenv(app_base_dir() / ".env", override=False)

# 创建日志
logger = setup_logger("config")


class Config:
    """配置管理类"""
    
    def __init__(self, config_file: str = "config.yaml"):
        """
        初始化配置（全部从环境变量 / .env 读取）

        Args:
            config_file: 已废弃，保留参数仅为向后兼容，不再使用
        """
        self.config_file = config_file
        self.config: Dict[str, Any] = {}
        self.load_config()
    
    # ------------------------------------------------------------------
    # 环境变量解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _env_raw(name: str) -> Optional[str]:
        """读取原始环境变量，空字符串 / null / none 视为 None。"""
        v = os.getenv(name)
        if v is None:
            return None
        v = v.strip()
        if v == "" or v.lower() in ("null", "none"):
            return None
        return v
    
    @classmethod
    def _env_float(cls, name: str, default: Optional[float]) -> Optional[float]:
        r = cls._env_raw(name)
        if r is None:
            return default
        try:
            return float(r)
        except (ValueError, TypeError):
            logger.warning(f"环境变量 {name} 不是有效的数值，使用默认值 {default}")
            return default

    @classmethod
    def _env_float_validated(
        cls,
        name: str,
        default: float,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
    ) -> float:
        """Parse a float config with range validation and safe fallback.

        Invalid or out-of-range values fall back to ``default`` (never zero
        unless zero is the explicit default).
        """
        r = cls._env_raw(name)
        if r is None:
            return default
        try:
            value = float(r)
        except (ValueError, TypeError):
            logger.warning(f"环境变量 {name} 不是有效的数值，使用默认值 {default}")
            return default
        if min_value is not None and value < min_value:
            logger.warning(
                f"环境变量 {name}={value} 低于下限 {min_value}，使用默认值 {default}"
            )
            return default
        if max_value is not None and value > max_value:
            logger.warning(
                f"环境变量 {name}={value} 超过上限 {max_value}，使用默认值 {default}"
            )
            return default
        return value

    @classmethod
    def _env_int_validated(
        cls,
        name: str,
        default: int,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
    ) -> int:
        """Parse an integer config with range validation and safe fallback."""
        r = cls._env_raw(name)
        if r is None:
            return default
        try:
            value = int(float(r))
        except (ValueError, TypeError):
            logger.warning(f"环境变量 {name} 不是有效的整数，使用默认值 {default}")
            return default
        if min_value is not None and value < min_value:
            logger.warning(
                f"环境变量 {name}={value} 低于下限 {min_value}，使用默认值 {default}"
            )
            return default
        if max_value is not None and value > max_value:
            logger.warning(
                f"环境变量 {name}={value} 超过上限 {max_value}，使用默认值 {default}"
            )
            return default
        return value
    
    @classmethod
    def _env_int(cls, name: str, default: Optional[int]) -> Optional[int]:
        r = cls._env_raw(name)
        if r is None:
            return default
        try:
            return int(float(r))
        except (ValueError, TypeError):
            logger.warning(f"环境变量 {name} 不是有效的整数，使用默认值 {default}")
            return default
    
    @classmethod
    def _env_bool(cls, name: str, default: bool) -> bool:
        r = cls._env_raw(name)
        if r is None:
            return default
        return r.lower() in ("1", "true", "yes", "on")
    
    @classmethod
    def _env_str(cls, name: str, default: Optional[str]) -> Optional[str]:
        r = cls._env_raw(name)
        return r if r is not None else default
    
    def load_config(self) -> None:
        """从环境变量构建配置（保持与原嵌套结构一致，供属性访问器使用）"""
        self.config = {
            # 做市策略
            "order_size_multiplier": self._env_float("ORDER_SIZE_MULTIPLIER", 2.0),
            "max_exposure_per_market_usdc": self._env_float("MAX_EXPOSURE_PER_MARKET_USDC", 100.0),
            "max_markets": self._env_int("MAX_MARKETS", 20),
            "min_reward_ratio": self._env_float("MIN_REWARD_RATIO", 0.01),
            "min_profit_margin_bps": self._env_int("MIN_PROFIT_MARGIN_BPS", 5),
            # 市场筛选
            "spread_range": {
                "min": self._env_float("SPREAD_RANGE_MIN", None),
                "max": self._env_float("SPREAD_RANGE_MAX", None),
            },
            "volume_24hr_range": {
                "min": self._env_float("VOLUME_24HR_RANGE_MIN", None),
                "max": self._env_float("VOLUME_24HR_RANGE_MAX", None),
            },
            "rewards_min_size_range": {
                "min": self._env_int("REWARDS_MIN_SIZE_RANGE_MIN", None),
                "max": self._env_int("REWARDS_MIN_SIZE_RANGE_MAX", None),
            },
            "min_days_until_end": self._env_int("MIN_DAYS_UNTIL_END", None),
            # 主循环
            "update_interval_seconds": self._env_int("UPDATE_INTERVAL_SECONDS", 300),
            "order_check_interval_seconds": self._env_float("ORDER_CHECK_INTERVAL_SECONDS", 30),
            "orderbook_update_interval_seconds": self._env_float("ORDERBOOK_UPDATE_INTERVAL_SECONDS", 5),
            "price_deviation_threshold_bps": self._env_float("PRICE_DEVIATION_THRESHOLD_BPS", 1),
            # 订单簿新鲜度
            "max_orderbook_age_seconds": self._env_float_validated(
                "MAX_ORDERBOOK_AGE_SECONDS", 3.0, min_value=0.0
            ),
            # 撤挂迟滞
            "min_quote_lifetime_seconds": self._env_float_validated(
                "MIN_QUOTE_LIFETIME_SECONDS", 10.0, min_value=0.0
            ),
            "requote_min_ticks": self._env_int_validated(
                "REQUOTE_MIN_TICKS", 2, min_value=0
            ),
            "requote_confirmations": self._env_int_validated(
                "REQUOTE_CONFIRMATIONS", 2, min_value=1
            ),
            "requote_cooldown_seconds": self._env_float_validated(
                "REQUOTE_COOLDOWN_SECONDS", 5.0, min_value=0.0
            ),
            # 清仓后重新入场
            "post_fill_reentry_cooldown_seconds": self._env_float_validated(
                "POST_FILL_REENTRY_COOLDOWN_SECONDS", 30.0, min_value=0.0
            ),
            "reward_boundary_inset_ticks": self._env_int_validated(
                "REWARD_BOUNDARY_INSET_TICKS", 0, min_value=0
            ),
            # 分级库存退出
            "exit_immediate_max_loss_ticks": self._env_int_validated(
                "EXIT_IMMEDIATE_MAX_LOSS_TICKS", 1, min_value=0
            ),
            "exit_immediate_max_loss_bps": self._env_float_validated(
                "EXIT_IMMEDIATE_MAX_LOSS_BPS", 300.0, min_value=0.0
            ),
            "exit_passive_wait_seconds": self._env_float_validated(
                "EXIT_PASSIVE_WAIT_SECONDS", 15.0, min_value=0.0
            ),
            "exit_emergency_loss_ticks": self._env_int_validated(
                "EXIT_EMERGENCY_LOSS_TICKS", 3, min_value=0
            ),
            "exit_emergency_loss_bps": self._env_float_validated(
                "EXIT_EMERGENCY_LOSS_BPS", 1000.0, min_value=0.0
            ),
            "exit_max_hold_seconds": self._env_float_validated(
                "EXIT_MAX_HOLD_SECONDS", 90.0, min_value=0.0
            ),
            # 退出能力与持仓确认
            "min_exit_depth_multiplier": self._env_float_validated(
                "MIN_EXIT_DEPTH_MULTIPLIER", 1.2, min_value=1.0
            ),
            "position_dust_size": self._env_float_validated(
                "POSITION_DUST_SIZE", 0.1, min_value=0.0
            ),
            "position_change_confirmations": self._env_int_validated(
                "POSITION_CHANGE_CONFIRMATIONS", 2, min_value=1
            ),
            "order_confirmation_timeout_seconds": self._env_float_validated(
                "ORDER_CONFIRMATION_TIMEOUT_SECONDS", 5.0, min_value=0.0
            ),
            "order_confirmation_retry_seconds": self._env_float_validated(
                "ORDER_CONFIRMATION_RETRY_SECONDS", 0.5, min_value=0.0
            ),
            # 订单管理
            "orderbook_wait_timeout": self._env_float("ORDERBOOK_WAIT_TIMEOUT", 2.0),
            "order_retry_count": self._env_int("ORDER_RETRY_COUNT", 3),
            "order_retry_delay": self._env_float("ORDER_RETRY_DELAY", 1.0),
            # 订单簿数据服务
            "orderbook_service": {
                "enabled": self._env_bool("ORDERBOOK_SERVICE_ENABLED", True),
                "market_scan_interval": self._env_int("ORDERBOOK_SERVICE_MARKET_SCAN_INTERVAL", 300),
                "orderbook_update_interval": self._env_int("ORDERBOOK_SERVICE_ORDERBOOK_UPDATE_INTERVAL", 30),
                "batch_size": self._env_int("ORDERBOOK_SERVICE_BATCH_SIZE", 200),
                "market_detail_ttl": self._env_int("ORDERBOOK_SERVICE_MARKET_DETAIL_TTL", 604800),
                "market_detail_batch_size": self._env_int("ORDERBOOK_SERVICE_MARKET_DETAIL_BATCH_SIZE", 20),
                "market_detail_fill_per_scan": self._env_int("ORDERBOOK_SERVICE_MARKET_DETAIL_FILL_PER_SCAN", 20),
                "storage": {
                    "orderbook_ttl": self._env_int("ORDERBOOK_TTL", 300),
                    "db_path": self._env_str("ORDERBOOK_DB_PATH", None),
                },
            },
            # 风险管理
            "price_cliff_threshold": self._env_float("PRICE_CLIFF_THRESHOLD", 0.05),
            "min_protection_size_multiplier": self._env_float("MIN_PROTECTION_SIZE_MULTIPLIER", 2.0),
            # 对冲卖出
            "hedge_sell": {
                "max_bid_gap": self._env_float("HEDGE_SELL_MAX_BID_GAP", 0.05),
            },
            # 交易配置
            "signature_type": self._env_int("SIGNATURE_TYPE", 1),
            "chain_id": self._env_int("CHAIN_ID", 137),
        }
        self._validate_cross_field_constraints()
        logger.info("成功从环境变量加载配置")

    def _validate_cross_field_constraints(self) -> None:
        """Enforce documented relations between related config values.

        Offending values are clamped to the constraint boundary with a warning
        instead of being silently zeroed.
        """
        cfg = self.config

        # confirmations 至少为 1
        for key in ("requote_confirmations", "position_change_confirmations"):
            if int(cfg.get(key, 1)) < 1:
                logger.warning(f"配置项 {key} 小于 1，回退到 1")
                cfg[key] = 1

        # 深度倍数不小于 1
        if float(cfg.get("min_exit_depth_multiplier", 1.2)) < 1.0:
            logger.warning("配置项 min_exit_depth_multiplier 小于 1，回退到 1.0")
            cfg["min_exit_depth_multiplier"] = 1.0

        # 紧急亏损阈值不小于快速退出阈值
        emergency_ticks = max(
            int(cfg.get("exit_emergency_loss_ticks", 3)),
            int(cfg.get("exit_immediate_max_loss_ticks", 1)),
        )
        if int(cfg.get("exit_emergency_loss_ticks", 3)) < int(
            cfg.get("exit_immediate_max_loss_ticks", 1)
        ):
            logger.warning(
                "配置项 exit_emergency_loss_ticks 小于 exit_immediate_max_loss_ticks，"
                f"回退到 {emergency_ticks}"
            )
            cfg["exit_emergency_loss_ticks"] = emergency_ticks

        emergency_bps = max(
            float(cfg.get("exit_emergency_loss_bps", 1000.0)),
            float(cfg.get("exit_immediate_max_loss_bps", 300.0)),
        )
        if float(cfg.get("exit_emergency_loss_bps", 1000.0)) < float(
            cfg.get("exit_immediate_max_loss_bps", 300.0)
        ):
            logger.warning(
                "配置项 exit_emergency_loss_bps 小于 exit_immediate_max_loss_bps，"
                f"回退到 {emergency_bps}"
            )
            cfg["exit_emergency_loss_bps"] = emergency_bps

        # 最大持仓时间不小于被动等待时间
        max_hold = max(
            float(cfg.get("exit_max_hold_seconds", 90.0)),
            float(cfg.get("exit_passive_wait_seconds", 15.0)),
        )
        if float(cfg.get("exit_max_hold_seconds", 90.0)) < float(
            cfg.get("exit_passive_wait_seconds", 15.0)
        ):
            logger.warning(
                "配置项 exit_max_hold_seconds 小于 exit_passive_wait_seconds，"
                f"回退到 {max_hold}"
            )
            cfg["exit_max_hold_seconds"] = max_hold

        # 确认重试间隔大于零；确认超时不少于重试间隔
        retry = float(cfg.get("order_confirmation_retry_seconds", 0.5))
        if retry <= 0.0:
            logger.warning("配置项 order_confirmation_retry_seconds 必须大于 0，回退到 0.5")
            cfg["order_confirmation_retry_seconds"] = 0.5
            retry = 0.5
        timeout = float(cfg.get("order_confirmation_timeout_seconds", 5.0))
        if timeout < retry:
            logger.warning(
                "配置项 order_confirmation_timeout_seconds 小于重试间隔，"
                f"回退到 {retry}"
            )
            cfg["order_confirmation_timeout_seconds"] = retry
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键
            default: 默认值
            
        Returns:
            配置值
        """
        return self.config.get(key, default)
    
    def get_int(self, key: str, default: int = 0) -> int:
        """获取整数配置值"""
        value = self.get(key, default)
        try:
            return int(value)
        except (ValueError, TypeError):
            logger.warning(f"配置项 {key} 不是有效的整数，使用默认值 {default}")
            return default
    
    def get_float(self, key: str, default: float = 0.0) -> float:
        """获取浮点数配置值"""
        value = self.get(key, default)
        try:
            return float(value)
        except (ValueError, TypeError):
            logger.warning(f"配置项 {key} 不是有效的浮点数，使用默认值 {default}")
            return default
    
    # 便捷属性访问
    @property
    def order_size_multiplier(self) -> float:
        """订单份额倍数（例如 2.0 表示使用市场最小奖励份额的 2 倍）"""
        return self.get_float("order_size_multiplier", 2.0)
    
    @property
    def max_exposure_per_market_usdc(self) -> float:
        """每市场最大敞口（USDC）"""
        return self.get_float("max_exposure_per_market_usdc", 100.0)
    
    @property
    def max_markets(self) -> int:
        """最大选择市场数量"""
        return self.get_int("max_markets", 20)
    
    @property
    def min_reward_ratio(self) -> float:
        """最小奖励比值阈值"""
        return self.get_float("min_reward_ratio", 0.01)
    
    @property
    def min_profit_margin_bps(self) -> int:
        """对冲卖出最小利润（基点）"""
        return self.get_int("min_profit_margin_bps", 5)
    
    @property
    def update_interval_seconds(self) -> int:
        """市场扫描更新间隔（秒）"""
        return self.get_int("update_interval_seconds", 300)
    
    @property
    def orderbook_update_interval_seconds(self) -> float:
        """订单簿监控更新间隔（秒）"""
        return self.get_float("orderbook_update_interval_seconds", 5)
    
    @property
    def price_deviation_threshold_bps(self) -> float:
        """价格偏离阈值（基点）"""
        return self.get_float("price_deviation_threshold_bps", 1)

    @property
    def order_check_interval_seconds(self) -> float:
        """订单状态检查间隔（秒）"""
        return self.get_float("order_check_interval_seconds", 30)

    @property
    def max_orderbook_age_seconds(self) -> float:
        """订单簿最大允许年龄（秒）"""
        return self.get_float("max_orderbook_age_seconds", 3.0)

    @property
    def min_quote_lifetime_seconds(self) -> float:
        """普通重新报价前订单最短存活时间（秒）"""
        return self.get_float("min_quote_lifetime_seconds", 10.0)

    @property
    def requote_min_ticks(self) -> int:
        """普通重新报价的最小价格变化（tick）"""
        return self.get_int("requote_min_ticks", 2)

    @property
    def requote_confirmations(self) -> int:
        """普通重新报价需要的连续确认次数"""
        return self.get_int("requote_confirmations", 2)

    @property
    def requote_cooldown_seconds(self) -> float:
        """普通重新报价冷却（秒）"""
        return self.get_float("requote_cooldown_seconds", 5.0)

    @property
    def post_fill_reentry_cooldown_seconds(self) -> float:
        """清仓后重新买入冷却（秒）"""
        return self.get_float("post_fill_reentry_cooldown_seconds", 30.0)

    @property
    def reward_boundary_inset_ticks(self) -> int:
        """奖励边界向内缩进的 tick 数（0 = 不缩进）"""
        return self.get_int("reward_boundary_inset_ticks", 0)

    @property
    def exit_immediate_max_loss_ticks(self) -> int:
        return self.get_int("exit_immediate_max_loss_ticks", 1)

    @property
    def exit_immediate_max_loss_bps(self) -> float:
        return self.get_float("exit_immediate_max_loss_bps", 300.0)

    @property
    def exit_passive_wait_seconds(self) -> float:
        return self.get_float("exit_passive_wait_seconds", 15.0)

    @property
    def exit_emergency_loss_ticks(self) -> int:
        return self.get_int("exit_emergency_loss_ticks", 3)

    @property
    def exit_emergency_loss_bps(self) -> float:
        return self.get_float("exit_emergency_loss_bps", 1000.0)

    @property
    def exit_max_hold_seconds(self) -> float:
        return self.get_float("exit_max_hold_seconds", 90.0)

    @property
    def min_exit_depth_multiplier(self) -> float:
        return self.get_float("min_exit_depth_multiplier", 1.2)

    @property
    def position_dust_size(self) -> float:
        return self.get_float("position_dust_size", 0.1)

    @property
    def position_change_confirmations(self) -> int:
        return self.get_int("position_change_confirmations", 2)

    @property
    def order_confirmation_timeout_seconds(self) -> float:
        return self.get_float("order_confirmation_timeout_seconds", 5.0)

    @property
    def order_confirmation_retry_seconds(self) -> float:
        return self.get_float("order_confirmation_retry_seconds", 0.5)
    
    @property
    def orderbook_wait_timeout(self) -> float:
        """订单簿数据等待超时时间（秒）"""
        return self.get_float("orderbook_wait_timeout", 2.0)
    
    @property
    def order_retry_count(self) -> int:
        """订单重试次数"""
        return self.get_int("order_retry_count", 3)
    
    @property
    def order_retry_delay(self) -> float:
        """订单重试延迟（秒）"""
        return self.get_float("order_retry_delay", 1.0)
    
    def _get_range_config(self, key: str, default_min: Optional[float] = None, default_max: Optional[float] = None) -> Dict[str, Optional[float]]:
        """
        获取范围配置
        
        Args:
            key: 配置键
            default_min: 默认最小值
            default_max: 默认最大值
            
        Returns:
            包含 min 和 max 的字典
        """
        range_config = self.get(key, {})
        if not isinstance(range_config, dict):
            # 兼容旧配置格式（单一值）
            logger.warning(f"配置项 {key} 不是范围格式，使用默认值")
            return {"min": default_min, "max": default_max}
        
        min_val = range_config.get("min")
        max_val = range_config.get("max")
        
        # 处理 None 字符串
        if min_val == "null" or min_val == "None":
            min_val = None
        if max_val == "null" or max_val == "None":
            max_val = None
        
        # 转换类型
        if min_val is not None:
            try:
                min_val = float(min_val)
            except (ValueError, TypeError):
                logger.warning(f"配置项 {key}.min 不是有效的数值，使用默认值 {default_min}")
                min_val = default_min
        
        if max_val is not None:
            try:
                max_val = float(max_val)
            except (ValueError, TypeError):
                logger.warning(f"配置项 {key}.max 不是有效的数值，使用默认值 {default_max}")
                max_val = default_max
        
        # 如果值为 None，保持为 None（不限制），否则使用配置值或默认值
        result = {}
        result["min"] = min_val  # None 表示不限制下限
        result["max"] = max_val  # None 表示不限制上限
        
        return result
    
    @property
    def spread_range(self) -> Dict[str, Optional[float]]:
        """价差过滤范围（绝对价差）"""
        return self._get_range_config("spread_range", default_min=None, default_max=0.05)
    
    @property
    def volume_24hr_range(self) -> Dict[str, Optional[float]]:
        """24小时交易量过滤范围（USDC）"""
        return self._get_range_config("volume_24hr_range", default_min=0.0, default_max=None)
    
    @property
    def rewards_min_size_range(self) -> Dict[str, Optional[int]]:
        """最小奖励份额过滤范围"""
        range_config = self._get_range_config("rewards_min_size_range", default_min=None, default_max=0)
        # 转换为整数
        min_val = range_config.get("min")
        max_val = range_config.get("max")
        
        if min_val is not None:
            try:
                min_val = int(float(min_val))  # 先转 float 再转 int，避免字符串问题
            except (ValueError, TypeError):
                logger.warning(f"配置项 rewards_min_size_range.min 不是有效的整数，使用 None")
                min_val = None
        
        if max_val is not None:
            try:
                max_val = int(float(max_val))  # 先转 float 再转 int，避免字符串问题
            except (ValueError, TypeError):
                logger.warning(f"配置项 rewards_min_size_range.max 不是有效的整数，使用 None")
                max_val = None
        
        return {"min": min_val, "max": max_val}
    
    # 向后兼容的属性（已废弃，建议使用范围配置）
    @property
    def max_spread(self) -> Optional[float]:
        """最大价差过滤（已废弃，建议使用 spread_range）"""
        return self.spread_range.get("max")
    
    @property
    def min_volume_24hr(self) -> Optional[float]:
        """最小24小时交易量过滤（已废弃，建议使用 volume_24hr_range）"""
        return self.volume_24hr_range.get("min")
    
    @property
    def max_rewards_min_size(self) -> Optional[int]:
        """最大最小奖励份额过滤（已废弃，建议使用 rewards_min_size_range）"""
        return self.rewards_min_size_range.get("max")
    
    @property
    def min_days_until_end(self) -> Optional[int]:
        """市场结束时间限制（最小剩余天数）"""
        value = self.get("min_days_until_end")
        if value is None or value == "null" or value == 0:
            return None
        try:
            return int(float(value))
        except (ValueError, TypeError):
            logger.warning(f"配置项 min_days_until_end 不是有效的整数，使用 None（不限制）")
            return None
    
    @property
    def signature_type(self) -> int:
        """签名类型（0=EOA/MetaMask, 1=email/Magic钱包, 2=浏览器钱包代理）"""
        return self.get_int("signature_type", 1)
    
    @property
    def chain_id(self) -> int:
        """链ID（137=Polygon主网）"""
        return self.get_int("chain_id", 137)
    
    @property
    def price_cliff_threshold(self) -> float:
        """价格断层阈值（绝对差值，例如 0.05 表示如果后面价格相差超过 0.05 就认为是断层）"""
        return self.get_float("price_cliff_threshold", 0.05)
    
    @property
    def min_protection_size_multiplier(self) -> float:
        """最小保护份额倍数（我们下单份额的倍数，例如 2.0 表示买一价和买二价的总份额至少是我们下单份额的2倍）"""
        return self.get_float("min_protection_size_multiplier", 2.0)
    
    @property
    def hedge_sell_config(self) -> Dict[str, Any]:
        """对冲卖出配置"""
        return self.get("hedge_sell", {})
    
    @property
    def hedge_sell_max_bid_gap(self) -> float:
        """使用买一价对冲时允许的最大价差"""
        hedge_config = self.hedge_sell_config or {}
        try:
            return float(hedge_config.get("max_bid_gap", 0.05))
        except (ValueError, TypeError):
            logger.warning("hedge_sell.max_bid_gap 配置无效，使用默认值 0.05")
            return 0.05
    
    @property
    def orderbook_service(self) -> Dict[str, Any]:
        """订单簿数据服务配置"""
        return self.get("orderbook_service", {})
    
    # 环境变量配置
    @staticmethod
    def get_api_key() -> Optional[str]:
        """获取 API 密钥"""
        return os.getenv("POLYMARKET_API_KEY")
    
    @staticmethod
    def get_api_secret() -> Optional[str]:
        """获取 API 密钥（如需要）"""
        return os.getenv("POLYMARKET_API_SECRET")
    
    @staticmethod
    def get_api_url() -> str:
        """获取 API 基础 URL"""
        return os.getenv("POLYMARKET_API_URL", "https://polymarket.com")
    
    @staticmethod
    def get_private_key() -> Optional[str]:
        """获取私钥（从环境变量读取）"""
        return os.getenv("POLYMARKET_PRIVATE_KEY")
    
    @staticmethod
    def get_funder_address() -> Optional[str]:
        """获取存款/代理钱包地址（从环境变量 POLYMARKET_PROXY_ADDRESS 读取）"""
        return os.getenv("POLYMARKET_PROXY_ADDRESS")


# 创建全局配置实例
config = Config()
