# 安卓 App 说明

安装包：**`android/build/chatroom.apk`**（约 21 KB，已签名，可直接安装）

## 1. 它是什么

一个**内嵌 WebView 的安卓 App**：打开后全屏显示电脑上跑着的聊天室网页。

```
手机 App（WebView 全屏）
        │  http://192.168.1.209:9999
        ▼
电脑上的 server.py（SQLite 存数据）
```

所以：

- 网页上有的功能，App 全都有：已读、撤回、编辑、未读、历史、断线重连……
- 改界面只需要改 `static/` 里的网页，**不用重新打 APK**
- App 本身只有 3 个东西：一个 WebView、一个“填服务器地址”的对话框、一个连不上时的错误页

它是一个**正常启动的普通 App**：桌面有图标、点开就用，不做隐藏、不伪装、不后台偷跑。

## 2. 安装

1. 把 `android/build/chatroom.apk` 传到手机（数据线 / 微信文件传输 / 网盘均可）
2. 手机上点开这个文件
3. 系统提示「为了安全，你的手机不允许安装此来源的应用」→ 去设置里允许「安装未知来源应用」→ 返回继续安装
4. 桌面出现「聊天室」图标

> 需要 Android 7.0（API 24）及以上。

## 3. 第一次打开：连服务器

1. 先在电脑上双击 `启动聊天室.bat`，黑窗口保持开着
2. 打开 App，点 **「自动查找」** —— App 会在局域网里广播一句「谁是聊天室服务器」，
   电脑上的服务器会应答，地址自动填好并连上（**电脑换了 IP 也不用管**）
3. 如果自动查找失败（路由器拦广播 / 防火墙挡了 UDP），就手动填：
   看黑窗口里「局域网访问」那一行，例如 `http://172.20.10.13:9999`，输入 `172.20.10.13:9999`
4. 点「测试连接」确认能通 →「保存并连接」

地址会记在手机里，以后打开 App 直接进。

## 3.5 在 App 里发照片 / 换头像（v3）

- 聊天输入框左边有个 **「＋」**：点它 → 系统相册/文件选择器 → 选一张图，App 会自动压缩后发出去
- 左下角点自己的**头像** → 选一张图 → 自动裁成方形并压缩成 128px
- 用的是系统的文件选择器（SAF），**不需要任何存储权限**，也不会读你的相册
- 如果点了「＋」没反应：说明装的是旧版 APK，装最新的 `chatroom.apk` 即可

## 4. 换 WiFi / IP 变了怎么办

**基本不用管**：App 连不上已保存的地址时，会**自动在局域网里找一次服务器**，找到就自动改地址并连上。
（比如从寝室的热点换到教室 WiFi，打开 App 就行。）

- 自动查找失败时，点错误页上的 **「自动查找服务器」** 或 **「修改服务器地址」** 手动填
- 手动改：右上角菜单 → **服务器设置**

> 注意：黑窗口里打印的地址是**启动那一刻**的，换网络后会过时；服务器本身不用重启，
> 以 App 自动查找的结果为准。
>
> 为什么能自动找到：服务器在 UDP 9999 上应答一个固定格式的广播，
> App 收到后用「发件人 IP + 返回的端口」拼出地址。

## 5. 装不上 / 连不上排查

| 现象 | 原因 |
| --- | --- |
| 解析包出错 / 安装失败 | 传输过程中文件损坏（微信有时会改后缀），用数据线或网盘重传；确认手机是 Android 7+ |
| 提示「应用未安装」 | 手机里已有同包名但签名不同的旧版本，先卸载旧版 |
| **白屏、什么都没有** | ①服务器没在运行（必须是你自己双击 bat 开的黑窗口，不能是临时后台进程）②App 里存的还是旧 IP ③新旧版 App 不同：新版会一直转圈「正在连接」并在 12 秒后显示错误页，旧版才会白屏——装新版即可 |
| App 显示「连不上服务器」 | ①手机和电脑不在同一网络 ②电脑黑窗口没开 ③地址填错 ④Windows 防火墙没放行 9999 |
| 测试连接提示超时 | 多半是防火墙或不同网段（比如手机用 4G/5G，电脑连的是别的 WiFi） |
| 连上了但页面空白 | 电脑上的 `static/` 文件缺失，确认项目完整；或在菜单里点「重新加载」 |
| 切到后台一会儿显示离线 | 安卓限速后台网页定时器，20 秒没心跳会临时离线，切回来自动恢复 |
| 没有消息通知 | 本来就没做推送：App 是网页外壳，靠页面轮询 |

