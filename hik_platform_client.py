#!/usr/bin/env python3
"""
海康威视综合安防平台资源与预览客户端
Hikvision Integrated Security Platform Resource & Preview Client

基于 hik_platform_api.py 中的 HikPlatformClient 构建，提供：
- 监控点资源查询（分页、详情、全量拉取）
- 实时预览 URL 获取（单路、批量）
- 预览 URL 转换为 HikConfig / proxy URL 格式

Author: Daikx (代可行)
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from hik_platform_api import HikPlatformClient
from hik_ws_client import HikConfig, parse_proxy_url

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class _LRUCacheItem:
    """LRU 缓存条目，支持 TTL"""

    def __init__(self, value: Any, ttl: float):
        self.value = value
        self.expire_at = time.time() + ttl
        self.access_time = time.time()


class _SimpleLRUCache:
    """简单线程安全内存 LRU 缓存"""

    def __init__(self, maxsize: int = 100, ttl: float = 300.0):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: Dict[str, _LRUCacheItem] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Any:
        """获取缓存值，若过期或不存在返回 None"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            if time.time() > item.expire_at:
                del self._data[key]
                return None
            item.access_time = time.time()
            return item.value

    def set(self, key: str, value: Any) -> None:
        """设置缓存值"""
        with self._lock:
            if len(self._data) >= self.maxsize and key not in self._data:
                # 淘汰最久未访问的条目
                lru_key = min(self._data, key=lambda k: self._data[k].access_time)
                del self._data[lru_key]
            self._data[key] = _LRUCacheItem(value, self.ttl)

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._data.clear()


