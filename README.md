# Video Analysis Bot：零基础部署说明

这是一个运行在 Windows 电脑上的 QQ 群机器人，主要提供以下功能：

- 自动识别 Bilibili、X/Twitter、抖音、小红书和小黑盒视频链接，转换为 MP4 后发回群聊；
- 查询并播报 Switch、PSN、Steam、Xbox 玩家的当前游戏；
- 生成今日、本周、本月游戏时长排行榜；
- 生成游戏发售日历并自动提醒；
- 可使用本地管理后台填写群号、密钥、轮询间隔和游戏日历；

> [!WARNING]
> NapCatQQ 是非官方 QQ 协议实现，可能受 QQ 客户端更新影响，并有账号风控风险。请使用专用 QQ 小号、限制启用群、避免高频发送，并只下载和传播你有权使用的内容。

## 一、部署完成后会运行什么

项目运行时包含四部分：

| 组件 | 用途 |
|---|---|
| QQ | 机器人在群里的实际账号 |
| NapCat | 把 QQ 群消息转换成 OneBot 事件，并负责发送文字、图片和视频 |
| Video MP4 Web API | 下载视频、调用 yutto/yt-dlp/FFmpeg、提供管理后台 |
| Video QQ Bot | 处理群指令、游戏平台状态、排行榜和发售提醒 |

默认只监听本机：

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:8000` | 视频网页和内部 API |
| `http://127.0.0.1:8000/admin` | 本地管理后台，通过根目录的 `机器人管理.cmd` 打开 |
| `ws://127.0.0.1:8081/onebot/v11/ws` | NapCat 需要连接的反向 WebSocket |
| `http://127.0.0.1:6099/webui/` | NapCat WebUI，端口以实际启动日志为准 |

关闭浏览器页面不会停止机器人，但 QQ、NapCat、视频 API 和 QQ Bot 进程必须保持运行。电脑睡眠、关机或断电后机器人会停止工作。

## 二、开始前需要准备

### 必需内容

- Windows 10 或 Windows 11 64 位电脑；
- 一个专门作为机器人的 QQ 账号；
- 机器人需要加入的 QQ 群，以及对应群号；
- 至少约 10 GB 可用磁盘空间；
- 可以正常访问 GitHub、Python 软件源和各游戏平台接口的网络。

### 可选账号

只用视频解析时，不需要准备游戏平台账号。需要对应功能时再准备：

- Switch：一个 Nintendo 观察账号；
- PSN：一个 PlayStation 观察账号；
- Steam：一个 Steam Web API Key；
- Xbox：一个微软/Xbox 观察账号；
- SteamGridDB：可选，用于补充 Steam 竖版封面。

不要把主账号的密码、Cookie、NPSSO、API Key、Nintendo 回调链接或 Xbox 令牌发到 QQ 群、截图或公开仓库。

## 三、推荐的安装目录



```text
C:\video_analysis
C:\NapCat.Shell
```

最终至少应该能看到：

```text
C:\video_analysis\机器人管理.cmd
C:\NapCat.Shell\launcher-win10.bat
```

如果你使用其他目录也可以，但后面需要在管理后台填写真实路径。

## 四、下载项目

项目仓库：https://github.com/OWNER/REPOSITORY（公开发布后替换为新仓库地址）

### 方法 A：下载 ZIP

第一次安装且电脑还没有 Git 时：

1. 登录有权访问仓库的 GitHub 账号；
2. 打开仓库，点击 `Code` → `Download ZIP`；
3. 解压并把文件夹移动为 `C:\video_analysis`；
4. 确认 `C:\video_analysis\机器人管理.cmd` 直接存在，不要在里面再套一层文件夹。

### 方法 B：使用 Git

已经安装 Git 时，在 PowerShell 执行：



```powershell
git clone https://github.com/OWNER/REPOSITORY.git video-analysis-bot
```