防火墙放行（**管理员**命令行执行一次；两条都要）：

```
netsh advfirewall firewall add rule name="ChatRoom 9999 TCP" dir=in action=allow protocol=TCP localport=9999
netsh advfirewall firewall add rule name="ChatRoom 9999 UDP" dir=in action=allow protocol=UDP localport=9999
```

TCP 用来传网页和消息；UDP 用来给「自动查找」应答。不放开 UDP 也能用，只是得手动填地址。

## 6. 重新构建 APK

不需要 Android Studio / Gradle，一条命令：

```powershell
.venv\Scripts\python.exe android\build_apk.py
```

产物仍然是 `android/build/chatroom.apk`。

构建依赖项目根目录 `.toolchain/` 里的三样东西（已下载，第一次构建会自动解压）：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `jdk17.zip` | JDK 17 | javac / keytool；d8、apksigner 也要用它的 java |
| `platform-34.zip` | Android SDK platform 34 | 编译时的 `android.jar` |
| `build-tools-34.zip` | Android SDK build-tools 34 | aapt2 / d8 / zipalign / apksigner |

换电脑重新下载时的地址（国内可访问）：

- JDK 17：`https://mirrors.huaweicloud.com/openjdk/17.0.2/openjdk-17.0.2_windows-x64_bin.zip`
- platform：`https://mirrors.cloud.tencent.com/AndroidSDK/platform-34-ext7_r03.zip`
- build-tools：`https://mirrors.cloud.tencent.com/AndroidSDK/build-tools_r34-windows.zip`

构建脚本做的事（和 Android Studio 内部一样，只是手动）：

```
aapt2 compile  →  aapt2 link（生成 R.java 和资源 APK）
javac          →  d8（Java 字节码转成 classes.dex）
塞进 APK       →  zipalign 对齐  →  apksigner 签名（debug 证书）
```

## 7. 用 Android Studio 打开工程

`android/` 目录本身就是标准 Gradle 工程：

1. Android Studio → Open → 选 `chat-room/android` 目录
2. 首次同步会下载 Gradle 和 Android Gradle 插件（`settings.gradle` 里已配置阿里云镜像，国内可通）
3. 直接 Run 即可

说明：

- 包名在 `app/build.gradle` 的 `namespace` 里，Manifest 里**故意没写** `package` 属性（AGP 8 的要求）；手动构建时 `build_apk.py` 会自动补上
- 项目**不依赖 AndroidX、不依赖任何第三方库**，所以没有 `dependencies` 块，同步很快
- 如果想改 App 名称/图标：`res/values/strings.xml` 的 `app_name`、`res/drawable/ic_launcher.xml`

## 8. 装好之后怎么验功能

拿同一台手机和电脑（电脑浏览器也开一个窗口）对照：

1. 手机发一条 → 电脑 1 秒内收到；电脑消息下方显示「全部已读」
2. 手机点自己消息的「撤回」→ 两端都变成「XX 撤回了一条消息」
3. 手机点「编辑」改内容 → 电脑上原地更新并显示「(已编辑)」
4. 电脑滚到上面，手机连发 3 条 → 电脑标题变 `(3)`、右下角「↓ 3 条新消息」
5. 关掉电脑黑窗口 → 手机 App 变「重连中…」，人数显示「—」；重新启动服务器 → 自动恢复
6. 手机按 Home 键再回来 → 自动重新登记，在线人数正常
7. 关掉服务端再启动 → 手机刷新，历史记录还在（数据在 `chat.db`）
8. **多房间**：App 里点左上角 ＋ 新建「学习」→ 发一条；回大厅看不到，切回学习还在；让人往大厅发 → 列表出现红点
9. **发照片**：点输入框左边 ＋ 选一张照片 → 两端都能看到，点图片能看大图
10. **换头像**：点左下角自己的头像选图 → 自己的消息气泡头像立刻变，电脑端 1 秒内同步
11. **撤回图片**：长按/点击自己发的图片消息 → 撤回 → 两端变成撤回提示，图片打不开
