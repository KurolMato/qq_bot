# DSH 私聊助手（可选）

在 QQ 私聊里直接问项目问题：DSH 会读 `data/qq-bot.log`、翻代码、联网查资料，然后把结论发回来。主人白名单私聊默认使用 **workspace-write**：可以按明确请求修改项目工作区文件，使用现有命令工具执行权限范围内的操作。目录外写入或其他额外权限请求仍会被拒绝；不自动获得管理员权限或桌面点击能力。

## 能做什么、不能做什么

| 能做 | 边界 |
|---|---|
| 读日志、读代码、搜索与修改工作区文件 | 以工作区写权限和本轮明确请求为限 |
| 用 pwsh 执行现有命令 | 不能自动批准工作区外写入或权限提升 |
| 联网搜索和抓取网页 | 主动发群消息、加好友 |
| 给出排查结论和建议 | 读取后输出 `.env`、`secrets/` 里的密钥 |

主人操作能力只服务 `QQ_DSH_USERS` 里的 QQ 私聊。群聊由独立模块处理；群聊、主动消息、风格总结和自动诊断均使用独立只读实例。

## 工作原理

```
QQ 私聊 → NapCat → NoneBot（本项目）→ 本机 dsh 子进程（ACP / stdio）→ 模型
```

- 使用独立的 `DSH_HOME`（默认 `%USERPROFILE%\.dsh-qq`），和你日常用的 DSH 桌面版互不干扰。
- 一个 QQ 号对应一个 DSH 会话，追问会带上文；`/dsh new` 开新会话。
- **记忆存在磁盘上**：对话日志在 `%USERPROFILE%\.dsh-qq\sessions\`。机器人重启、dsh 进程空闲退出后，下一条消息会用 ACP 的 `session/resume` 自动接回原来那段对话，不会重开。
- dsh 子进程按需拉起，`QQ_DSH_IDLE_SECONDS` 默认 0（禁用空闲关闭）。设为正数后，仅在没有请求执行且空闲达到该秒数时关闭；下次发消息时重新拉起并恢复会话。
- 模型调用、联网搜索都用 `DEEPSEEK_API_KEY` 这一个 key。

## 一次性安装

前提：已装 Node.js（开发机为 v24.19.0）。

1. 安装 dsh CLI（约 250 MB，装到用户目录，不进仓库）：

```powershell
npm install --prefix "$env:USERPROFILE\.dsh-qq\cli" `
  --cache "$env:USERPROFILE\.dsh-qq\npm-cache" `
  --registry https://registry.npmmirror.com --prefer-offline `
  --no-audit --no-fund @deepseek-ai/dsh@0.2.1-rc.3
```

网络中断（`ECONNRESET`）时重复执行同一条命令即可，已下载的包会走缓存。

2. 从 acp 模板生成 `qq` profile：

```powershell
$env:DSH_HOME = "$env:USERPROFILE\.dsh-qq"
node "$env:DSH_HOME\cli\node_modules\@deepseek-ai\dsh\lib\bin.js" `
  --profile qq --from-default-profile acp --dump-config
```

3. 使用生成的默认 profile 即可；如需自定义人设或模型参数，只编辑本机 `%USERPROFILE%\.dsh-qq\profiles\qq\cordis.patch.yml`。不要把该文件、个人提示词或会话记录放进仓库。运行时沙箱权限由桥接单独覆盖，与人设文字无关。仓库不提供开发者的私人 profile。

4. 在项目 `.env` 配置（`.env` 已被 git 忽略）：

```dotenv
DEEPSEEK_API_KEY=请在本机填写自己的密钥
QQ_DSH_ENABLED=true
QQ_DSH_USERS=你的QQ号
QQ_DSH_TIMEOUT=600
QQ_DSH_IDLE_SECONDS=0
QQ_DSH_MAX_ANSWER_CHARS=700
```

5. 重启 QQ Bot（`机器人管理.cmd` → 只重启 QQ Bot），用白名单 QQ 私聊发一句话测试。

不经过 QQ 的自检（同样读 `.env`，会真的调用一次模型）：

```powershell
cmd /v:on /c 'call tools\select_python.cmd && "!BOT_PYTHON!" tools\dsh_smoke.py'
cmd /v:on /c 'call tools\select_python.cmd && "!BOT_PYTHON!" tools\dsh_smoke.py "看下 data/qq-bot.log 最近有没有 ERROR"'
```

输出会给出 profile、会话 id、耗时、调用的工具和回答正文；失败时打印 dsh 的 stderr 尾部。