如使用 GitHub CLI 克隆：gh repo clone OWNER/REPOSITORY video-analysis-bot。

## 五、一键安装基础软件

进入 `C:\video_analysis`，双击 `机器人管理.cmd`，选择“安装 Windows 基础软件”。内部脚本位于：

```text
scripts\windows\install-laptop-prerequisites.cmd
```

该脚本使用 Windows 的 `winget` 安装：

- Python 3.12；
- Node.js LTS；
- Git；
- FFmpeg。

Windows 弹出管理员确认时选择“是”。如果提示找不到 `winget`，打开 Microsoft Store，安装或更新“应用安装程序（App Installer）”，然后重试。

安装完成后建议重启一次 Windows。重新打开 PowerShell，检查：

```powershell
py -3.12 --version
node --version
npm --version
git --version
ffmpeg -version
```

每条命令都能显示版本号才算成功。

## 六、安装 QQ 和 NapCat

### 1. 安装 QQ

安装 Windows x64 版 QQ。建议使用专门的机器人 QQ，不要使用重要主账号。

### 2. 安装 NapCat

从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 获取与当前 QQ 版本兼容的 Windows Shell 版本，完整解压到 `C:\NapCat.Shell`。

必须复制或解压整个 NapCat 目录，只保留一个 `launcher-win10.bat` 无法启动。

如果目录中的启动文件不是 `launcher-win10.bat`，记住实际 `.bat` 文件的完整路径，稍后在管理后台填写。

> [!IMPORTANT]
> 如果日志出现 `PacketBackend 不支持当前QQ版本架构`，说明 NapCat 与当前 QQ 版本或 x64 架构不匹配。此时需要按照对应 NapCat Release 的说明安装受支持的 QQ 版本，不是本项目代码故障。

## 七、一键配置项目环境

双击根目录的 `机器人管理.cmd`，选择“首次配置 / 更新 Python 依赖”。内部脚本位于：

```text
C:\video_analysis\scripts\windows\setup-laptop.cmd
```

脚本会自动：

1. 为当前电脑创建 `.venvs\电脑名称` Python 环境；
2. 安装 yutto、yt-dlp、Pillow、NoneBot2、PSN 和 Xbox 等依赖；
3. 创建抖音图文转换专用环境；
4. 检查 nxapi，并询问是否安装；
5. 创建 `.env`、`data`、`secrets` 目录；
6. 限制 `secrets` 目录访问权限；
7. 打开本地管理后台。

如果询问是否安装 `nxapi@next`：

- 需要 Switch 功能：输入 `Y`；
- 暂时只用视频、PSN、Steam 或 Xbox：输入 `N`，以后仍可安装。

安装失败时不要直接关闭窗口，先阅读最后几行错误。网络中断后可以重新运行该脚本，已经安装的内容不会重复破坏。

## 八、配置本地管理后台

以后需要打开后台时，在根目录双击 `机器人管理.cmd`，选择“打开管理后台”。内部脚本位于：

```text
scripts\windows\open-admin.cmd
```

管理后台只监听本机，不对局域网或公网开放。第一次至少填写以下内容。

### 1. 允许使用机器人的群号

例如：

```text
123456789,987654321
```

多个群号使用英文逗号。强烈不建议留空，因为留空代表机器人所在的所有群都可能触发功能。

### 2. NapCat 启动器路径

推荐填写：

```text
C:\NapCat.Shell\launcher-win10.bat
```

如果你的文件名或目录不同，填写真实完整路径。

### 3. NapCat 快速登录 QQ

可以填写机器人 QQ 号，也可以留空。填写后启动脚本会按照下面的形式调用 NapCat：

```text
launcher-win10.bat -q 机器人QQ号
```

### 4. 其他配置

第一次部署建议保留默认值：

- 群视频上限：100 MB；
- Switch 轮询：60 秒；
- PSN、Steam、Xbox 轮询：90 秒；
- 排行榜采样最大间隔：300 秒；
- 发售提醒：每天 09:00 检查，提前一天和当天提醒。

