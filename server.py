#!/usr/bin/env python3
"""
海康威视 WebSocket 视频流统一入口脚本
Hikvision WebSocket Video Stream Unified Entry Point

支持三种启动模式：
- 平台模式：通过平台 API 自动查询摄像头并启动 WS 中继服务器
- 直连模式：使用给定的代理 URL 直接启动 WS 中继服务器
- 查询模式：仅查询并打印摄像头列表，然后退出

Author: Daikx (代可行)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any, Dict, List, Optional

from hik_platform_api import HikPlatformClient
from hik_platform_client import HikPlatformResourceClient
from ws_server import HikStreamRelayServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def load_config(args: argparse.Namespace) -> Dict[str, Any]:
    """
    加载配置：从 JSON 配置文件读取后，用命令行参数覆盖

    Args:
        args: argparse 解析后的命令行参数

    Returns:
        合并后的配置字典
    """
    config: Dict[str, Any] = {
        "host": "0.0.0.0",
        "port": 8765,
        "platform_host": None,
        "app_key": None,
        "app_secret": None,
        "default_username": "admin",
        "default_password": "",
        "proxy_url": None,
        "camera_index_code": None,
    }

    # 1. 若指定了配置文件，先加载
    if args.config and os.path.isfile(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                file_config = json.load(f)
            if isinstance(file_config, dict):
                config.update(file_config)
                logger.info(f"已加载配置文件: {args.config}")
            else:
                logger.warning(f"配置文件格式不正确，应为 JSON 对象: {args.config}")
        except json.JSONDecodeError as e:
            logger.error(f"配置文件 JSON 解析失败: {e}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"读取配置文件失败: {e}")
            sys.exit(1)

    # 2. 命令行参数覆盖配置文件（仅当参数显式提供时）
    arg_mapping = {
        "host": args.host,
        "port": args.port,
        "platform_host": args.platform_host,
        "app_key": args.app_key,
        "app_secret": args.app_secret,
        "default_username": args.default_username,
        "default_password": args.default_password,
        "proxy_url": args.proxy_url,
        "camera_index_code": args.camera_index_code,
    }

    for key, value in arg_mapping.items():
        if value is not None:
            config[key] = value

    return config


def query_cameras(platform_client: HikPlatformResourceClient) -> List[Dict[str, Any]]:
    """
    查询并返回摄像头列表

    Args:
        platform_client: 已初始化的平台资源客户端

    Returns:
        监控点字典列表
    """
    try:
        cameras = platform_client.get_all_cameras()
        logger.info(f"共查询到 {len(cameras)} 个监控点")
        return cameras
    except Exception as e:
        logger.error(f"查询监控点列表失败: {e}")
        return []


def print_cameras_table(cameras: List[Dict[str, Any]]) -> None:
    """
    以表格形式打印摄像头列表到控制台

    Args:
        cameras: 监控点字典列表
    """
    if not cameras:
        print("未查询到任何监控点。")
        return

    # 计算每列最大宽度
    headers = ["序号", "监控点编号", "监控点名称", "类型", "区域"]
    rows: List[List[str]] = []
    for idx, cam in enumerate(cameras, start=1):
        rows.append([
            str(idx),
            str(cam.get("cameraIndexCode", "")),
            str(cam.get("cameraName", "")),
            str(cam.get("cameraTypeName", "")),
            str(cam.get("regionName", "")),
        ])

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # 打印分隔线
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    def fmt_line(cells: List[str]) -> str:
        return "| " + " | ".join(
            cell.ljust(widths[i]) for i, cell in enumerate(cells)
        ) + " |"

    print(sep)
    print(fmt_line(headers))
    print(sep)
    for row in rows:
        print(fmt_line(row))
    print(sep)


async def run_server(config: Dict[str, Any]) -> None:
    """
    启动 WebSocket 中继服务器

    Args:
        config: 服务器配置字典
    """
    server = HikStreamRelayServer(config)

    # 注册信号处理器以实现优雅关闭
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    try:
        await server.start()
    except asyncio.CancelledError:
        logger.info("服务器任务被取消")
    finally:
        await server.stop()


async def main() -> None:
    """主入口函数"""
    parser = argparse.ArgumentParser(
        description="海康威视 WebSocket 视频流统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 平台模式（自动查询摄像头并启动服务器）
  python server.py --platform-host x.x.x.x --app-key xxx --app-secret xxx

  # 直连模式（使用给定代理 URL 启动服务器）
  python server.py --proxy-url "wss://..."

  # 仅查询摄像头列表并退出
  python server.py --platform-host x.x.x.x --app-key xxx --app-secret xxx --query-cameras

  # 使用配置文件启动
  python server.py --config config.json

  # 指定默认摄像头快速测试
  python server.py --platform-host x.x.x.x --app-key xxx --app-secret xxx --camera-index-code xxx
        """
    )

    # 模式参数
    parser.add_argument("--config", default=None, help="JSON 配置文件路径")
    parser.add_argument("--platform-host", default=None, help="平台地址（不含协议和路径）")
    parser.add_argument("--app-key", default=None, help="应用 Key")
    parser.add_argument("--app-secret", default=None, help="应用 Secret")
    parser.add_argument("--proxy-url", default=None, help="直连模式代理 URL（wss://...）")

    # 服务器参数
    parser.add_argument("--host", default=None, help="WS 服务器监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="WS 服务器监听端口 (默认: 8765)")
    parser.add_argument("--default-username", default=None, help="默认设备用户名 (默认: admin)")
    parser.add_argument("--default-password", default=None, help="默认设备密码 (默认: 空)")

    # 功能开关
    parser.add_argument("--query-cameras", action="store_true", help="仅查询摄像头列表并退出")
    parser.add_argument("--list-cameras", action="store_true", help="以表格形式打印摄像头列表")
    parser.add_argument("--camera-index-code", default=None, help="预选择默认摄像头编号（用于快速测试）")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 加载并合并配置
    config = load_config(args)

    # 判断运行模式
    has_platform = bool(config.get("platform_host") and config.get("app_key") and config.get("app_secret"))
    has_proxy_url = bool(config.get("proxy_url"))

    # 查询模式：仅查询摄像头并退出
    if args.query_cameras:
        if not has_platform:
            logger.error("--query-cameras 需要平台参数: --platform-host, --app-key, --app-secret")
            sys.exit(1)

        http_client = HikPlatformClient(
            platform_host=config["platform_host"],
            app_key=config["app_key"],
            app_secret=config["app_secret"],
        )
        platform_client = HikPlatformResourceClient(http_client)
        cameras = query_cameras(platform_client)

        if args.list_cameras:
            print_cameras_table(cameras)
        else:
            for cam in cameras:
                print(f"{cam.get('cameraIndexCode')} | {cam.get('cameraName')} | {cam.get('cameraTypeName')} | {cam.get('regionName')}")
        sys.exit(0)

    # 平台模式：查询摄像头并启动服务器
    if has_platform:
        http_client = HikPlatformClient(
            platform_host=config["platform_host"],
            app_key=config["app_key"],
            app_secret=config["app_secret"],
        )
        platform_client = HikPlatformResourceClient(http_client)
        cameras = query_cameras(platform_client)

        if args.list_cameras:
            print_cameras_table(cameras)
        else:
            for cam in cameras:
                logger.info(
                    f"监控点: {cam.get('cameraIndexCode')} | {cam.get('cameraName')} | "
                    f"{cam.get('cameraTypeName')} | {cam.get('regionName')}"
                )

        if not cameras:
            logger.warning("未查询到任何监控点，服务器将以空列表启动")

        # 若指定了默认摄像头，打印提示
        if config.get("camera_index_code"):
            logger.info(f"已预选择默认摄像头: {config['camera_index_code']}")

        await run_server(config)
        return

    # 直连模式：使用 proxy_url 启动服务器
    if has_proxy_url:
        logger.info(f"直连模式启动，代理 URL: {config['proxy_url']}")
        await run_server(config)
        return

    # 没有任何有效模式
    logger.error("未提供有效的启动参数。请提供平台参数 (--platform-host, --app-key, --app-secret) 或 --proxy-url")
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
