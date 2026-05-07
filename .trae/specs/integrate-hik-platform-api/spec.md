# 海康视频平台API集成与WS视频流转发 Spec

## Why

当前项目已实现海康私有WebSocket协议客户端，可以直接从代理URL获取视频流。但用户实际只有海康视频平台（如iVMS-8700、HikCentral等）的地址和API Key，没有直接的设备代理URL。因此需要：
1. 通过海康视频平台OpenAPI获取摄像头点位信息（设备列表、通道号等）
2. 将平台返回的信息转换为本项目所需的WS连接参数
3. 通过本项目已有的WS客户端拉取视频流并对外提供WS视频流服务
4. 从而**不依赖海康官方的WebSocket SDK**，完全使用自研协议栈

## What Changes

- 新增海康视频平台OpenAPI客户端模块（Python）
- 新增摄像头点位信息查询接口封装
- 新增平台URL到本地WS代理的转换逻辑
- 新增WS视频流转发服务端（将海康流转发为标准WS流）
- 新增统一入口脚本，支持从平台地址+key直接启动视频流服务

## Impact

- 新增模块：`hik_platform_api.py`、`hik_platform_client.py`、`ws_server.py`
- 修改模块：`demo.py` 增加平台模式支持
- 依赖新增：`requests`（用于HTTP调用平台API）
- 不影响现有直接URL模式的任何功能

## ADDED Requirements

### Requirement: 平台API认证
The system SHALL 支持海康视频平台OpenAPI的认证方式。

#### Scenario: 成功认证
- **GIVEN** 用户提供了平台地址和AppKey/Secret
- **WHEN** 调用平台API前
- **THEN** 系统应自动计算签名（HMAC-SHA256）并添加到请求头

### Requirement: 摄像头点位查询
The system SHALL 提供获取摄像头点位列表的接口。

#### Scenario: 查询成功
- **GIVEN** 认证通过
- **WHEN** 调用获取监控点资源接口
- **THEN** 返回摄像头列表，包含名称、编码、设备IP、端口等信息

### Requirement: 实时预览URL获取
The system SHALL 支持通过平台API获取实时预览的WS/RTSP URL。

#### Scenario: 获取预览URL
- **GIVEN** 用户指定了摄像头编码
- **WHEN** 调用实时预览接口
- **THEN** 返回可用于本项目连接的代理URL或RTSP地址

### Requirement: WS视频流转发服务
The system SHALL 提供WebSocket服务端，将海康视频流转发给浏览器/前端客户端。

#### Scenario: 客户端连接
- **GIVEN** WS服务端已启动
- **WHEN** 前端通过WebSocket连接到服务端并指定摄像头
- **THEN** 服务端后台通过本项目协议栈连接海康设备，并将视频数据转发给前端

#### Scenario: 多路并发
- **GIVEN** 多个前端客户端连接不同摄像头
- **WHEN** 每路连接独立运行
- **THEN** 各通道互不干扰，支持并发取流

### Requirement: 统一启动入口
The system SHALL 提供统一命令行入口，支持从平台配置直接启动。

#### Scenario: 平台模式启动
- **GIVEN** 用户只有平台地址和key
- **WHEN** 执行 `python server.py --platform-host x.x.x.x --app-key xxx --app-secret xxx`
- **THEN** 自动查询点位、启动WS服务，前端可直接连接观看

## MODIFIED Requirements

### Requirement: 现有URL直连模式
现有通过代理URL直连的模式保持不变，新增平台API模式作为可选路径。

## REMOVED Requirements

无
