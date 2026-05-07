#!/usr/bin/env python3
"""
海康威视 WebSocket 视频流转发服务器
Hikvision WebSocket Video Stream Relay Server

提供 HTTP + WebSocket 混合服务：
- HTTP GET /cameras      查询平台监控点列表（平台模式）
- HTTP GET /health       健康检查
- WS   /ws/stream        视频流 WebSocket 端点

架构：
- HikStreamRelayServer 管理 WebSocket 服务器生命周期
- 每个摄像头流由 StreamSession 管理，支持多客户端共享
- 使用 asyncio + websockets 实现高并发

Author: Daikx (代可行)
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.legacy.server import WebSocketServerProtocol

from hik_platform_api import HikPlatformClient
from hik_platform_client import HikPlatformResourceClient
from hik_ws_client import HikConfig, HikMediaClient, parse_proxy_url

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class ClientConnection:
    """WebSocket 客户端连接上下文"""
    ws: WebSocketServerProtocol
    camera_index_code: Optional[str] = None
    stream_type: int = 0
    connected_at: float = field(default_factory=time.time)
    last_ping_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None


@dataclass
class StreamSession:
    """摄像头流会话，支持多客户端共享"""
    camera_index_code: str
    stream_type: int
    config: HikConfig
    clients: Set[WebSocketServerProtocol] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    media_client: Optional[HikMediaClient] = None
    created_at: float = field(default_factory=time.time)
    frame_count: int = 0
    error_count: int = 0
    last_frame_at: Optional[float] = None
    stopped: bool = False


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #

class StreamError(Exception):
    """流处理错误"""
    pass


class AuthenticationError(Exception):
    """认证错误"""
    pass


# --------------------------------------------------------------------------- #
# 共享流管理器
# --------------------------------------------------------------------------- #

class StreamManager:
    """
    管理活跃的摄像头流会话

    支持多客户端共享同一个 HikMediaClient，通过引用计数控制生命周期。
    """

    def __init__(self, default_username: str = "admin", default_password: str = ""):
        self._streams: Dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()
        self.default_username = default_username
        self.default_password = default_password

    def _make_key(self, camera_index_code: str, stream_type: int) -> str:
        """生成流唯一键"""
        return f"{camera_index_code}:{stream_type}"

    async def add_client(
        self,
        ws: WebSocketServerProtocol,
        camera_index_code: str,
        stream_type: int,
        config: HikConfig,
    ) -> StreamSession:
        """
        将客户端添加到流会话，如果不存在则创建新会话

        Args:
            ws: WebSocket 客户端连接
            camera_index_code: 监控点唯一标识
            stream_type: 码流类型
            config: HikConfig 配置

        Returns:
            StreamSession 对象
        """
        key = self._make_key(camera_index_code, stream_type)

        async with self._lock:
            session = self._streams.get(key)
            if session is None or session.stopped:
                session = StreamSession(
                    camera_index_code=camera_index_code,
                    stream_type=stream_type,
                    config=config,
                )
                self._streams[key] = session
                logger.info(f"创建新流会话: {key}")

            session.clients.add(ws)
            logger.info(f"客户端 {ws.remote_address} 加入流 {key}, 当前客户端数: {len(session.clients)}")
            return session

    async def remove_client(self, ws: WebSocketServerProtocol, camera_index_code: str, stream_type: int) -> None:
        """
        从流会话中移除客户端，引用计数归零时停止流

        Args:
            ws: WebSocket 客户端连接
            camera_index_code: 监控点唯一标识
            stream_type: 码流类型
        """
        key = self._make_key(camera_index_code, stream_type)

        async with self._lock:
            session = self._streams.get(key)
            if session is None:
                return

            if ws in session.clients:
                session.clients.discard(ws)
                logger.info(f"客户端 {ws.remote_address} 离开流 {key}, 剩余客户端数: {len(session.clients)}")

            if not session.clients and not session.stopped:
                session.stopped = True
                logger.info(f"流 {key} 无客户端，准备停止")
                if session.task and not session.task.done():
                    session.task.cancel()
                if session.media_client:
                    try:
                        await session.media_client.close()
                    except Exception as e:
                        logger.warning(f"关闭 media_client 异常: {e}")
                self._streams.pop(key, None)

    async def stop_all(self) -> None:
        """停止所有流会话"""
        async with self._lock:
            for key, session in list(self._streams.items()):
                session.stopped = True
                if session.task and not session.task.done():
                    session.task.cancel()
                if session.media_client:
                    try:
                        await session.media_client.close()
                    except Exception as e:
                        logger.warning(f"关闭 media_client 异常: {e}")
            self._streams.clear()
            logger.info("所有流会话已停止")


# --------------------------------------------------------------------------- #
# WebSocket 视频流转发服务器
# --------------------------------------------------------------------------- #

class HikStreamRelayServer:
    """
    海康威视 WebSocket 视频流转发服务器

    功能：
    - 接受浏览器/前端 WebSocket 连接
    - 支持平台模式（通过 HikPlatformResourceClient 获取预览 URL）
    - 支持直连模式（客户端提供代理 URL）
    - 多客户端共享同一摄像头流
    - 自动重连（最多 3 次）
    - 心跳保活
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化服务器

        Args:
            config: 配置字典，包含：
                - host: WS 服务器监听地址
                - port: WS 服务器监听端口
                - platform_host: 平台地址（可选）
                - app_key: 应用 Key（可选）
                - app_secret: 应用 Secret（可选）
                - default_username: 默认设备用户名
                - default_password: 默认设备密码
        """
        self.host = config.get("host", "0.0.0.0")
        self.port = config.get("port", 8765)
        self.platform_host = config.get("platform_host")
        self.app_key = config.get("app_key")
        self.app_secret = config.get("app_secret")
        self.default_username = config.get("default_username", "admin")
        self.default_password = config.get("default_password", "")

        self._platform_client: Optional[HikPlatformResourceClient] = None
        self._stream_manager = StreamManager(
            default_username=self.default_username,
            default_password=self.default_password,
        )
        self._clients: Dict[WebSocketServerProtocol, ClientConnection] = {}
        self._server = None
        self._running = False

        # 心跳配置
        self.ping_interval = 30.0
        self.ping_timeout = 10.0

        # 重连配置
        self.max_retries = 3
        self.retry_delay = 2.0

        self._init_platform_client()

    def _init_platform_client(self) -> None:
        """初始化平台客户端（如果配置了平台参数）"""
        if self.platform_host and self.app_key and self.app_secret:
            try:
                http_client = HikPlatformClient(
                    platform_host=self.platform_host,
                    app_key=self.app_key,
                    app_secret=self.app_secret,
                )
                self._platform_client = HikPlatformResourceClient(http_client)
                logger.info(f"平台客户端初始化成功: {self.platform_host}")
            except Exception as e:
                logger.error(f"平台客户端初始化失败: {e}")
                self._platform_client = None
        else:
            logger.info("未配置平台参数，以直连模式运行")

    # ------------------------------------------------------------------ #
    # HTTP 处理器
    # ------------------------------------------------------------------ #

    async def _handle_http(self, path: str, request_headers) -> tuple:
        """
        处理 HTTP 请求（websockets 库支持的 HTTP 升级前拦截）

        由于 websockets 库主要处理 WS，HTTP 端点通过独立的 aiohttp 风格
        或在本模块中通过 process_request 处理。这里我们使用简单的路径分发。
        """
        return None

    async def _http_health(self) -> bytes:
        """健康检查响应"""
        health = {
            "status": "ok",
            "timestamp": time.time(),
            "clients": len(self._clients),
            "streams": len(self._stream_manager._streams),
        }
        return json.dumps(health, ensure_ascii=False).encode("utf-8")

    async def _http_cameras(self) -> bytes:
        """获取监控点列表（平台模式）"""
        if self._platform_client is None:
            error = {"error": "平台客户端未初始化，无法获取监控点列表"}
            return json.dumps(error, ensure_ascii=False).encode("utf-8")

        try:
            cameras = self._platform_client.get_all_cameras()
            result = [
                {
                    "cameraIndexCode": c.get("cameraIndexCode"),
                    "cameraName": c.get("cameraName"),
                    "cameraTypeName": c.get("cameraTypeName"),
                    "regionName": c.get("regionName"),
                }
                for c in cameras
                if c.get("cameraIndexCode")
            ]
            return json.dumps({"data": result}, ensure_ascii=False).encode("utf-8")
        except Exception as e:
            logger.error(f"获取监控点列表失败: {e}")
            error = {"error": f"获取监控点列表失败: {str(e)}"}
            return json.dumps(error, ensure_ascii=False).encode("utf-8")

    # ------------------------------------------------------------------ #
    # WebSocket 处理器
    # ------------------------------------------------------------------ #

    async def _send_json(self, ws: WebSocketServerProtocol, data: dict) -> None:
        """发送 JSON 消息到客户端"""
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"发送 JSON 消息失败: {e}")

    async def _send_error(self, ws: WebSocketServerProtocol, message: str, code: int = 400) -> None:
        """发送错误消息并关闭连接"""
        await self._send_json(ws, {"type": "error", "code": code, "message": message})

    async def _handle_client(self, ws: WebSocketServerProtocol, path: str) -> None:
        """
        处理 WebSocket 客户端连接

        流程：
        1. 接收首条 JSON 消息，解析 cameraIndexCode 和 streamType
        2. 根据模式获取预览 URL 或直接解析代理 URL
        3. 创建/加入流会话
        4. 启动后台任务拉取视频流并转发
        5. 循环接收客户端消息（心跳、控制命令等）
        """
        remote = ws.remote_address
        logger.info(f"客户端连接: {remote}, path={path}")

        conn = ClientConnection(ws=ws)
        self._clients[ws] = conn

        try:
            # 等待首条消息（带超时）
            try:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                await self._send_error(ws, "等待初始化消息超时，请发送 cameraIndexCode")
                return

            # 解析初始化消息
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await self._send_error(ws, "首条消息必须是 JSON 格式")
                return

            camera_index_code = msg.get("cameraIndexCode")
            stream_type = msg.get("streamType", 0)
            proxy_url = msg.get("proxyUrl")

            if not camera_index_code and not proxy_url:
                await self._send_error(ws, "必须提供 cameraIndexCode 或 proxyUrl")
                return

            conn.camera_index_code = camera_index_code or "direct"
            conn.stream_type = stream_type

            # 获取 HikConfig
            if proxy_url:
                # 直连模式
                try:
                    hik_config = parse_proxy_url(proxy_url)
                    hik_config.username = self.default_username
                    hik_config.password = self.default_password
                except Exception as e:
                    logger.error(f"解析 proxyUrl 失败: {e}")
                    await self._send_error(ws, f"解析 proxyUrl 失败: {e}")
                    return
            elif self._platform_client:
                # 平台模式
                try:
                    preview_url = self._platform_client.get_preview_url(
                        camera_index_code=camera_index_code,
                        stream_type=stream_type,
                        protocol="ws",
                    )
                    if not preview_url:
                        await self._send_error(ws, f"未获取到摄像头 {camera_index_code} 的预览 URL")
                        return

                    config_dict = self._platform_client.convert_to_proxy_url(
                        preview_url=preview_url,
                        username=self.default_username,
                        password=self.default_password,
                    )
                    hik_config = HikConfig(**config_dict)
                except Exception as e:
                    logger.error(f"获取预览 URL 失败: {e}")
                    await self._send_error(ws, f"获取预览 URL 失败: {e}")
                    return
            else:
                await self._send_error(ws, "未配置平台客户端且未提供 proxyUrl，无法建立流")
                return

            # 加入流会话
            session = await self._stream_manager.add_client(
                ws=ws,
                camera_index_code=conn.camera_index_code,
                stream_type=stream_type,
                config=hik_config,
            )

            # 如果是第一个客户端，启动后台拉流任务
            if session.task is None or session.task.done():
                session.task = asyncio.create_task(
                    self._stream_worker(session)
                )

            # 发送就绪消息
            await self._send_json(ws, {
                "type": "ready",
                "cameraIndexCode": conn.camera_index_code,
                "streamType": stream_type,
            })

            # 客户端消息循环（处理心跳、控制命令）
            async for message in ws:
                if isinstance(message, str):
                    try:
                        client_msg = json.loads(message)
                        msg_type = client_msg.get("type")
                        if msg_type == "ping":
                            await self._send_json(ws, {"type": "pong", "timestamp": time.time()})
                        elif msg_type == "switchStream":
                            # 支持切换码流（先断开当前，重新初始化）
                            await self._send_json(ws, {"type": "info", "message": "切换码流请重新连接"})
                        else:
                            logger.debug(f"收到客户端消息: {client_msg}")
                    except json.JSONDecodeError:
                        pass
                else:
                    # 忽略客户端发送的二进制数据
                    pass

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"客户端断开: {remote}")
        except Exception as e:
            logger.error(f"客户端处理异常: {e}")
            logger.debug(traceback.format_exc())
        finally:
            # 清理
            await self._cleanup_client(ws)

    async def _cleanup_client(self, ws: WebSocketServerProtocol) -> None:
        """清理客户端连接"""
        conn = self._clients.pop(ws, None)
        if conn and conn.camera_index_code:
            await self._stream_manager.remove_client(
                ws=ws,
                camera_index_code=conn.camera_index_code,
                stream_type=conn.stream_type,
            )
        logger.info(f"客户端清理完成: {ws.remote_address}")

    # ------------------------------------------------------------------ #
    # 流工作线程
    # ------------------------------------------------------------------ #

    async def _stream_worker(self, session: StreamSession) -> None:
        """
        后台任务：通过 HikMediaClient 拉取视频流并广播给所有客户端

        支持自动重连（最多 max_retries 次）。
        """
        camera_key = self._stream_manager._make_key(session.camera_index_code, session.stream_type)
        logger.info(f"流工作线程启动: {camera_key}")

        retry_count = 0
        while not session.stopped and retry_count <= self.max_retries:
            try:
                await self._run_media_client(session)
                # 正常结束（无客户端或主动停止）
                break
            except asyncio.CancelledError:
                logger.info(f"流工作线程取消: {camera_key}")
                raise
            except Exception as e:
                retry_count += 1
                logger.error(f"流 {camera_key} 异常 (重试 {retry_count}/{self.max_retries}): {e}")
                if retry_count > self.max_retries:
                    logger.error(f"流 {camera_key} 达到最大重试次数，停止")
                    await self._broadcast_to_clients(session, {
                        "type": "error",
                        "code": 500,
                        "message": f"流异常终止: {e}",
                    })
                    break
                await asyncio.sleep(self.retry_delay * retry_count)

        session.stopped = True
        logger.info(f"流工作线程结束: {camera_key}, 总帧数: {session.frame_count}")

    async def _run_media_client(self, session: StreamSession) -> None:
        """运行单个 HikMediaClient 实例"""
        camera_key = self._stream_manager._make_key(session.camera_index_code, session.stream_type)

        media_client = HikMediaClient(session.config)
        session.media_client = media_client

        # 设置回调
        def on_video_data(data: bytes) -> None:
            asyncio.create_task(self._on_video_frame(session, data))

        def on_error(error_msg: str) -> None:
            asyncio.create_task(self._on_stream_error(session, error_msg))

        media_client.on_video_data = on_video_data
        media_client.on_error = on_error

        try:
            await media_client.run()
        finally:
            await media_client.close()
            session.media_client = None

    async def _on_video_frame(self, session: StreamSession, data: bytes) -> None:
        """视频帧到达回调：广播给所有客户端"""
        if session.stopped:
            return

        session.frame_count += 1
        session.last_frame_at = time.time()

        # 使用二进制帧发送视频数据
        dead_clients = set()
        for ws in list(session.clients):
            try:
                await ws.send(data)
            except websockets.exceptions.ConnectionClosed:
                dead_clients.add(ws)
            except Exception as e:
                logger.warning(f"发送视频帧失败: {e}")
                dead_clients.add(ws)

        # 清理已断开客户端
        for ws in dead_clients:
            await self._stream_manager.remove_client(
                ws=ws,
                camera_index_code=session.camera_index_code,
                stream_type=session.stream_type,
            )

    async def _on_stream_error(self, session: StreamSession, error_msg: str) -> None:
        """流错误回调"""
        session.error_count += 1
        logger.error(f"流错误 [{session.camera_index_code}]: {error_msg}")
        await self._broadcast_to_clients(session, {
            "type": "error",
            "code": 502,
            "message": error_msg,
        })

    async def _broadcast_to_clients(self, session: StreamSession, message: dict) -> None:
        """向流会话中的所有客户端广播 JSON 消息"""
        dead_clients = set()
        for ws in list(session.clients):
            try:
                await ws.send(json.dumps(message, ensure_ascii=False))
            except websockets.exceptions.ConnectionClosed:
                dead_clients.add(ws)
            except Exception as e:
                logger.warning(f"广播消息失败: {e}")
                dead_clients.add(ws)

        for ws in dead_clients:
            await self._stream_manager.remove_client(
                ws=ws,
                camera_index_code=session.camera_index_code,
                stream_type=session.stream_type,
            )

    # ------------------------------------------------------------------ #
    # 服务器生命周期
    # ------------------------------------------------------------------ #

    async def _process_request(self, path: str, request_headers) -> Optional[tuple]:
        """
        处理 HTTP 请求（在 WebSocket 升级前拦截）

        websockets 库支持通过 process_request 处理纯 HTTP 请求。
        返回 (status, headers, body) 表示 HTTP 响应，返回 None 继续 WS 升级。
        """
        if path == "/health":
            body = await self._http_health()
            return (
                200,
                [("Content-Type", "application/json; charset=utf-8"), ("Access-Control-Allow-Origin", "*")],
                body,
            )

        if path == "/cameras":
            body = await self._http_cameras()
            return (
                200,
                [("Content-Type", "application/json; charset=utf-8"), ("Access-Control-Allow-Origin", "*")],
                body,
            )

        # 继续 WebSocket 升级
        return None

    async def start(self) -> None:
        """启动服务器"""
        self._running = True
        logger.info(f"启动服务器: {self.host}:{self.port}")

        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            process_request=self._process_request,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
        )

        logger.info(f"服务器已启动，监听: {self.host}:{self.port}")
        logger.info(f"  WebSocket 端点: ws://{self.host}:{self.port}/ws/stream")
        logger.info(f"  HTTP 健康检查: http://{self.host}:{self.port}/health")
        logger.info(f"  HTTP 监控点列表: http://{self.host}:{self.port}/cameras")

        await self._server.wait_closed()

    async def stop(self) -> None:
        """停止服务器"""
        self._running = False
        logger.info("正在停止服务器...")

        # 关闭所有客户端连接
        close_tasks = []
        for ws in list(self._clients.keys()):
            close_tasks.append(ws.close())
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        # 停止所有流
        await self._stream_manager.stop_all()

        # 关闭服务器
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("服务器已停止")


# --------------------------------------------------------------------------- #
# 入口函数
# --------------------------------------------------------------------------- #

async def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="海康威视 WebSocket 视频流转发服务器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 直连模式（无需平台）
  python3 ws_server.py --host 0.0.0.0 --port 8765

  # 平台模式
  python3 ws_server.py --host 0.0.0.0 --port 8765 \\
      --platform-host platform.example.com \\
      --app-key YOUR_APP_KEY \\
      --app-secret YOUR_APP_SECRET
        """
    )

    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认: 8765)")
    parser.add_argument("--platform-host", default=None, help="平台地址")
    parser.add_argument("--app-key", default=None, help="应用 Key")
    parser.add_argument("--app-secret", default=None, help="应用 Secret")
    parser.add_argument("--default-username", default="admin", help="默认设备用户名")
    parser.add_argument("--default-password", default="", help="默认设备密码")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = {
        "host": args.host,
        "port": args.port,
        "platform_host": args.platform_host,
        "app_key": args.app_key,
        "app_secret": args.app_secret,
        "default_username": args.default_username,
        "default_password": args.default_password,
    }

    server = HikStreamRelayServer(config)

    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
