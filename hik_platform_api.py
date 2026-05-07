#!/usr/bin/env python3
"""
海康威视综合安防平台 OpenAPI 客户端
Hikvision Integrated Security Platform OpenAPI Client

基于 Hikvision OpenAPI 签名认证规范实现

协议流程:
1. 使用 HMAC-SHA256 对请求参数进行签名
2. 将签名信息通过 HTTP Header 发送
3. 平台返回的 token 进行本地缓存复用

Author: Daikx (代可行)
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any, Optional

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class HikPlatformAuth:
    """海康威视平台 OpenAPI 签名认证工具"""

    SIGNATURE_METHOD = "HMAC-SHA256"
    SIGNATURE_VERSION = "V1.0"

    @staticmethod
    def _build_signature_string(params: dict) -> str:
        """
        构建签名字符串
        按参数名 ASCII 码从小到大排序，拼接为 key1=value1&key2=value2...
        """
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        return "&".join(f"{k}={v}" for k, v in sorted_params)

    @classmethod
    def generate_signature(
        cls,
        app_key: str,
        app_secret: str,
        timestamp: str,
        nonce: str
    ) -> str:
        """
        生成 HMAC-SHA256 签名

        Args:
            app_key: 应用 Key
            app_secret: 应用 Secret
            timestamp: ISO 8601 格式时间戳
            nonce: 随机 UUID

        Returns:
            Base64 编码的签名字符串
        """
        params = {
            "signature_method": cls.SIGNATURE_METHOD,
            "signature_version": cls.SIGNATURE_VERSION,
            "app_key": app_key,
            "timestamp": timestamp,
            "nonce": nonce,
        }
        signature_string = cls._build_signature_string(params)
        signature = hmac.new(
            app_secret.encode("utf-8"),
            signature_string.encode("utf-8"),
            hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode("utf-8")

    @classmethod
    def generate_auth_headers(
        cls,
        app_key: str,
        app_secret: str
    ) -> dict:
        """
        生成认证所需的 HTTP Header

        Returns:
            包含签名信息的 Header 字典
        """
        timestamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        # 格式化时区偏移为 +08:00 形式（插入冒号）
        if len(timestamp) >= 5 and timestamp[-5] in "+-":
            timestamp = timestamp[:-2] + ":" + timestamp[-2:]
        # 确保毫秒为 3 位（截断或补齐）
        if "." in timestamp:
            dot_idx = timestamp.index(".")
            tz_idx = timestamp.index("+") if "+" in timestamp[dot_idx:] else timestamp.index("-") if "-" in timestamp[dot_idx:] else len(timestamp)
            ms = timestamp[dot_idx + 1:tz_idx]
            ms = (ms + "000")[:3]
            timestamp = timestamp[:dot_idx + 1] + ms + timestamp[tz_idx:]
        nonce = str(uuid.uuid4())
        signature = cls.generate_signature(app_key, app_secret, timestamp, nonce)

        return {
            "X-Ca-Key": app_key,
            "X-Ca-Signature": signature,
            "X-Ca-Signature-Method": cls.SIGNATURE_METHOD,
            "X-Ca-Signature-Version": cls.SIGNATURE_VERSION,
            "X-Ca-Timestamp": timestamp,
            "X-Ca-Nonce": nonce,
        }


class HikPlatformClient:
    """海康威视综合安防平台 OpenAPI HTTP 客户端"""

    def __init__(
        self,
        platform_host: str,
        app_key: str,
        app_secret: str,
        protocol: str = "https"
    ):
        """
        初始化客户端

        Args:
            platform_host: 平台地址（不含协议和路径）
            app_key: 应用 Key
            app_secret: 应用 Secret
            protocol: 协议类型，默认 https
        """
        self.platform_host = platform_host.rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = f"{protocol}://{self.platform_host}"
        self._token: Optional[str] = None
        self._token_expire_time: Optional[datetime.datetime] = None

    def _get_auth_headers(self) -> dict:
        """获取带签名的认证 Header"""
        return HikPlatformAuth.generate_auth_headers(self.app_key, self.app_secret)

    def _is_token_valid(self) -> bool:
        """检查当前缓存的 token 是否有效"""
        if not self._token or not self._token_expire_time:
            return False
        # 预留 60 秒缓冲时间，避免在边界过期
        return datetime.datetime.now() < (self._token_expire_time - datetime.timedelta(seconds=60))

    def _update_token(self, response_data: dict) -> None:
        """从响应中解析并缓存 token"""
        # 不同接口返回 token 的字段名可能不同，常见有 access_token / token
        token = response_data.get("access_token") or response_data.get("token")
        if not token:
            return

        expire_seconds = response_data.get("expire", response_data.get("expires_in", 0))
        try:
            expire_seconds = int(expire_seconds)
        except (ValueError, TypeError):
            expire_seconds = 0

        self._token = token
        if expire_seconds > 0:
            self._token_expire_time = datetime.datetime.now() + datetime.timedelta(seconds=expire_seconds)
        else:
            self._token_expire_time = None

        logger.info(f"Token cached, expires in {expire_seconds} seconds")

    def _make_request(
        self,
        method: str,
        url: str,
        headers: dict,
        params: Optional[dict] = None,
        data: Optional[dict] = None
    ) -> dict:
        """
        发送 HTTP 请求并处理响应

        Args:
            method: HTTP 方法 (GET/POST 等)
            url: 请求 URL
            headers: 请求头
            params: URL 查询参数
            data: POST 请求体数据

        Returns:
            解析后的 JSON 响应

        Raises:
            requests.HTTPError: HTTP 请求失败
        """
        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            elif method.upper() == "POST":
                resp = requests.post(
                    url,
                    headers=headers,
                    params=params,
                    json=data,
                    timeout=30
                )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # 处理常见 HTTP 错误
            if resp.status_code == 401:
                logger.error("Authentication failed: 401 Unauthorized, please check app_key and app_secret")
                resp.raise_for_status()
            elif resp.status_code == 403:
                logger.error("Access forbidden: 403 Forbidden, please check API permissions")
                resp.raise_for_status()
            elif resp.status_code == 404:
                logger.error(f"API not found: 404 Not Found, url={url}")
                resp.raise_for_status()
            elif resp.status_code >= 500:
                logger.error(f"Server error: {resp.status_code}, please check platform status")
                resp.raise_for_status()

            resp.raise_for_status()

            # 尝试解析 JSON
            try:
                result = resp.json()
            except json.JSONDecodeError:
                logger.warning(f"Response is not valid JSON: {resp.text[:200]}")
                result = {"raw_response": resp.text}

            # 如果响应中包含 token，进行缓存
            if isinstance(result, dict):
                self._update_token(result)

            return result

        except requests.exceptions.Timeout:
            logger.error(f"Request timeout: {method} {url}")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e}")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during request: {e}")
            raise

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None
    ) -> dict:
        """
        发送带签名的 API 请求

        Args:
            method: HTTP 方法 (GET/POST)
            path: API 路径（如 /api/v1/auth/token）
            params: URL 查询参数
            data: POST 请求体数据

        Returns:
            解析后的 JSON 响应
        """
        url = f"{self.base_url}{path}"
        headers = self._get_auth_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        # 如果有缓存的 token，添加到 Header
        if self._is_token_valid() and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
            logger.debug("Using cached token for request")

        logger.info(f"Sending {method} request to {url}")
        logger.debug(f"Headers: {json.dumps({k: v for k, v in headers.items() if 'Signature' not in k}, ensure_ascii=False)}")

        return self._make_request(method, url, headers, params=params, data=data)

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        """发送 GET 请求"""
        return self.request("GET", path, params=params)

    def post(self, path: str, data: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        """发送 POST 请求"""
        return self.request("POST", path, params=params, data=data)

    def clear_token(self) -> None:
        """清除缓存的 token"""
        self._token = None
        self._token_expire_time = None
        logger.info("Token cache cleared")