## 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQ_DSH_ENABLED` | `false` | 总开关 |
| `QQ_DSH_PERMISSION_MODE` | `workspace-write` | 主人私聊权限，可设为 `read-only` 恢复只读；额外权限申请仍拒绝 |
| `QQ_DSH_BROWSER_ENABLED` | `true` | 本机安装 Playwright MCP 后启用浏览器操作，仅对可写的白名单私聊挂载 |
| `QQ_DSH_USERS` | 空 | 逗号分隔的 QQ 号白名单；空 = 谁都不服务 |
| `QQ_DSH_TIMEOUT` | `600` | 单轮回答超时（秒）；超时取消，发送已有部分回复并保留会话映射 |
| `QQ_DSH_IDLE_SECONDS` | `0` | 空闲多久关闭 dsh 子进程（会话记忆保存在磁盘，下次自动恢复） |
| `QQ_DSH_MAX_ANSWER_CHARS` | `700` | 单条回答的字数上限，超出在句末截断 |
| `QQ_DSH_MAX_IMAGES` | `4` | 单条消息最多带几张图给模型看，0 关闭识图 |
| `QQ_DSH_HOME` | `~/.dsh-qq` | 独立 DSH_HOME（CLI、profile、会话都在里面） |
| `QQ_DSH_PROFILE` | `qq` | 使用的 profile 名 |
| `QQ_DSH_CLI` | 空 | dsh 启动器路径；留空按 `QQ_DSH_HOME` 推导 |
| `QQ_DSH_WORKDIR` | 项目根 | agent 的工作目录（`AGENTS.md` 从这里读取） |
| `DEEPSEEK_API_KEY` | 空 | 模型与联网搜索共用；dsh 从“进程工作目录/.env”或启动环境读取 |

注意：本桥接**不读**环境里继承的 `DSH_HOME` / `DSH_PROFILE`。如果机器人本身跑在 DSH 会话里，那些变量描述的是宿主，桥接会忽略它们，并给子进程清掉所有 `DSH_*`。

## 使用

- 私聊直接发问题，例如：「今天视频接口 500 了吗」「小黑盒图文发送报错的原因」「查一下 yt-dlp 这个参数什么意思」。
- `/dsh help` 帮助
- `/dsh status` 看进程状态、会话 id、已完成轮数
- `/dsh new` 开新会话（旧上下文不带入）
- `/dsh stop` 取消正在进行的回答
- `/dsh restart` 或 `/dsh 重启`：仅白名单私聊可用，先回复“准备重启”，再由独立进程延迟 3 秒重启 QQ Bot。30 秒内重复请求不会重复启动；不调用模型，也不重启 NapCat 或视频 API。重启任务校验原 Bot 的 PID，若期间进程已被替换，就跳过该请求。启动失败查看 `data/qq-bot.log` 或 `data/napcat-watchdog.log`。

首次加入此指令后，旧进程尚不认识它，需要先在 `机器人管理.cmd` 中选择“只重启 QQ Bot”加载新代码。之后可直接通过 QQ 使用。该管理指令不依赖 DSH 是否处于只读模式；仍要求 `QQ_DSH_ENABLED=true` 且发送者位于 `QQ_DSH_USERS`。

回答默认只有两三句话：先给结论，需要细节你再追问。超过 `QQ_DSH_MAX_ANSWER_CHARS`（默认 700）字会在句子末尾截断并加省略号，确实要长内容时最多分 2 条发送。单条提问上限 3000 字；连着发的几条会按顺序一条条处理，不会丢；`/dsh stop` 会把排队的也清掉。

不再先发「收到，正在排查…」这类占位消息：发完问题后安静等结果即可，简单问题通常几秒，要翻日志的问题十几秒到半分钟。

回复会拆成多条气泡：短句里用逗号/分号/句号隔开的片段各发一条，间隔 0.6 秒；整句超过 `QQ_DSH_BUBBLE_COMMA_CHARS`（默认 30 字）就保持一条，避免长句被切碎。默认不限条数（`QQ_DSH_MAX_BUBBLES=0`）；设成正整数就把多出来的并进最后一条，不丢内容。无论怎么设，硬上限 30 条，防止逗号链刷屏。发送间隔 `QQ_DSH_BUBBLE_DELAY_SECONDS` 默认 0.6 秒。

## 图片与表情包

浏览器打开、搜索与播放控制的安装和使用，见 [电脑浏览器操作.md](电脑浏览器操作.md)。它控制网页和播放器，不会自动分析完整视频内容。

**能看**：私聊里发截图、报错图、表情包，最多 `QQ_DSH_MAX_IMAGES`（默认 4）张，单张 ≤ 8 MB，会作为 ACP 图片块交给模型，所以它能读到图里的字。图片只在当轮有效，历史里保留的是附件引用。

