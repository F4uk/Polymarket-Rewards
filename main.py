"""
Polymarket 自动做市机器人主程序
整合所有组件，实现主循环逻辑
"""
import time
import signal
import sys
import os
import argparse
import threading
from typing import Optional

from api_client import PolymarketAPIClient
from market_manager import MarketManager
from order_manager import OrderManager
from risk_manager import RiskManager
from market_making_strategy import MarketMakingStrategy
# WebSocket 客户端导入已移除
from config import config
from logger import setup_logger

logger = setup_logger("main")

# 全局变量，用于优雅关闭
running = True
shutdown_event = threading.Event()


def signal_handler(signum, frame):
    """信号处理器，用于优雅关闭"""
    global running
    logger.info(f"收到信号 {signum}，准备关闭...")
    running = False
    shutdown_event.set()


# WebSocket 服务检查函数已移除，现在使用 HTTP 接口和 Redis


def stop_all_buy_orders():
    """
    停止模式：取消所有买单后退出
    
    用于 --stop 参数
    """
    logger.info("=" * 60)
    logger.info("停止模式：取消所有买单")
    logger.info("=" * 60)
    
    try:
        # 初始化必要的组件
        logger.info("初始化组件...")
        
        # API 客户端
        api_client = PolymarketAPIClient()
        logger.info("✓ API 客户端初始化成功")
        
        # 做市策略
        strategy = MarketMakingStrategy()
        logger.info("✓ 做市策略初始化成功")
        
        # 风险管理器
        risk_manager = RiskManager()
        logger.info("✓ 风险管理器初始化成功")
        
        # 订单管理器
        order_manager = OrderManager(
            api_client=api_client,
            risk_manager=risk_manager,
            strategy=strategy
        )
        logger.info("✓ 订单管理器初始化成功")
        
        # 取消所有购买挂单
        logger.info("=" * 60)
        logger.info("取消所有购买挂单...")
        logger.info("=" * 60)
        
        cancelled_count = order_manager.cancel_all_buy_orders()
        logger.info(f"已取消 {cancelled_count} 个购买挂单")
        
        # 显示最终统计
        try:
            final_stats = order_manager.get_order_statistics()
            logger.info("最终订单统计:")
            logger.info(f"  - 活跃订单数: {final_stats['active_orders_count']}")
            logger.info(f"  - 活跃市场数: {final_stats['active_markets_count']}")
            logger.info(f"  - 总敞口: {final_stats['total_exposure_usdc']:.2f} USDC")
            logger.info(f"  - 已成交买单数: {final_stats['filled_buy_orders_count']}")
        except Exception as e:
            logger.error(f"获取最终统计失败: {e}")
        
        logger.info("=" * 60)
        logger.info("停止模式完成，程序退出")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"停止模式执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def daemonize():
    """
    将进程转为守护进程（后台运行）
    
    用于 --daemon 参数
    """
    try:
        # 第一次 fork
        pid = os.fork()
        if pid > 0:
            # 父进程退出
            sys.exit(0)
    except OSError as e:
        logger.error(f"第一次 fork 失败: {e}")
        sys.exit(1)
    
    # 脱离父进程的进程组
    os.setsid()
    
    try:
        # 第二次 fork
        pid = os.fork()
        if pid > 0:
            # 父进程退出
            sys.exit(0)
    except OSError as e:
        logger.error(f"第二次 fork 失败: {e}")
        sys.exit(1)
    
    # 改变工作目录
    os.chdir("/")
    
    # 重定向标准输入输出错误
    sys.stdout.flush()
    sys.stderr.flush()
    
    # 关闭文件描述符（可选，根据需要调整）
    # os.close(sys.stdin.fileno())
    # os.close(sys.stdout.fileno())
    # os.close(sys.stderr.fileno())
    
    logger.info("进程已转为守护进程（后台运行）")


