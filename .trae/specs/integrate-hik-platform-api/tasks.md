# Tasks

- [x] Task 1: 实现海康平台OpenAPI认证模块
  - [x] SubTask 1.1: 实现签名算法（HMAC-SHA256，符合海康开放平台规范）
  - [x] SubTask 1.2: 封装HTTP请求基础类，自动附加签名头
  - [x] SubTask 1.3: 添加token获取和缓存机制

- [x] Task 2: 实现摄像头点位查询接口
  - [x] SubTask 2.1: 封装获取监控点列表API
  - [x] SubTask 2.2: 封装获取监控点详情API
  - [x] SubTask 2.3: 添加结果缓存和分页支持

- [x] Task 3: 实现实时预览URL获取
  - [x] SubTask 3.1: 封装获取预览URL接口（支持WS/RTSP）
  - [x] SubTask 3.2: 将平台返回的URL转换为本项目可解析的格式

- [x] Task 4: 实现WS视频流转发服务端
  - [x] SubTask 4.1: 基于asyncio/websockets创建WS服务端
  - [x] SubTask 4.2: 实现客户端连接管理（连接、断开、心跳）
  - [x] SubTask 4.3: 集成hik_ws_client，后台拉取海康流并转发
  - [x] SubTask 4.4: 支持多路并发（每个摄像头独立协程）

- [x] Task 5: 实现统一启动入口
  - [x] SubTask 5.1: 创建server.py，支持平台模式和直连模式
  - [x] SubTask 5.2: 添加命令行参数解析
  - [x] SubTask 5.3: 添加配置文件支持（JSON/YAML）

- [x] Task 6: 测试与验证
  - [x] SubTask 6.1: 测试平台API认证和点位查询
  - [x] SubTask 6.2: 测试WS转发服务端基础功能
  - [x] SubTask 6.3: 测试多路并发稳定性

# Task Dependencies

- Task 2 depends on Task 1
- Task 3 depends on Task 2
- Task 4 depends on Task 3
- Task 5 depends on Task 4
- Task 6 depends on Task 5
