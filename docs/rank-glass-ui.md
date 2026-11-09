# 排行榜玻璃 UI

`/rank`、`/rank y`、`/rank w`、`/rank m` 及另一成员组共用新渲染器。

- 深色玻璃面板，前三位金银铜细框；总时长放大并右对齐。
- 榜首游戏使用三列横版封面网格，多于三款自动换行；其他成员逐款展示紧凑明细，不截断游戏数量。
- 保留 Steam、PS、Switch、Xbox 原平台底色；每款游戏下方进度条以该成员最长游戏为满条，颜色跟随平台。
- 日期和截止时间集中在页头。长名称自动缩小或省略，缺失封面使用占位底色。
- 月榜继续只展示超过一小时的游戏，但总时长不变。排名、周期边界、屏蔽规则不变。
- 月榜缓存使用 `glass-v1` 子目录，首次查询或后台刷新生成新图；旧缓存保留，不删除运行数据。

重启 QQ 机器人后生效，无新增依赖或数据库迁移。运行变更文件：

- `qq_bot/game_time_leaderboard.py`
- `qq_bot/rank_board_card.py`
- `qq_bot/monthly_rank_cache.py`

新渲染器复用项目已有 `hot_games_card.py` 的金属边框、`personal_rank_card.py` 的玻璃面板和 `list_cards.py` / `switch_presence.py` 的图像字体工具。同步到尚未更新群热游的旧安装时也需带上这些已有依赖及其依赖文件。

离线预览入口：`tools/preview_rank_board.py`，使用 `assets/previews/rank-me-assets/` 测试封面和虚构时长，不发送群消息。