def main():
    """主函数"""
    global running
    
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 60)
    logger.info("Polymarket 自动做市机器人启动")
    logger.info("=" * 60)
    
    try:
        # 1. 初始化组件
        logger.info("初始化组件...")
        
        # API 客户端
        api_client = PolymarketAPIClient()
        logger.info("✓ API 客户端初始化成功")
        
        # 做市策略
        strategy = MarketMakingStrategy()
        logger.info("✓ 做市策略初始化成功")
        
        # 风险管理器
        risk_manager = RiskManager()
        logger.info("✓ 风险管理器初始化成功")
        
        # 订单管理器
        order_manager = OrderManager(
            api_client=api_client,
            risk_manager=risk_manager,
            strategy=strategy
        )
        logger.info("✓ 订单管理器初始化成功")
        
        # 市场管理器
        market_manager = MarketManager(api_client)
        logger.info("✓ 市场管理器初始化成功")
        
        # 2. WebSocket 服务已移除，现在使用 HTTP 接口和 Redis 获取订单簿数据
        
        # 2.5. 启动对账：先处理现有订单和持仓，再扫描并挂新 BUY
        logger.info("=" * 60)
        logger.info("启动对账（现有订单/持仓优先于新买单）...")
        logger.info("=" * 60)
        try:
            startup_reconciliation = order_manager.reconcile_startup()
            logger.info(
                f"启动对账结果: 活跃订单={startup_reconciliation['open_orders']}, "
                f"取消遗留买单={startup_reconciliation['buys_cancelled']}, "
                f"导入卖单={startup_reconciliation['sells_imported']}, "
                f"导入持仓={startup_reconciliation['positions_imported']}"
            )
        except Exception as e:
            logger.warning(f"启动对账时发生错误: {e}，继续运行")
        
        # 3. 初始市场扫描和筛选
        logger.info("=" * 60)
        logger.info("开始初始市场扫描和筛选...")
        logger.info("=" * 60)
        
        try:
            # 扫描所有流动性奖励市场
            all_markets = market_manager.scan_rewards_markets()
            logger.info(f"扫描到 {len(all_markets)} 个有流动性奖励的市场")
            
            # 筛选最优市场
            selected_markets = market_manager.filter_markets()
            logger.info(f"筛选出 {len(selected_markets)} 个机会市场")
            
            if not selected_markets:
                logger.warning("未筛选出任何机会市场，程序将退出")
                return
            
            # 显示选中的市场
            logger.info("选中的市场:")
            for i, market in enumerate(selected_markets[:5], 1):  # 只显示前5个
                market_id = market.get("market_id", "N/A")
                question = market.get("question", "N/A")
                reward_ratio = market.get("reward_ratio", 0)
                logger.info(
                    f"  {i}. ID={market_id}, 收益比值={reward_ratio:.6f}, "
                    f"问题={question[:50]}..."
                )
            if len(selected_markets) > 5:
                logger.info(f"  ... 还有 {len(selected_markets) - 5} 个市场")
            
            # 为选中的市场挂单
            # 注意：不再一次性获取所有订单簿数据，而是在每个市场挂单前实时获取
            # 这样可以避免在挂单过程中使用过期的订单簿数据
            logger.info("=" * 60)
            logger.info("开始为机会市场挂单...")
            logger.info("=" * 60)
            
            # 为了向后兼容，仍然准备一个空的订单簿字典作为备用数据源
            # 但 place_market_orders 会优先使用实时获取的数据
            orderbooks_dict = {}
            
            for idx, market in enumerate(selected_markets, 1):
                if not running:
                    break
                
                market_id = market.get("market_id")
                question = market.get("question", "N/A")
                logger.info(f"为市场挂单 ({idx}/{len(selected_markets)}): ID={market_id}, 问题={question[:50]}...")
                
                try:
                    # 每次挂单前实时获取该市场的订单簿数据（作为备用）
                    # place_market_orders 方法会强制实时获取，这里只是作为备用
                    market_orderbooks = api_client.get_markets_orderbooks([market], use_cache=False)
                    orderbooks_dict.update(market_orderbooks)
                    
                    # 挂单（place_market_orders 会强制实时获取最新数据）
                    results = order_manager.place_market_orders(market, orderbooks_dict)
                    success_count = sum(1 for v in results.values() if v)
                    total_count = len(results)
                    logger.info(f"市场 {market_id} 挂单完成: {success_count}/{total_count} 成功")
                    
                    # 每挂完一个市场就检查一次价格调整（防止信息滞后）
                    if success_count > 0:
                        logger.info(f"检查并调整已挂订单价格...")
                        try:
                            active_markets = []
                            for mid in order_manager.get_active_orders().keys():
                                m = order_manager.market_data_cache.get(mid)
                                if m:
                                    active_markets.append(m)
                            
                            if active_markets:
                                adjusted_counts = order_manager.adjust_orders_to_reward_boundaries(active_markets)
                                if adjusted_counts:
                                    total_adjusted = sum(adjusted_counts.values())
                                    logger.info(f"价格调整完成: 共调整 {total_adjusted} 个订单")
                        except Exception as e:
                            logger.warning(f"价格调整检查失败: {e}")
                            
                except Exception as e:
                    logger.error(f"为市场 {market_id} 挂单失败: {e}")
                    import traceback
                    traceback.print_exc()
            
            logger.info("初始挂单完成")
            
            # 初始挂单完成后，立即进行一次价格调整检查
            # 因为在挂单过程中，前面挂的订单价格可能已经偏离了奖励区间边界
            logger.info("=" * 60)
            logger.info("初始挂单完成，立即检查并调整订单价格...")
            logger.info("=" * 60)
            
            try:
                # 获取当前活跃市场的列表
                active_markets = []
                for market_id in order_manager.get_active_orders().keys():
                    market = order_manager.market_data_cache.get(market_id)
                    if market:
                        active_markets.append(market)
                
                if active_markets:
                    logger.info(f"检查 {len(active_markets)} 个市场的订单价格...")
                    adjusted_counts = order_manager.adjust_orders_to_reward_boundaries(active_markets)
                    if adjusted_counts:
                        total_adjusted = sum(adjusted_counts.values())
                        logger.info(f"初始价格调整完成: 共调整 {total_adjusted} 个订单")
                    else:
                        logger.info("所有订单价格都在正确位置，无需调整")
                else:
                    logger.info("当前没有活跃订单，跳过价格调整")
            except Exception as e:
                logger.error(f"初始价格调整失败: {e}")
                import traceback
                traceback.print_exc()
            
        except Exception as e:
            logger.error(f"初始市场扫描和筛选失败: {e}")
            import traceback
            traceback.print_exc()
            return
        
        # 4. 主循环
        logger.info("=" * 60)
        logger.info("进入主循环...")
        logger.info("=" * 60)
        logger.info(f"配置参数:")
        logger.info(f"  - 市场扫描更新间隔: {config.update_interval_seconds} 秒")
        logger.info(f"  - 订单状态检查间隔: {config.order_check_interval_seconds} 秒")
        logger.info(f"  - 订单簿监控更新间隔: {config.orderbook_update_interval_seconds} 秒")
        
        last_market_scan = time.time()
        last_order_check = time.time()
        last_orderbook_update = time.time()
        
        while running:
            try:
                current_time = time.time()
                
                # 4.1 定期检查订单状态（检测成交、补单、对冲卖出）
                if current_time - last_order_check >= config.order_check_interval_seconds:
                    logger.info("-" * 60)
                    logger.info("检查订单状态和持仓对冲...")
                    
                    try:
                        # 检查持仓并挂出对冲卖单（简化逻辑）
                        hedge_results = order_manager.check_positions_and_hedge()
                        if hedge_results:
                            success_count = sum(1 for v in hedge_results.values() if v)
                            logger.info(
                                f"持仓对冲检查完成: {success_count}/{len(hedge_results)} 个token成功挂出对冲卖单"
                            )
                        
                        # 原有的订单检查逻辑（用于补单和清理）
                        filled_orders_by_market = order_manager.check_orders()

                        if filled_orders_by_market:
                            logger.info(
                                f"检测到成交：进入库存退出，不立即补买 "
                                f"({sum(len(v) for v in filled_orders_by_market.values())} 个订单)"
                            )

                        # 库存清仓并冷却结束后，重新执行完整准入后恢复 BUY
                        try:
                            reentry_results = order_manager.maybe_reenter_markets(
                                market_manager.get_selected_markets()
                            )
                            reentered = sum(1 for v in reentry_results.values() if v)
                            if reentered > 0:
                                logger.info(f"重新准入并恢复 BUY: {reentered} 个 token")
                        except Exception as e:
                            logger.warning(f"重新入场检查失败: {e}")
                        
                        # 显示订单统计
                        stats = order_manager.get_order_statistics()
                        logger.info(
                            f"订单统计: 活跃订单={stats['active_orders_count']}, "
                            f"活跃市场={stats['active_markets_count']}, "
                            f"总敞口={stats['total_exposure_usdc']:.2f} USDC, "
                            f"已成交买单={stats['filled_buy_orders_count']}, "
                            f"订阅token={stats['subscribed_tokens_count']}"
                        )
                        
                        last_order_check = current_time
                    except Exception as e:
                        # 使用 try-except 包裹日志记录，防止日志写入失败导致程序卡死
                        try:
                            logger.error(f"检查订单状态失败: {e}")
                            import traceback
                            traceback.print_exc()
                        except Exception as log_error:
                            # 如果日志写入也失败，至少尝试输出到 stderr
                            try:
                                print(f"[错误] 检查订单状态失败: {e}", file=sys.stderr)
                                print(f"[错误] 日志写入也失败: {log_error}", file=sys.stderr)
                                import traceback
                                traceback.print_exc(file=sys.stderr)
                            except:
                                pass  # 如果连 stderr 都无法写入，静默忽略
                        # 继续执行，不中断主循环
                        last_order_check = current_time  # 更新检查时间，避免频繁重试
                
                # 4.2 定期调整订单价格（保持在奖励区间边界）
                if current_time - last_orderbook_update >= config.orderbook_update_interval_seconds:
                    logger.info("-" * 60)
                    logger.info("调整订单价格...")
                    
                    try:
                        # 获取当前活跃市场的列表
                        active_markets = []
                        for market_id in order_manager.get_active_orders().keys():
                            market = order_manager.market_data_cache.get(market_id)
                            if market:
                                active_markets.append(market)
                        
                        if active_markets:
                            adjusted_counts = order_manager.adjust_orders_to_reward_boundaries(active_markets)
                            if adjusted_counts:
                                logger.info(f"订单价格调整完成: {sum(adjusted_counts.values())} 个订单已调整")
                        
                        last_orderbook_update = current_time
                    except Exception as e:
                        # 使用 try-except 包裹日志记录，防止日志写入失败导致程序卡死
                        try:
                            logger.error(f"调整订单价格失败: {e}")
                            import traceback
                            traceback.print_exc()
                        except Exception as log_error:
                            try:
                                print(f"[错误] 调整订单价格失败: {e}", file=sys.stderr)
                                print(f"[错误] 日志写入也失败: {log_error}", file=sys.stderr)
                            except:
                                pass
                        # 继续执行，不中断主循环
                        last_orderbook_update = current_time
                
                # 4.3 定期重新扫描和筛选市场（差量更新模式）
                if current_time - last_market_scan >= config.update_interval_seconds:
                    logger.info("=" * 60)
                    logger.info("重新扫描和筛选市场（差量更新）...")
                    logger.info("=" * 60)
                    
                    try:
                        # 第一步：记录上一轮选择（独立于活跃订单集合）
                        previous_market_ids = market_manager.update_selected_market_ids()
                        logger.info(f"上一轮选择市场数: {len(previous_market_ids)}")

                        # 第二步：扫描所有流动性奖励市场
                        all_markets = market_manager.scan_rewards_markets()
                        logger.info(f"扫描到 {len(all_markets)} 个有流动性奖励的市场")
                        
                        # 第三步：筛选最优市场
                        new_selected_markets = market_manager.filter_markets()
                        logger.info(f"筛选出 {len(new_selected_markets)} 个机会市场")
                        
                        # 第四步：差量更新（retained 保留，removed 只撤 BUY，added 完整准入后挂单）
                        new_market_ids = market_manager.get_selected_market_ids()
                        refresh = order_manager.refresh_market_selection(
                            previous_market_ids,
                            new_market_ids,
                            new_selected_markets,
                        )
                        logger.info(
                            f"市场差量更新: retained={len(refresh['retained'])}, "
                            f"removed={len(refresh['removed'])}, "
                            f"added={len(refresh['added'])}, "
                            f"取消 BUY={refresh['cancelled_buys']}, "
                            f"新挂单市场={refresh['placed_markets']}"
                        )
                        
                        last_market_scan = current_time
                    except Exception as e:
                        # 使用 try-except 包裹日志记录，防止日志写入失败导致程序卡死
                        try:
                            logger.error(f"重新扫描和筛选市场失败: {e}")
                            import traceback
                            traceback.print_exc()
                        except Exception as log_error:
                            try:
                                print(f"[错误] 重新扫描和筛选市场失败: {e}", file=sys.stderr)
                                print(f"[错误] 日志写入也失败: {log_error}", file=sys.stderr)
                            except:
                                pass
                        # 继续执行，不中断主循环
                        last_market_scan = current_time
                
                # 短暂休眠，避免 CPU 占用过高
                time.sleep(1)
                
            except KeyboardInterrupt:
                logger.info("收到键盘中断信号，准备关闭...")
                running = False
                break
            except Exception as e:
                # 使用 try-except 包裹日志记录，防止日志写入失败导致程序卡死
                try:
                    logger.error(f"主循环发生错误: {e}")
                    import traceback
                    traceback.print_exc()
                except Exception as log_error:
                    # 如果日志写入也失败，至少尝试输出到 stderr
                    try:
                        print(f"[错误] 主循环发生错误: {e}", file=sys.stderr)
                        print(f"[错误] 日志写入也失败: {log_error}", file=sys.stderr)
                        import traceback
                        traceback.print_exc(file=sys.stderr)
                    except:
                        pass  # 如果连 stderr 都无法写入，静默忽略
                
                # 继续执行，不中断主循环
                try:
                    time.sleep(5)  # 出错后等待5秒再继续
                except:
                    pass  # 即使 sleep 失败也继续
        
        # 5. 优雅关闭
        logger.info("=" * 60)
        logger.info("正在关闭...")
        logger.info("=" * 60)
        
        # 取消所有购买挂单（做市策略要求）
        logger.info("取消所有购买挂单...")
        try:
            cancelled_count = order_manager.cancel_all_buy_orders()
            logger.info(f"已取消 {cancelled_count} 个购买挂单")
        except Exception as e:
            logger.error(f"取消购买挂单时发生错误: {e}")
        
        # 显示最终统计
        try:
            final_stats = order_manager.get_order_statistics()
            logger.info("最终订单统计:")
            logger.info(f"  - 活跃订单数: {final_stats['active_orders_count']}")
            logger.info(f"  - 活跃市场数: {final_stats['active_markets_count']}")
            logger.info(f"  - 总敞口: {final_stats['total_exposure_usdc']:.2f} USDC")
            logger.info(f"  - 已成交买单数: {final_stats['filled_buy_orders_count']}")
        except Exception as e:
            logger.error(f"获取最终统计失败: {e}")
        
        logger.info("程序已关闭")
        
    except Exception as e:
        logger.error(f"程序运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="Polymarket 自动做市机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python main.py              # 前台运行
  python main.py --stop       # 取消所有买单后退出
  python main.py --daemon     # 后台运行
        """
    )
    
    parser.add_argument(
        "--stop",
        action="store_true",
        help="取消所有买单后退出（不启动主循环）"
    )
    
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="后台运行（守护进程模式）"
    )
    
    args = parser.parse_args()
    
    # 处理 --stop 参数
    if args.stop:
        stop_all_buy_orders()
        sys.exit(0)
    
    # 处理 --daemon 参数
    if args.daemon:
        # 检查是否在 Unix/Linux 系统上
        if hasattr(os, 'fork'):
            daemonize()
        else:
            logger.warning("当前系统不支持守护进程模式，将在前台运行")
            logger.warning("提示：在 Windows 系统上，可以使用 nohup 或任务计划程序实现后台运行")
    
    # 正常前台运行
    main()