**能搜图发图**：白名单私聊说「上网搜两张橘猫图片发给我」「找一张星空壁纸发我」，模型会用现有联网搜索或浏览器找图片直链，再通过 `[[发送图片:https://图片直链]]` 交给机器人下载发送。先发文字，再发普通 QQ 图片；每轮最多 3 张，支持 PNG/JPEG/GIF/WebP，单张 ≤ 8 MB，不保存到表情库，也不额外随机附带表情。下载检查初始地址和跳转地址，只接受公开 HTTP(S) 地址；网页、登录页、失效或超限图片会跳过，并说明未发出的数量。模型超时、取消或报错时不会执行发图标记。搜索结果和下载可能受网站限制，可要求换个来源；标记由模型生成，平时直接说需求即可。

此功能使用现有依赖，更新代码后重启 QQ Bot 生效。本次搜图发图功能同步到笔记本需带上 `qq_bot/dsh_web_images.py`、`qq_bot/dsh_commands.py`，以及说明文件 `docs/DSH私聊助手.md`、`docs/电脑浏览器操作.md`；对应测试为 `tests/test_dsh_web_images.py`、`tests/test_dsh_commands.py`。浏览器首次安装仍按上方文档操作。

**能发表情**：每轮普通文字回复发完后，机器人以 **50%**（`qq_bot/dsh_policy.py` 里的 `STICKER_SEND_PROBABILITY`）的概率附带一张表情。模型可在回复末尾写 `[[表情:分类]]` 或 `[[表情:分类/文件名]]` 指定内容；没有选择或所选文件不可用时，从现有表情库随机补选。每轮只抽签一次，最多发送一张，不再叠加“模型先决定要不要表情”的概率。私聊、群聊和主动消息使用相同规则；错误、超时、取消、空回复及项目功能图片不会额外随机配表情。没有可用文件时不发。保留原图，使用 NapCat 的 QQ 自定义表情包格式（`image` 消息段的 `sub_type=1`，不是普通图片模式，也不是系统小黄脸）。表情标记会从文字中移除。审计日志的 `sticker_requested` 表示模型是否指定表情，`sticker` 表示是否实际发送，便于区分模型选择与最终发送率。分类就是你建的文件夹名：

```
data\stickers\无语\01.png
data\stickers\开心\01.gif
data\stickers\赞\01.png
```

支持 png/jpg/gif/webp、单张 ≤ 4 MB；分类不存在或为空就不发，不会报错。目录里有 `README.txt`，图片本身不进 Git。

**能自己收表情**：

