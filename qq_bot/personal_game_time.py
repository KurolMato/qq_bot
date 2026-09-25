"""Personal playtime command parsing and text presentation."""
from .game_time_tracker import LeaderboardSnapshot

PERIODS = {"d": "day", "y": "yesterday", "w": "week", "m": "month"}
LABELS = {"day": "今日", "yesterday": "昨日", "week": "本周", "month": "本月"}


def parse_personal_query(value: str) -> tuple[str, str]:
    parts = value.strip().split(maxsplit=1)
    if not parts:
        raise ValueError("用法：/rank player [d/y/w/m] 昵称，例如 /rank player w Mato")
    period = PERIODS.get(parts[0].casefold())
    if period:
        if len(parts) < 2:
            raise ValueError("请在时间范围后输入昵称")
        return period, parts[1].strip()
    return "day", value.strip()


def _duration(seconds: float) -> str:
    # Do not inflate tiny samples into a full minute.
    minutes = int(seconds // 60)
    if not minutes:
        return "不足1分钟"
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}小时" if hours else "") + (f"{minutes}分钟" if minutes else "")


def format_personal(snapshot: LeaderboardSnapshot, name: str) -> str:
    label = LABELS[snapshot.period]
    if not snapshot.members:
        return f"{name}：{label}暂无可统计的游戏时长。"
    member = snapshot.members[0]
    end = (snapshot.last_poll or snapshot.day_start).astimezone(snapshot.day_start.tzinfo)
    lines = [f"{member.name} · {label}游玩", f"{snapshot.day_start:%m-%d %H:%M}—{end:%m-%d %H:%M}",
             f"总时长：{_duration(member.total_seconds)}"]
    platforms = {"steam": "Steam", "ps": "PS", "ps5": "PS", "switch": "Switch", "xbox": "Xbox"}
    lines.extend(f"{platforms.get(g.platform, g.platform)} · {g.name}：{_duration(g.seconds)}"
                 for g in member.games)
    return "\n".join(lines)


def resolve_binding_name(group_id: str, query: str) -> str:
    from .steam_registry import registry as steam
    from .ps5_registry import registry as psn
    from .switch_registry import registry as switch
    from .xbox_registry import registry as xbox

    query_key = " ".join(query.split()).casefold()
    if not query_key:
        raise ValueError("用法：/绑定 昵称或玩家名")
    exact, partial = {}, {}
    for registry, fallback in ((steam, "display_name"), (psn, "online_id"),
                               (switch, "ns_name"), (xbox, "gamertag")):
        for item in registry.list_group(group_id):
            name = item.nickname or getattr(item, fallback)
            aliases = {" ".join(value.split()).casefold()
                       for value in (name, getattr(item, fallback)) if value}
            key = " ".join(name.split()).casefold()
            if query_key in aliases:
                exact[key] = name
            elif any(query_key in alias for alias in aliases):
                partial[key] = name
    matches = exact or partial
    if len(matches) == 1:
        return next(iter(matches.values()))
    if matches:
        raise ValueError("匹配到多个成员，请输入完整昵称：" + "、".join(matches.values()))
    raise ValueError("本群没有找到该昵称或玩家名，请先登记平台账号。")