点击“保存配置”。普通配置保存后需要重启 QQ Bot；游戏日历内容保存后立即生效。

## 九、第一次启动

双击根目录的 `机器人管理.cmd`，选择“启动整套机器人”。内部脚本位于：

```text
scripts\windows\start-all.cmd
```

脚本会依次：

1. 启动 NapCat 和 QQ；
2. 启动 8000 端口的视频 API；
3. 启动 8081 端口的 QQ Bot；
4. 启动连接看门狗；
5. 检查 API 和机器人端口是否正常。

Windows 弹出管理员权限确认时选择“是”。正常情况下，只需要保留 `Video QQ Bot` 窗口；其他组件会在后台运行并写入日志。

首次登录 QQ 时，根据 NapCat/QQ 窗口完成扫码、设备验证或密码登录。

## 十、配置 NapCat 的 OneBot 连接

QQ 登录成功后，打开 NapCat WebUI：

```text
http://127.0.0.1:6099/webui/
```

实际端口和登录 Token 以 NapCat 启动日志为准。日志通常会出现：

```text
WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxx
```

如果没有看到，可以在 NapCat 的 `config\webui.json` 中查看 `port` 和 `token`。Token 相当于管理密码，不要公开。

进入 WebUI 后：

1. 打开 `OneBot 11` 网络配置；
2. 新增或启用“WebSocket 客户端/反向 WebSocket”；
3. URL 填写 `ws://127.0.0.1:8081/onebot/v11/ws`；
4. 消息格式选择 `array`；
5. 启用并保存；
6. 等待 NapCat 自动连接。

若此前出现 `ECONNREFUSED 127.0.0.1:8081`，表示 NapCat 找不到 QQ Bot。先确认 `Video QQ Bot` 窗口没有报错，并确认 8081 端口已经监听。NapCat 通常会在 30 秒内自动重连。

配置完成后，为确保全部设置生效，运行：

双击 `机器人管理.cmd`，依次选择“完全关闭机器人”和“启动整套机器人”。

## 十一、确认部署成功

把机器人 QQ 拉进管理后台填写的允许群，在群里输入：

```text
/bot status
```

全部正常时会看到：

```text
机器人运行状态：
QQ / NapCat：正常（已连接）
视频解析：正常（正常）
Switch：正常（正常）
PS5：正常（正常）
Steam：正常（正常）
Xbox：正常（正常）
运行时间：0分钟
```

尚未配置的游戏平台可能显示“未启用”，不影响视频解析和其他已经配置的平台。

直接单独 @机器人会收到指令说明；管理员提供本地 assets/instruction.png 时发送图片，公开源码不包含这张含第三方美术资源的图片，缺少时会返回常用指令文字。

测试视频时，在允许群发送一个公开视频链接。机器人不会发送“正在转换”的提示，成功后直接发送 MP4；超时、文件过大或链接本身没有视频时保持静默。

## 十二、按需启用游戏平台

四个平台互相独立。某个平台没配置好，不会阻止其他平台和视频解析运行。

### Switch

Switch 功能需要 Nintendo 观察账号和 nxapi。

1. 如果尚未安装 nxapi，在 PowerShell 运行 `npm install --global nxapi@next`；
2. 在 `机器人管理.cmd` 中选择“登录 Switch 观察账号”；
3. 只打开本次终端生成的新登录网址；
4. 在 Nintendo 最终页面右键“Select this person”，复制以 `npf71b963c1b7b6d119://auth` 开头的链接；
5. 将原始链接粘贴回同一个终端；
6. 看到 `Login and verification succeeded` 后重新启动机器人；
7. 群内测试 `/switch status`。

