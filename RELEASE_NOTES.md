# 版本发布说明

## v3.0.0（2026-11-21）

### 新增
- **多房间**：所有人可建、所有人可见；消息、已读水位、在线人数按房间隔离
- **图片消息**：客户端压缩后上传（最长边 1600px、≤1.5MB），独立端点读取 + 版本号长缓存
- **自定义头像**：按设备保存 128px，版本号变化后所有端自动刷新
- **微信风界面**：会话列表 + 绿色气泡 + 图片气泡（点开看大图），桌面双栏 / 手机两屏

### 底层
- 存储从 JSON 换成 **SQLite**（`chat.db`），schema 升级到 v2，旧数据自动迁入「大厅」
- 新增表 `rooms` / `avatars` / `images`；`messages` 增加 `room_id` / `kind`；
  `read_positions` 改成 (`device_id`, `room_id`) 复合主键
- 安卓 App 的 WebView 支持选择图片（`onShowFileChooser`，无需存储权限）
- 修复 Windows 下 `SO_REUSEADDR` 导致多进程同时监听 9999 的问题

### 下载
- 安卓安装包：见本 Release 的 Assets `chatroom-v3.apk`
- 服务器与网页：下载 Source code (zip)，或 `git clone`

### 安装
1. 解压后双击 `启动聊天室.bat`（Windows，需要 Python 3.11+）
2. 手机装 APK → 与电脑连同一 WiFi → 打开 App 点「自动查找」
3. 若连不上：管理员执行
   ```
   netsh advfirewall firewall add rule name="ChatRoom 9999 TCP" dir=in action=allow protocol=TCP localport=9999
   netsh advfirewall firewall add rule name="ChatRoom 9999 UDP" dir=in action=allow protocol=UDP localport=9999
   ```

### 测试
`python tests/test_api.py -v` → 21 组全部通过（含房间隔离、图片、头像、数据库升级）。