- 发一张图 + 「收下这张，叫无语」，它会自己判断并保存到 `data\stickers\无语\`；
- 或者引用一张图发 `/dsh 存表情 无语`（分类名自定义，2~4 字最合适）；
- 保存按内容哈希去重，重复的图不会存第二次，每个分类上限 300 张；
- 它只会收表情包和梗图，截图需要你明确要求。

**能看合并转发**：私聊里转发一段聊天记录，机器人会取回完整内容（昵称 + 文字，图片标成 `[图片]`）再交给模型，单段最多 3000 字，超长截断。

**能从网上收表情**：它联网找到图片直链后，用 `[[收图:分类|https://直链]]` 让机器人下载并存进 `data/stickers/<分类>/`；一次最多两张，只接受公开 http(s) 直链、单张 ≤ 4 MB、按哈希去重。也可以手动 `/dsh 存图 开饭 https://example.com/a.png`。

**修改项目代码**：主人白名单私聊可按明确请求修改工作区内的文件；实际能力以工具和沙箱返回为准。启动代理等系统操作需要对应命令和系统权限，桌面点击还需单独接入。

**不能做**：

- 画图、生成图片、拍照——只能发你事先放好的表情包；
- 发 QQ 空间说说/动态——NapCat 和 OneBot 协议没有这个接口，需要另外接 QQ 空间 Web API 和登录态，风险和改动都大，暂不支持；
- 部分“市场表情”（mface）取不到图片 URL，那一轮只能看到文字提示。

## 说话风格记忆

先采样、再总结一两轮，之后自动停止，不做持续训练。

- 采样：私聊里你发的文本、允许群里所有人的文本，按「私聊 / 每个群」记到 `data/dsh_style_samples.jsonl`（去 @、去图片、单条 200 字，每个范围最多 600 条）；
- 总结：后台每小时检查一次，某范围攒够 `QQ_STYLE_MIN_SAMPLES`（默认 60）条、且距上次总结超过 `QQ_STYLE_INTERVAL_HOURS`（默认 24）小时，就让模型提炼最多 6 条风格要点；
- 自动轮次：每个范围最多自动总结 `QQ_STYLE_MAX_ROUNDS`（默认 2）轮，之后不再自动学，避免风格漂移；
- 注入：之后的提问会附一句「这个群/这个人平时说话的风格：…，自然贴合」，人设硬规则不变；
- 手动：`/dsh 学语气` 立即对私聊范围总结一次，不受轮次限制。

风格档案在 `data/dsh_style.json`，最多保留 8 条要点；想重来就删这个文件和样本文件。不要自动学就设 `QQ_STYLE_ENABLED=false`。

## 排障

| 现象 | 检查 |
|---|---|
| 私聊完全没反应 | `QQ_DSH_ENABLED=true`？QQ 号在 `QQ_DSH_USERS` 里？`data/qq-bot.log` 里有没有加载 `dsh_commands` |
| 回复「我这边没起来」，日志里有 `YAMLException` / `failed to parse overlay` | `profiles/qq/cordis.patch.yml` 缩进坏了（常见于直接把整段人设粘进去）。正文每行要缩进 6 个空格；修完先跑 `--dump-config` 验证 |
| 重启后第一句回「刚才那段接不上了」 | 恢复失败（会话日志被删、工作目录变了、或上次 `/dsh new` 之后没再存）。会自动开新对话，正常继续用即可 |
| 回复提到 `MISSING_CREDENTIAL` / 401 | `DEEPSEEK_API_KEY` 是否写在项目 `.env`（子进程工作目录就是项目根） |
| 总是超时 | 调大 `QQ_DSH_TIMEOUT`；问题拆小；看 `data/dsh-audit.jsonl` 的 elapsed |
| 想确认用了哪些工具 | `data/dsh-audit.jsonl` 每轮记录 `tools` 和 `stop` |
| 会话串了 / 上下文太脏 | 发 `/dsh new` |
| 想彻底清空记忆 | 停机器人，删 `data/dsh_sessions.json` 和 `%USERPROFILE%\.dsh-qq\sessions\` 里对应会话目录 |

文件位置：

- `data/dsh_sessions.json`：QQ 号 → DSH 会话 id，删掉等于全部重置
- `data/dsh-audit.jsonl`：每轮的问题长度、耗时、工具、回答长度
- `%USERPROFILE%\.dsh-qq\`：CLI、profile、会话日志、`sessions/`

## 安全边界

- 主人私聊使用 `QQ_DSH_PERMISSION_MODE`，默认 `workspace-write`；额外权限请求一律拒绝。
- 群聊、主动消息、风格总结和视频自动诊断使用独立 `read-only` 进程；启动时通过 `--patch` 覆盖沙箱模式和默认权限预设，不依赖人设文字限制。
- 写权限仅对白名单私聊开放。新的可写私聊使用 `workspace-write:QQ号` 会话键，群聊使用 `readonly:group:群号`，主动消息使用 `proactive:QQ号`；旧会话映射和日志保留，但不复用旧权限范围的会话。
- 修改配置后在 `机器人管理.cmd` 中只重启 QQ Bot；`/dsh status` 会显示主人私聊权限。
- 提示词注入仍有残余风险：agent 能联网，也能读取本机文件，并在主人私聊中修改工作区文件。因此：
  - persona 明确禁止输出密钥；
  - 建议给这个 `DEEPSEEK_API_KEY` 单独设额度，怀疑泄露就换 key；
  - 审计日志会记录每次调用的工具名，异常可回溯。
- 不要让这个 QQ 号加入不受信任的群，也不要把白名单扩大给不信任的人。

## 同步到笔记本

仓库文件（需复制）：

```
qq_bot/dsh_client.py
qq_bot/browser_tools.py
qq_bot/dsh_commands.py
qq_bot/dsh_media.py
qq_bot/dsh_context.py
qq_bot/dsh_reply.py
qq_bot/dsh_policy.py
qq_bot/group_dsh.py
qq_bot/group_rules.py
qq_bot/group_usage.py
qq_bot/project_images.py
qq_bot/qq_forward.py
qq_bot/style_memory.py
qq_bot/video_delivery.py
qq_bot/video_repair.py
qq_bot/plugin.py
qq_bot/game_commands.py
qq_bot/game_time_commands.py
qq_bot/restart_bot.py
qq_bot/napcat_watchdog.py
qq_bot/run.py
scripts/windows/start-qq-bot.cmd
scripts/windows/setup-computer-use.cmd
scripts/windows/setup-computer-use.ps1
tools/dsh_smoke.py
tests/fake_acp_server.py
tests/test_dsh_client.py
tests/test_dsh_media.py
docs/DSH私聊助手.md
docs/电脑浏览器操作.md
data/stickers/README.txt
.env.example
```

笔记本额外步骤：安装 dsh CLI、生成本机 `qq` profile，在笔记本 `.env` 里配置自己的 `DEEPSEEK_API_KEY` 和 `QQ_DSH_*`，然后重启 QQ Bot。`%USERPROFILE%\.dsh-qq` 不建议跨机器复制（含私人提示词、会话和本机路径），按步骤重装更稳。保留原电脑的 `.env`、`secrets`、`data` 及私人 profile，更新代码无需重填现有配置。