class HikPlatformResourceClient:
    """海康威视平台资源与预览客户端"""

    def __init__(self, platform_client: HikPlatformClient):
        """
        初始化资源客户端

        Args:
            platform_client: 已初始化的 HikPlatformClient 实例
        """
        self.client = platform_client
        self._camera_cache = _SimpleLRUCache(maxsize=100, ttl=300.0)

    # ------------------------------------------------------------------ #
    # 监控点查询 API
    # ------------------------------------------------------------------ #

    def get_camera_list(
        self,
        page_no: int = 1,
        page_size: int = 1000,
        **filters: Any
    ) -> List[Dict[str, Any]]:
        """
        查询监控点列表（分页）

        Args:
            page_no: 页码，从 1 开始
            page_size: 每页数量，最大 1000
            **filters: 可选过滤条件，如 regionIndexCode、encodeDevIndexCode 等

        Returns:
            监控点字典列表，每个字典包含 cameraIndexCode、cameraName 等字段
        """
        cache_key = f"camera_list:{page_no}:{page_size}:{hash(tuple(sorted(filters.items())))}"
        cached = self._camera_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"Camera list cache hit for page {page_no}")
            return cached

        body: Dict[str, Any] = {
            "pageNo": page_no,
            "pageSize": page_size,
        }
        body.update(filters)

        resp = self.client.post("/api/resource/v1/cameras", data=body)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        camera_list = data.get("list", []) if isinstance(data, dict) else []

        self._camera_cache.set(cache_key, camera_list)
        logger.info(f"Fetched camera list page {page_no}, count={len(camera_list)}")
        return camera_list

    def get_camera_detail(self, camera_index_code: str) -> Dict[str, Any]:
        """
        查询单个监控点详情

        Args:
            camera_index_code: 监控点唯一标识

        Returns:
            监控点详情字典
        """
        resp = self.client.post(
            "/api/resource/v1/camera/detail",
            data={"cameraIndexCode": camera_index_code},
        )
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        logger.info(f"Fetched camera detail: {camera_index_code}")
        return data

    def get_all_cameras(self, **filters: Any) -> List[Dict[str, Any]]:
        """
        获取全部监控点（自动分页）

        Args:
            **filters: 可选过滤条件，同 get_camera_list

        Returns:
            所有监控点字典的扁平列表
        """
        all_cameras: List[Dict[str, Any]] = []
        page_no = 1
        page_size = 1000

        while True:
            page = self.get_camera_list(page_no=page_no, page_size=page_size, **filters)
            if not page:
                break
            all_cameras.extend(page)
            if len(page) < page_size:
                break
            page_no += 1

        logger.info(f"Fetched all cameras, total={len(all_cameras)}")
        return all_cameras

    def clear_camera_cache(self) -> None:
        """清空监控点列表缓存"""
        self._camera_cache.clear()
        logger.info("Camera cache cleared")

    # ------------------------------------------------------------------ #
    # 预览 URL API
    # ------------------------------------------------------------------ #

    def get_preview_url(
        self,
        camera_index_code: str,
        protocol: str = "ws",
        stream_type: int = 0,
    ) -> str:
        """
        获取实时预览 URL

        Args:
            camera_index_code: 监控点唯一标识
            protocol: 协议类型，如 "ws"、"rtsp" 等
            stream_type: 码流类型，0=主码流，1=子码流

        Returns:
            预览 URL 字符串
        """
        body: Dict[str, Any] = {
            "cameraIndexCode": camera_index_code,
            "streamType": stream_type,
            "protocol": protocol,
            "transmode": 1,
        }

        resp = self.client.post("/api/video/v1/cameras/previewURLs", data=body)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        url = data.get("url", "") if isinstance(data, dict) else ""

        if not url:
            logger.warning(f"Preview URL empty for camera {camera_index_code}")
        else:
            logger.info(f"Got preview URL for camera {camera_index_code}: {url[:80]}...")
        return url

    def get_preview_urls(
        self,
        camera_index_codes: List[str],
        **kwargs: Any
    ) -> Dict[str, str]:
        """
        批量获取实时预览 URL

        Args:
            camera_index_codes: 监控点唯一标识列表
            **kwargs: 额外参数，如 protocol、stream_type

        Returns:
            字典，键为 cameraIndexCode，值为对应预览 URL
        """
        results: Dict[str, str] = {}
        for code in camera_index_codes:
            try:
                url = self.get_preview_url(code, **kwargs)
                results[code] = url
            except Exception as e:
                logger.error(f"Failed to get preview URL for {code}: {e}")
                results[code] = ""
        logger.info(f"Batch preview URLs fetched, count={len(results)}")
        return results

    # ------------------------------------------------------------------ #
    # URL 转换
    # ------------------------------------------------------------------ #

    def convert_to_proxy_url(
        self,
        preview_url: str,
        username: str = "admin",
        password: str = "",
    ) -> Dict[str, Any]:
        """
        将平台预览 URL 转换为 HikConfig 兼容字典

        Args:
            preview_url: 平台返回的预览 URL
            username: 设备用户名
            password: 设备密码

        Returns:
            HikConfig 兼容字典，可直接用于 HikMediaClient
        """
        if not preview_url:
            raise ValueError("preview_url is empty")

        # 若已是 wss proxy URL 格式，直接解析
        if preview_url.startswith("wss://") and "/proxy/" in preview_url and "/openUrl/" in preview_url:
            config = parse_proxy_url(preview_url)
            # 覆盖用户名密码
            config.username = username
            config.password = password
            return {
                "proxy_host": config.proxy_host,
                "proxy_port": config.proxy_port,
                "proxy_path": config.proxy_path,
                "device_ip": config.device_ip,
                "device_port": config.device_port,
                "username": config.username,
                "password": config.password,
                "version": config.version,
                "cipher_suites": config.cipher_suites,
            }

        # 处理 RTSP URL，如 rtsp://ip:port/...
        if preview_url.startswith("rtsp://"):
            parsed = urlparse(preview_url)
            device_ip = parsed.hostname or ""
            device_port = parsed.port or 554
            # 使用平台地址作为代理
            platform_host = self.client.platform_host
            # 判断 platform_host 是否包含端口
            if ":" in platform_host:
                proxy_host, proxy_port_str = platform_host.rsplit(":", 1)
                try:
                    proxy_port = int(proxy_port_str)
                except ValueError:
                    proxy_port = 443
            else:
                proxy_host = platform_host
                proxy_port = 443

            # 构造 wss proxy URL
            proxy_path = f"/proxy/{device_ip}:{device_port}/openUrl/{password or username}"
            wss_url = f"wss://{proxy_host}:{proxy_port}{proxy_path}"
            config = parse_proxy_url(wss_url)
            config.username = username
            config.password = password
            return {
                "proxy_host": config.proxy_host,
                "proxy_port": config.proxy_port,
                "proxy_path": config.proxy_path,
                "device_ip": config.device_ip,
                "device_port": config.device_port,
                "username": config.username,
                "password": config.password,
                "version": config.version,
                "cipher_suites": config.cipher_suites,
            }

        # 其他未知格式，尝试直接解析
        logger.warning(f"Unknown preview URL format, attempting generic parse: {preview_url[:80]}...")
        parsed = urlparse(preview_url)
        proxy_host = parsed.hostname or self.client.platform_host
        proxy_port = parsed.port or 443
        device_ip = parsed.hostname or ""
        device_port = parsed.port or 554
        proxy_path = parsed.path or "/"
        return {
            "proxy_host": proxy_host,
            "proxy_port": proxy_port,
            "proxy_path": proxy_path,
            "device_ip": device_ip,
            "device_port": device_port,
            "username": username,
            "password": password,
            "version": "0.1",
            "cipher_suites": 0,
        }