回调链接只能使用一次，不能刷新、复用或从聊天软件中复制。nxapi 依赖外部认证服务；若出现 502、OAuth、f-generation 或 remote configuration 错误，查看 [nxapi 服务状态](https://nxapi-status.fancy.org.uk/) 后再试。

群友登记示例：

```text
/switch add SW-1234-5678-9012 群友昵称
/switch list
/switch nickname SW-1234-5678-9012 新昵称
/switch remove SW-1234-5678-9012
```

观察账号会发送 Nintendo 好友申请，对方同意并允许查看在线状态后才能播报当前游戏。

### PSN / PS4 / PS5

1. 使用专门的 PSN 观察账号登录 PlayStation 官网；
2. 在同一浏览器打开 <https://ca.account.sony.com/api/v1/ssocookie>；
3. 复制返回内容中的 64 位 `npsso`；
4. 在 `机器人管理.cmd` 中选择“登录 PSN 观察账号”；
5. 粘贴 NPSSO，输入时窗口不会回显；
6. 登录成功后重启机器人；
7. 群内测试 `/psn status`。

NPSSO 等同账号密码，只能粘贴到本机登录窗口。

PSN 头像会在机器人启动时及每天本机时间 0 点自动刷新，无需 remove 后重新 add。机器人需保持运行；关机期间错过的刷新会在下次启动时补做。单个账号读取失败或头像为空时保留原头像，次日再试。

群友登记示例：

```text
/psn add PSN在线ID 群友昵称
/psn list
/psn nickname PSN在线ID 新昵称
/psn remove PSN在线ID
```

如果无法读取目标玩家，提醒对方把“在线状态和当前游戏”设为所有人可见。

### Steam

1. 登录 Steam；
2. 打开 <https://steamcommunity.com/dev/apikey>；
3. 创建 Steam Web API Key；
4. 在 `机器人管理.cmd` 中选择“配置 Steam API Key”；
5. 粘贴 32 位 API Key；
6. 成功后重启机器人；
7. 群内测试 `/steam status`。

群友登记示例：

```text
/steam add 7656119xxxxxxxxxx 群友昵称
/steam add 39734272 群友昵称
/steam add https://steamcommunity.com/id/example 群友昵称
/steam list
/steam nickname SteamID64 新昵称
/steam remove SteamID64
```

目标玩家必须公开 Steam 个人资料和游戏详情。

Steam 官方缺少合适竖版封面时，可以到 <https://www.steamgriddb.com/profile/preferences/api> 创建 SteamGridDB Key，然后在 `机器人管理.cmd` 中选择“配置 SteamGridDB Key”。这个步骤不是必需的。

### Xbox

1. 在 `机器人管理.cmd` 中选择“打开管理后台”；
2. 点击页面上方“打开 Xbox 登录”；
3. 在浏览器登录用于观察状态的微软/Xbox 账号；
4. 页面提示成功后重启 QQ Bot；
5. 群内测试 `/xbox status`。

不需要 OpenXBL 手机号验证或 API Key。目标玩家需要公开在线状态、当前游戏和游戏历史。

群友登记示例：

```text
/xbox add 玩家代号 群友昵称
/xbox list
/xbox nickname 玩家代号 新昵称
/xbox remove 玩家代号
```

## 十三、视频解析 Cookie（可选）

Bilibili 和普通公开视频通常不需要 Cookie。只有平台要求登录、验证码或风控时才配置。

支持两种方法：

1. 导出 Netscape 格式 `cookies.txt`，放进 `C:\video_analysis\secrets`；
2. 在管理后台填写浏览器配置，例如 `edge` 或 `chrome:Default`。

常用 `.env` 项：

```dotenv
X_COOKIES_FILE=C:\video_analysis\secrets\x-cookies.txt
DOUYIN_COOKIES_FILE=C:\video_analysis\secrets\douyin-cookies.txt
XIAOHONGSHU_COOKIES_FILE=C:\video_analysis\secrets\xiaohongshu-cookies.txt
```

Cookie 文件必须是 Netscape 格式，不能把浏览器导出的 JSON 直接改名使用。Cookie 属于账号凭据，不能上传 GitHub。

小红书只处理视频笔记，文字或图片笔记不会回复“转换失败”。抖音图文会尝试合成竖版 MP4；此功能通常更依赖有效 Cookie。

## 十四、排行榜与游戏日历

### 排行榜

```text
/rank          今日排行，04:00 开始统计
/rank y        昨日结算排行（上一统计日 04:00 至下一日 04:00）
/绑定 昵称或玩家名   将自己的 QQ 号绑定到本群成员，再次绑定直接替换
/rank me       已绑定昵称的今日时长（图片）
/rank me w     已绑定昵称的本周时长（也支持 y、m）
/rank player w 昵称   指定成员的本周时长（省略 w 查询今日，也支持 y、m）
/rank w        本周排行，本周一 04:00 开始统计
/rank m        本月排行，当月 1 日 04:00 开始统计
/rank m 8      查看最近一次 8 月的历史月排行
/rank o        另一成员组的今日排行
/rank o y      另一成员组的昨日结算排行
/rank o w      另一成员组的本周排行
/rank o m      另一成员组的本月排行
/rank o move 成员名
/rank o restore 成员名
/rank remove 游戏名
/rank add 游戏名
```

昵称相同的 Switch、PSN、Steam 和 Xbox 记录会合并。`/rank o move` 会把合并后的成员从主榜移至另一成员组，`restore` 可恢复；设置按群保存。屏蔽游戏只影响排行榜，不影响开始游戏时的实时播报。

月榜图片每天北京时间 04:00 后由后台更新一次，`/rank m` 和 `/rank o m` 发送已缓存的单张图片；首次无缓存时生成。缓存保存在 `data/monthly_rank_cache/<群号>/<年月>/<main或other>/`，重启后继续复用。历史月份按需生成，每个统计日最多成功更新一次；刷新失败时保留同月份旧图。月榜只展示严格超过 1 小时的游戏，总时长与排名仍包含未展示游戏；屏蔽、移组等修改在月榜下次刷新时体现，日榜和周榜仍即时生成。

### 正在游玩列表

```text
/game ol
```

把本群 Steam、PS、Switch、Xbox 正在游玩的玩家合成一张图片，不显示离线玩家。

### 游戏发售日历

```text
/game add 游戏名
/game add switch 游戏名
/game add psn 游戏名
/game add 任天堂或PlayStation商店链接
/game list 9
/game list 2027-9
/game list 2026
```

`/game list 年份` 将该年的月表按月份从左到右拼成一张图片。当年从本月展示到 12 月，其他年份展示 1—12 月；没有登记游戏的月份也会保留。数据来自已登记的游戏。

`@宇宙机器人 game 游戏名` 或 `/game 游戏名` 查询本群已登记 Steam 账号的成就进度，也支持 `/game AppID` 精确查询。图片分为已全成就（按达成时间先后）和未全成就（按已解锁比例）；显示头像、登记昵称、Steam 名称、成就数量和累计游玩时长，每页最多 12 人，所有分页图片合并在一条消息中发送。头像和封面优先复用列表图片缓存，没有有效缓存时才下载并缓存。达成时间取最后一个成就的解锁时间，缺失时标注未记录；累计时长不是全成就耗时。私密、接口失败或成就定义不一致的账号计入无法查询，不当作未完成。需要已配置 Steam API Key；同名候选会提示使用 AppID。此查询不会修改成就监控记录或触发全成就广播。

没有平台前缀时默认搜索 Steam。匹配不到、尚未公布明确日期或图片不合适时，可以在管理后台手动填写游戏名、发售日期并上传、裁剪封面。

## 十五、日常启动、关闭和查看日志

根目录只需要双击 `机器人管理.cmd`。它提供以下菜单，实际脚本统一存放在 `scripts\windows`：

| 管理器菜单 | 用途 |
|---|---|
| 启动整套机器人 | 启动 NapCat、视频 API、QQ Bot 和看门狗 |
| 完全关闭机器人 | 先停止看门狗，再关闭整套服务 |
| 打开管理后台 | 打开本地配置、状态和日志页面 |
| 只重启 QQ Bot | 不影响其他服务地重启机器人 |
| 首次配置 / 更新 Python 依赖 | 安装或更新当前电脑的依赖 |
| 从 GitHub 更新项目 | 检查代码后执行 `git pull --ff-only` |
| 打开日志目录 | 直接打开 `data` 文件夹 |

日志位置：

| 文件 | 内容 |
|---|---|
| `data\qq-bot.log` | QQ Bot、Switch、PSN、Steam、Xbox 和群指令异常 |
| `data\video-api.log` | 视频 API、下载器、FFmpeg 等后台输出 |
| `data\napcat-launch.log` | NapCat 启动输出 |
| `data\napcat-watchdog.log` | WebSocket 检查和自动重启动作 |

管理后台的“日志”区域会过滤普通群消息，重点显示机器人主动行为和异常。

## 十六、从 GitHub 更新

使用 Git 克隆的项目可以直接更新：

1. 双击 `机器人管理.cmd`；
2. 选择“完全关闭机器人”；
3. 返回菜单并选择“从 GitHub 更新项目”；
4. 更新完成后选择“启动整套机器人”。

管理器会使用 `git pull --ff-only`，不会覆盖已经被忽略的 `.env`、`data`、`secrets`、日志和虚拟环境。如果提示存在本地代码修改，先执行 `git status` 检查，不要使用 `git reset --hard`。

ZIP 下载的项目不能直接 `git pull`。需要长期更新时，建议重新用 Git 克隆，再把旧项目的 `.env`、`data` 和 `secrets` 复制回来。

## 十七、备份和迁移

停止机器人后，至少备份：

```text
.env
data\
secrets\
switch_presence.json（如果存在）
昵称绑定.txt（如果存在）
```

不要把 `.venv` 或 `.venvs` 复制到另一台电脑；新电脑通过管理器重新安装依赖。Switch 的 nxapi 登录通常位于 Windows 用户目录，换电脑后还需要重新登录。

详细流程见 [笔记本迁移与管理后台.md](docs/笔记本迁移与管理后台.md)。



## 十八、常见问题

### 双击 PowerShell 脚本提示禁止运行

日常使用 `.cmd` 文件即可，它们会自行以合适的 ExecutionPolicy 调用内部脚本，不需要修改全局执行策略。

### 提示找不到 Python 环境

在 `机器人管理.cmd` 中选择“首次配置 / 更新 Python 依赖”。不要手动复制其他电脑的 `.venv` 或 `.venvs`。

### 提示找不到 yutto、yt-dlp 或 Pillow

在 `机器人管理.cmd` 中重新选择“首次配置 / 更新 Python 依赖”。它会从 `requirements-bot.txt` 安装完整依赖。

### NapCat 一直显示 ECONNREFUSED 127.0.0.1:8081

说明 QQ Bot 没有在 8081 端口监听。检查 `Video QQ Bot` 窗口和 `data\qq-bot.log`，确认已经完成首次配置，然后在管理器中依次选择“完全关闭机器人”和“启动整套机器人”。

### NapCat 报 PacketBackend 不支持当前 QQ 版本

NapCat 与 QQ 版本不匹配。按照当前 NapCat Release 的说明更换受支持的 QQ x64 版本或升级 NapCat。

### `/switch list`、`/psn list` 或 `/steam list` 很慢

新版列表命令只读取本地 SQLite 数据库，正常应快速返回。先查看 `data\qq-bot.log` 是否有数据库占用、图片下载、网络轮询或旧进程重复运行，并确认只启动了一套机器人。

### 卡片头像或游戏封面没有显示

通常是 Nintendo、Sony、Steam、Xbox 或图片 CDN 临时无法访问。文字状态仍然有效。查看 `data\qq-bot.log` 中的图片下载错误，并检查系统代理、防火墙和网络。

### 视频链接没有回复

程序对以下明确情况会故意保持静默：

- 动态本身没有视频；
- 视频超过群文件上限；
- 下载或转换超时；
- 小红书链接是图文笔记；
- 平台要求登录但未配置 Cookie。

先用公开、较小的视频测试，再查看 `data\video-api.log` 和 `data\qq-bot.log`。


### WebSocket 偶尔断开

短暂网络波动、QQ 重连、NapCat/QQ 版本兼容或系统休眠都可能导致断开。看门狗会先重启 QQ Bot，仍未恢复再重启 NapCat。若持续断开，查看 `data\napcat-watchdog.log` 和 `data\qq-bot.log`。

## 十九、安全规则

绝对不要上传或公开：

```text
.env
secrets\
data\
*.log
cookies.txt
NPSSO
Nintendo session token 或回调链接
Xbox token
NapCat WebUI token
```

项目默认只监听 `127.0.0.1`。不要把 8000、8081 或 6099 端口直接映射到公网。确实需要跨电脑访问视频 API 时，必须设置 `VIDEO_API_TOKEN`，并配合防火墙、可信局域网或 HTTPS 反向代理。

建议 GitHub 仓库保持私有。每次提交前运行：

```powershell
git status
git diff --cached
```

确认没有密钥、数据库、Cookie 和日志后再推送。

## 通关时长查询（HLTB）

在允许的群中发送 `/hltb 《艾尔登法环》` 或 `/hltb Elden Ring`，机器人引用原消息，返回主线、主线＋支线、全收集的参考小时数与来源链接。没有数据的项目显示“暂无数据”，不会发送查询中的提示。

名称不唯一时，按返回的列表发送 `/hltb select 1`。候选在 5 分钟内有效，仅限原群、原发起用户选择。中文名先使用已确认映射，否则通过 Steam 查英文名；未找到时可直接输入英文全名，Steam 未收录的游戏也可以这样查询。

数据保存在 `data/hltb-cache.db`，全群共享、重启保留；有效期 7 天。过期数据先显示并标注更新时间，后台刷新，不追加群消息。未找到的结果缓存 10 分钟，网络错误不会覆盖成功缓存。冷查询最多等待 20 秒，同时最多进行 2 个查询，不影响游戏平台轮询。该功能只提供文字查询，不加入卡片、排行榜或 MC 评分。

笔记本升级本功能必须同步以下全部运行文件（保持原目录）：

- `qq_bot/hltb_service.py`
- `qq_bot/hltb_commands.py`
- `qq_bot/run.py`
- `requirements-bot.txt`

随后打开 `机器人管理.cmd`，选择安装/更新依赖，再选择只重启 QQ Bot。新增依赖为 `howlongtobeatpy==1.0.23`，无需填写新密钥。缓存库首次查询自动生成，不需要从桌面电脑复制；已有数据库不要覆盖。文字说明可另行同步本 README 和 `docs/机器人指令说明书.md`，无需更新说明图片或后台文件。服务使用第三方非官方 HLTB 查询库，网站变化或网络异常可能导致暂时不可用，详情见机器人日志。

## 心跳与自动恢复更新

心跳写入使用独立临时文件和短暂重试；文件持续不可写时保留旧数据、记录错误，不退出心跳任务。持续过期或断开的应用心跳经过 90 秒宽限后启动恢复，TCP 连接仍在也不会跳过。默认心跳过期阈值为 25 秒，因此从最后一次心跳算起通常约两分钟开始恢复。连续三次健康采样才确认恢复；恢复失败有冷却，避免反复刷错。

完整启动前需右键 `机器人管理.cmd` → **以管理员身份运行**，再选 1。这是 NapCat 启动器要求的权限；脚本不再静默启动失败，也不会在后台不断弹出提权窗口。看门狗只终止经路径和命令行确认的目标进程；无法确认时会拒绝操作并记录原因，不会随意结束其他 QQ 或 Python。

本次修复同步到笔记本所需的全部运行文件：`qq_bot/atomic_json.py`、`qq_bot/watchdog.py`、`qq_bot/napcat_watchdog.py`、`qq_bot/run.py`、`scripts/windows/start-all.cmd`、`scripts/windows/start-napcat.cmd`。不新增依赖。先在管理器选 2 完全关闭旧进程，复制文件，再以管理员身份打开管理器选 1；只重启 QQ Bot 不会更新已运行的外部看门狗。无需删除任何数据库或修改目录权限。

`data/napcat-watchdog.log` 每分钟记录看门狗存活、心跳新鲜度、连接状态、进程 ID 和恢复阶段；`data/napcat-launch.log` 记录自动恢复启动输出。启动动作已发出不等于恢复成功，应以随后连续健康的记录为准。首次卡顿的具体阻塞点仍需现场诊断，自动恢复不能保证消除所有卡顿原因。

## Steam 全成就自动播报

现有 `/steam add` 登记的玩家会自动参与，无需新命令或密钥。首次读取只建立成就基准，已完成的老游戏不补发；之后从未完成变为全成就时，向该玩家已登记且在白名单内的群发送“昵称在《游戏名》中获得全成就。”和 1200×800 卡片。

卡片包含 Steam 头像、游戏封面、成就数、累计游玩时长和达成时间。昵称优先使用各群绑定值，游戏名复用官方中文名称。时长来自 Steam 已同步的累计分钟数，缺失显示暂无数据。右上角为本地绘制的 Steam 风格 100% 绶带，并非从 Valve 下载的官方原图。

正在玩的游戏每 5 分钟检查；每 30 分钟发现最近两周玩过的游戏并检查。退出游戏或离线同步后可能延迟播报。成就不存在或隐私不可读属于正常跳过，日志只留一行并在 7 天后重试；网络故障采用短暂退避。全成就清单会再次校验，空成就不会触发。DLC 计入同一游戏的成就总数；同一账号同一游戏只播报一次，后续新增成就或重置不重复播报。新登记群不补发历史事件。

`data/steam-achievements.db` 自动生成并保留基准、轮询进度和各群发送状态，不要覆盖或删除。明确发送失败最多尝试 3 次；发送结果不确定会记录 `uncertain` 并停止自动重发。已成功发送的记录重启后不重复。

笔记本运行本功能必须同步全部文件：`qq_bot/steam_achievement_store.py`、`qq_bot/steam_achievement_card.py`、`qq_bot/steam_achievements.py`、`qq_bot/steam_registry.py`、`qq_bot/run.py`、`assets/steam-perfect.png`。无需安装额外依赖，同步后在管理器选择“只重启 QQ Bot”。`tools/preview_steam_achievement.py` 是可选的本地样例生成工具，`assets/steam-perfect-source.txt` 说明图标来源，均不是运行依赖。首次部署时，玩家需允许公开读取成就和游戏详情；隐藏累计时长不会阻止全成就事件。

## 二十、进一步说明

- [机器人指令说明书.md](docs/机器人指令说明书.md)：完整群聊指令、权限和功能规则；
- [使用说明.md](docs/使用说明.md)：深入故障排查和技术细节；
- [笔记本迁移与管理后台.md](docs/笔记本迁移与管理后台.md)：迁移、备份和管理后台说明；
- [.env.example](.env.example)：全部配置项及默认值。

如果机器人已经启动但行为不符合预期，优先提供以下三项用于排查：

1. 群内 `/bot status` 输出；
2. 管理后台日志中对应时间附近的异常；
3. `data\qq-bot.log` 或 `data\video-api.log` 中相关错误段落。

发送日志前先删除其中可能出现的 Cookie、Token、NPSSO、API Key、本机用户名和敏感路径。
