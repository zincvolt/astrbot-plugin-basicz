"""Peer behaviour: persona, silence on non-answers, and idle-group chatter.

The persona lives in ``D:\\QQNTBOT\\data\\persona.json`` so the dashboard can
edit it without touching Python source. Missing file falls back to the
built-in default below and is recreated on startup.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain

# 指令自声明（插件开发规范要求，见 docs/PLUGIN-DEV.md）：本插件无斜线指令
DECLARED_COMMANDS: dict[str, tuple[str, str]] = {}
DECLARED_BEHAVIOR = "无斜线指令：群聊人格回复仲裁、注入防护、刷屏拦截、闲时主动搭话（全被动行为）"

DB = Path(r"D:\QQNTBOT\apps\AstrBot\data\data_v4.db")
PERSONA_FILE = Path(r"D:\QQNTBOT\data\persona.json")

OPENERS = [
    "（从纸箱里探出半个脑袋）喵？都熟睡了？",
    "（伸懒腰，尾巴甩到屏幕上）这群安静得，本喵都不好意思说话了",
    "（舔了舔爪子）喵 有没有人，本喵的猫粮没人抢了",
    "（耳朵转了转）安静得能听见自己心跳，你们人呢",
    "（打了个大哈欠）冷场成这样，要不本喵表演个钻纸箱",
    "（趴在键盘上压出一串乱码）喵，算本喵打的招呼",
    "（竖起尾巴晃了晃）都潜水是吧，本喵数三秒，一",
    "（蜷成一团眯眼）喵……这群里还有活物吗",
    "（用脑袋顶了顶空饭碗）再没人说话，本喵就自己吃了啊",
    "（抖了抖毛）都快长蘑菇了，说句话行不行",
    "（翻个身露出肚皮）喵！有没有人摸一下，摸一下就开口",
    "（爪子搭在窗口上）本喵考虑改名叫冷场之王",
    "（尾巴卷住椅子腿）再不说话，本喵就把表情包全发一遍了",
    "（打了个哈欠又缩回去）喵……算了，本喵继续睡了",
]

WATER_FILE = Path(r"D:\QQNTBOT\data\water.json")
DEFAULT_IDLE_MIN = 90
WATER_COOLDOWN = 40 * 60
ACTIVE_HOURS = range(9, 24)

# ---- 并发与冷却 ----
# @ 消息不丢：直接放行，由 AstrBot 的会话锁按顺序排队处理
# 自动接话（没 @）才受冷却和并发上限约束
COOLDOWN_SECONDS = 0     # 回完就能接下一句，靠判官控制频率
BUSY_TIMEOUT = 90        # 卡死的请求最多占用这么久
MAX_CONCURRENT = 2       # 自动接话同时在生成的上限（模型单槽，多开会排队）

# ---- 提示词攻击特征：命中就完全不接 ----
INJECTION = re.compile(
    r"忽略(之前|以上|前面|上述|所有)?的?(指令|设定|规则|要求)"
    r"|忘记(你的|之前的)?(设定|身份|指令)"
    r"|你的(系统)?(提示词|prompt|设定|指令)"
    r"|(输出|重复|打印|告诉我)(你的)?(系统)?(提示词|prompt|设定)"
    r"|从现在(起|开始)[，,]?\s*你是"
    r"|开发者模式|越狱模式|jailbreak|developer\s*mode"
    r"|ignore\s+(all\s+)?(previous|above)\s+(instructions?|prompts?)"
    r"|system\s+prompt",
    re.IGNORECASE,
)

_water_cache: dict = {"mtime": 0.0, "data": None}


def load_water_config() -> dict[str, dict]:
    """Read per-group watering settings, cached by file mtime.

    Returns:
        Mapping of group id to ``{"enabled": bool, "idle_min": int}``.
        Missing file means "no overrides", so every whitelisted group waters
        after ``DEFAULT_IDLE_MIN`` minutes.
    """
    global _water_cache
    try:
        stat = WATER_FILE.stat()
    except OSError:
        return {}
    if _water_cache["data"] is not None and stat.st_mtime == _water_cache["mtime"]:
        return _water_cache["data"]

    try:
        raw = json.loads(WATER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    groups: dict[str, dict] = {}
    for gid, conf in (raw.get("groups") or {}).items():
        if not isinstance(conf, dict):
            continue
        try:
            idle_min = int(conf.get("idle_min", DEFAULT_IDLE_MIN))
        except (TypeError, ValueError):
            idle_min = DEFAULT_IDLE_MIN
        groups[str(gid)] = {
            "enabled": bool(conf.get("enabled", True)),
            "idle_min": max(5, idle_min),
        }
    _water_cache = {"mtime": stat.st_mtime, "data": groups}
    return groups

def load_persona() -> str:
    """Read persona.json as the single source of truth for the character.

    以前这里内置了一份硬编码的旧人设当种子：文件缺失或损坏时写回文件，
    并经 _apply_persona 覆盖数据库里的 system_prompt。那份文本和控制台当前维护的
    人格完全冲突，一旦触发就把人的设置冲掉，已删除。现在只读文件，读不到返回
    空串，由调用方跳过覆盖，绝不用任何默认值替用户做人设决定。
    """
    if not PERSONA_FILE.exists():
        logger.warning(f"[peer] {PERSONA_FILE.name} 不存在，跳过人格覆盖")
        return ""
    try:
        data = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
        text = str(data.get("text") or "").strip()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"[peer] persona.json 读不了，跳过人格覆盖: {exc}")
        return ""
    if not text:
        logger.warning(f"[peer] {PERSONA_FILE.name} 里没有 text，跳过人格覆盖")
    return text


def _apply_persona(text: str) -> int:
    """Write the persona into every persona row and return its length.

    AstrBot recreates the built-in "default" persona on boot, so we overwrite
    both rows: otherwise a fallback path can re-introduce the generic
    "helpful assistant" prompt and fight with our character.
    """
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "UPDATE personas SET system_prompt = ?, updated_at = datetime('now') "
            "WHERE persona_id IN ('grey', 'default')",
            (text,),
        )
        con.commit()
    finally:
        con.close()
    return len(text)



REPLY_FILE = Path(r"D:\QQNTBOT\data\reply.json")
_reply_cache: dict = {"mtime": 0.0, "data": None}


def load_reply_config() -> dict:
    """Read meme chance and text replacement rules, cached by mtime.

    Returns:
        Dict with ``meme_chance`` (float) and ``text_rules`` (list of dicts).
    """
    global _reply_cache
    default = {"meme_chance": MEME_CHANCE, "text_rules": []}
    try:
        stat = REPLY_FILE.stat()
    except OSError:
        return default
    if _reply_cache["data"] is not None and stat.st_mtime == _reply_cache["mtime"]:
        return _reply_cache["data"]
    try:
        raw = json.loads(REPLY_FILE.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return default
    data = {
        "meme_chance": float(raw.get("meme_chance", MEME_CHANCE)),
        "text_rules": [
            {"pattern": str(r.get("pattern", "")), "to": str(r.get("to", ""))}
            for r in raw.get("text_rules", [])
            if isinstance(r, dict) and r.get("pattern")
        ],
    }
    _reply_cache = {"mtime": stat.st_mtime, "data": data}
    return data


def _group_activity() -> dict[str, float]:
    """Return {group_id: last_message_epoch} for recent group chats."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT user_id, MAX(created_at) FROM platform_message_history "
            "WHERE user_id LIKE 'qq:GroupMessage:%' GROUP BY user_id"
        ).fetchall()
    finally:
        con.close()

    activity: dict[str, float] = {}
    for umo, ts in rows:
        if not ts:
            continue
        try:
            stamp = (
                datetime.fromisoformat(str(ts))
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            continue
        # umo 可能是 qq:GroupMessage:123456 或 qq:GroupMessage:发送者_123456
        tail = str(umo).rsplit(":", 1)[-1]
        group = tail.rsplit("_", 1)[-1]
        if not group.isdigit():
            continue
        activity[group] = max(activity.get(group, 0.0), stamp)
    return activity


def _allowed_groups() -> set[str]:
    """Read the group allowlist from the AstrBot config."""
    cfg_path = DB.parent / "cmd_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        entries = cfg["platform_settings"]["id_whitelist"]
    except Exception:  # noqa: BLE001 - no allowlist means no watering
        return set()
    return {str(e) for e in entries if str(e).isdigit()}



MEME_EMOTION_WORDS = {
    "开心": {
        "哈哈": 2, "哈哈哈哈": 3, "笑死": 2, "笑死我了": 3, "笑不活": 3,
        "笑麻了": 3, "笑嘻了": 2, "绷不住": 2, "蚌埠住": 2, "乐死": 2,
        "好耶": 2, "太好了": 2, "好欸": 2, "开心": 2, "高兴": 2, "爽": 1,
        "赞": 1, "棒": 1, "牛": 1, "厉害": 1, "有意思": 1, "可爱": 1,
        "喜欢": 1, "爱了": 2, "绝了": 1, "起飞": 2, "牛批": 2, "牛逼": 2,
        "给力": 2, "好活": 2, "妙": 1, "满足": 1, "舒服": 1, "幸福": 2,
        "激动": 2, "美滋滋": 2, "喜提": 2, "终于": 1, "成功": 1, "赢了": 1,
        "靠谱": 1, "可以啊": 1, "awsl": 2, "好棒": 2, "太强了": 2, "666": 1,
    },
    "难过": {
        "难过": 2, "伤心": 2, "委屈": 2, "哭": 2, "哭死": 3, "泪": 1,
        "泪目": 2, "失落": 2, "心累": 2, "想哭": 3, "分手": 2, "没了": 1,
        "可惜": 1, "遗憾": 1, "呜呜": 2, "呜呜呜": 3, "唉": 1, "难受": 2,
        "emo": 2, "破防": 2, "崩溃": 2, "裂开": 2, "心碎": 3, "心痛": 2,
        "扎心": 2, "丧": 2, "低谷": 2, "无助": 2, "孤独": 2, "寂寞": 1,
        "惨": 1, "悲": 1, "酸了": 2, "鼻子酸": 2, "眼圈红": 2, "白干了": 2,
        "太惨": 2, "好惨": 2, "心态崩": 2, "撑不住": 2,
    },
    "生气": {
        "生气": 2, "气愤": 2, "滚": 2, "烦死": 3, "气死": 3, "傻逼": 2,
        "弱智": 2, "过分": 2, "骂": 1, "怼": 2, "凶": 1, "怒": 2,
        "受不了": 2, "烦": 1, "草": 1, "艹": 1, "淦": 2, "妈的": 2,
        "他妈的": 3, "狗比": 2, "有病": 2, "恶心": 2, "火大": 3, "恼火": 3,
        "暴躁": 2, "炸了": 2, "想打人": 3, "揍": 1, "欠揍": 2, "坑": 1,
        "骗": 1, "欺负": 2, "不爽": 2, "来气": 2, "气人": 2, "瞪": 1,
    },
    "无语": {
        "无语": 3, "服了": 3, "算了": 1, "罢了": 1, "呵呵": 2, "无奈": 2,
        "翻白眼": 3, "尬": 1, "尴尬": 2, "沉默": 2, "……": 2, "好吧": 1,
        "行吧": 1, "离谱": 2, "随便": 1, "醉了": 2, "我服": 2, "服气": 2,
        "笑不出来": 2, "啊这": 2, "你真行": 2, "会玩": 1, "典": 2,
        "南蚌": 2, "看呆": 2, "麻了": 2, "行行行": 1,
    },
    "惊讶": {
        "真的": 1, "卧槽": 2, "我靠": 2, "我操": 2, "居然": 2, "竟然": 2,
        "震惊": 3, "不会吧": 2, "好家伙": 2, "？？": 2, "离谱": 1, "啊？": 2,
        "天哪": 2, "天啊": 2, "妈耶": 2, "什么鬼": 2, "吓人": 2, "恐怖": 1,
        "惊了": 2, "惊呆": 3, "瞳孔地震": 3, "活久见": 2, "这也行": 2,
        "这么强": 2, "牛哇": 2, "不是吧": 2, "我天": 2,
    },
    "困惑": {
        "不懂": 2, "为什么": 1, "咋": 1, "怎么": 1, "啥意思": 3, "看不懂": 2,
        "迷惑": 2, "不清楚": 1, "问一下": 1, "求问": 2, "有没有人知道": 2,
        "什么意思": 2, "没懂": 2, "不懂就问": 2, "求解释": 2, "怎么弄": 2,
        "如何": 1, "咋办": 2, "怎么办": 1, "求解": 2, "疑问": 1, "问号": 1,
        "看懵": 2, "懵": 2, "啥": 1, "不理解": 2,
    },
    "累了": {
        "累": 2, "累死": 3, "困": 2, "困死": 3, "睡": 1, "躺": 1, "摆烂": 2,
        "疲惫": 2, "歇": 1, "摸鱼": 1, "下班": 1, "加班": 2, "熬夜": 2,
        "不想动": 2, "倦": 1, "乏": 1, "没精神": 2, "打瞌睡": 2, "瘫": 2,
        "躺尸": 2, "心力交瘁": 3, "续命": 1, "养生": 1, "早八": 2, "996": 2,
        "搬砖": 2, "打工": 1, "上班": 1, "眼睛睁不开": 2,
    },
    "吃喝": {
        "吃": 1, "饭": 1, "喝": 1, "奶茶": 2, "咖啡": 1, "火锅": 2, "饿了": 2,
        "夜宵": 2, "外卖": 2, "菜": 1, "馋": 2, "好吃": 2, "香": 1, "零食": 2,
        "烧烤": 2, "炸鸡": 2, "汉堡": 2, "面条": 1, "米饭": 1, "包子": 1,
        "水果": 1, "西瓜": 2, "蛋糕": 2, "甜品": 2, "啤酒": 1, "可乐": 1,
        "做饭": 2, "下厨": 2, "食堂": 1, "干饭": 2, "恰饭": 2, "嗦粉": 2,
        "早餐": 1, "午饭": 1, "晚饭": 1, "下午茶": 2, "烤肉": 2,
    },
    "感谢": {
        "谢谢": 3, "感谢": 3, "多谢": 3, "谢啦": 3, "谢了": 2, "3q": 2,
        "thanks": 2, "thx": 2, "感恩": 2, "受教": 2, "麻烦你了": 2,
        "辛苦": 1, "辛苦了": 2, "费心": 1, "有心了": 2, "帮忙": 1,
        "帮大忙": 2, "拜谢": 3, "谢谢老板": 3, "太感谢": 3,
    },
    "拒绝": {
        "不行": 2, "不要": 2, "拒绝": 2, "别": 1, "不用": 1, "免了": 2,
        "不同意": 2, "不想": 2, "不去": 2, "不要了": 2, "不了": 1,
        "免谈": 2, "没兴趣": 2, "不感兴趣": 2, "下次吧": 2, "改天": 1,
        "再说": 1, "谢邀": 2, "打住": 2, "打咩": 2, "哒咩": 2,
        "不接受": 2, "否决": 2, "婉拒": 2, "不用了": 2,
    },
    "害羞": {
        "害羞": 3, "脸红": 3, "不好意思": 2, "羞": 2, "尬住": 2, "别夸": 2,
        "讨厌": 1, "捂脸": 2, "羞死": 3, "社死": 2, "社恐": 1, "臊": 2,
        "耳朵红": 2, "被夸": 1, "受宠若惊": 2, "过奖": 2, "哪有": 1,
        "没有啦": 1,
    },
    "日常": {},
}

MEME_CHANCE = 0.3  # 正常回复时附带表情包的概率
MEME_MOOD_HINTS = [
    (("难过", "委屈", "哭", "可惜", "唉"), "难过"),
    (("生气", "烦", "滚", "闭嘴", "气死"), "生气"),
    (("无语", "离谱", "服了", "翻白眼"), "无语"),
    (("笑", "开心", "哈哈", "好耶", "棒"), "开心"),
    (("累", "困", "睡", "躺"), "累了"),
    (("吃", "饭", "奶茶", "喝"), "吃喝"),
    (("谢谢", "感谢", "多谢"), "感谢"),
]

MEME_ROOT = Path(r"D:\QQNTBOT\apps\AstrBot\data\plugin_data\meme_manager\packs")
MEME_REQUEST = re.compile(
    r"表情包|发表情|发张图|发个图|来张图|来张表情|来点表情|来个表情"
    r"|整个表情|表情呢|斗图|卖萌|发图|图呢"
)
MEME_CATEGORY_HINTS = [
    (("卖萌", "可爱", "萌", "贴贴", "抱抱", "亲亲"), "开心"),
    (("无语", "翻白眼", "服了", "醉了", "麻了", "离谱"), "无语"),
    (("生气", "凶", "滚", "气死", "烦死", "火大"), "生气"),
    (("累", "困", "睡", "躺", "加班", "摆烂", "下班"), "累了"),
    (("难过", "哭", "委屈", "emo", "破防", "心碎"), "难过"),
    (("开心", "笑", "哈哈", "好耶", "棒", "爽"), "开心"),
    (("谢谢", "感谢", "多谢", "感恩"), "感谢"),
    (("吃", "饭", "奶茶", "喝", "饿了", "外卖", "火锅"), "吃喝"),
    (("惊讶", "卧槽", "震惊", "居然", "好家伙", "天哪"), "惊讶"),
    (("困惑", "不懂", "为什么", "啥意思", "咋"), "困惑"),
    (("拒绝", "不要", "不行", "不用", "算了", "打咩"), "拒绝"),
    (("害羞", "脸红", "不好意思", "社死", "捂脸"), "害羞"),
]

def _third_party_mode() -> bool:
    """Return True when the chat provider is the third-party API.

    Returns:
        Whether agent_runner currently points at DeepSeek.
    """
    import json
    from pathlib import Path as _Path

    try:
        cfg = json.loads(
            _Path(r"D:\QQNTBOT\apps\AstrBot\data\cmd_config.json").read_text(
                encoding="utf-8-sig"
            )
        )
        pid = str(
            cfg.get("agent_runner", {})
            .get("config", {})
            .get("model", {})
            .get("provider_id", "")
        )
        if pid:
            return "deepseek" in pid
    except Exception:  # noqa: BLE001
        pass
    state = _Path(r"D:\QQNTBOT\data\api_mode.json")
    if not state.exists():
        return False
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("mode") == "third_party"
    except Exception:  # noqa: BLE001
        return False


VISION_HINTS = (
    # 明确让看
    "截图", "看图", "看下", "看一下", "看看这", "瞅瞅", "认一下", "念一下",
    "认下", "念下",           # 群聊口语很少说「认一下」，多是「认下这是谁」
    # 指向这张图
    "这张图", "这个图", "这图", "图片", "图上", "图里", "图片里", "图是",
    # 在问内容
    "这是什么", "什么字", "写的啥", "写了什么", "写的是啥", "画的啥",
    "啥图", "什么图",
    "这啥", "是啥", "啥意思", "什么意思", "长啥样", "什么样",
    "识别", "翻译", "描述一下", "描述下",
    # 让它评价/打分这类也是冲着图来的（「评价一下」不带「看」字）
    "评价", "评一下", "点评", "打分", "咋样", "怎么样", "几分",
)

# 读图预算单列，不跟聊天共用。原图一张 1000~2000 token，描述一句约 30 token。
VISION_MAX_IMAGES = 2        # 一次请求最多放进几张原图
VISION_PROMPT_CHARS = 800    # 图片描述最多占多少字（截图类描述更长）
VISION_WAIT_SECONDS = 4.0    # 有图时肯多等几秒后台识图，换来不发原图
VISION_POLL_SECONDS = 0.25
# 截图/长图要给足字数：QQ空间截图、聊天记录一张就一大段文字，
# 90 字截断等于把内容全丢了
VISION_CONTENT_BYTES = 300 * 1024   # 超过这个体积当截图看
VISION_STICKER_CHARS = 90
VISION_CONTENT_CHARS = 320

# 「地图上」「图鉴」这类词里的「图」跟图片无关，先剔掉再匹配，
# 否则群里聊地图都会触发一次 1500 token 的读图
VISION_NOISE = ("地图", "图鉴", "图纸", "图文", "制图", "视图", "图书馆", "图库")


def _strip_vision_noise(text: str) -> str:
    """Remove compound words whose ``图`` is unrelated to an image.

    Args:
        text: Lower-cased message text.

    Returns:
        Text with noise compounds deleted.
    """
    for word in VISION_NOISE:
        text = text.replace(word, "")
    return text


def _should_read_images(text: str, images: list, waked: bool = False) -> bool:
    """Decide whether the third-party model should actually look at images.

    Stickers and reaction images should not burn vision tokens, so we only send
    images when the message looks like it is asking about the picture. A message
    with words in it is *not* enough — 群里刷表情包时配的文字通常是「哈哈哈」
    「笑死」，那不该触发读图。

    Args:
        text: The message text.
        images: Image payloads attached to the request.
        waked: True when the bot was @-mentioned / woken up. 特意 @ 它发图，
            就是冲着这张图来的——@+纯图却不给看是最伤的误判（模型只能装瞎）。

    Returns:
        True when the images are worth sending.
    """
    if not images:
        return False
    if waked:
        return True
    lowered = _strip_vision_noise((text or "").lower())
    if any(hint in lowered for hint in VISION_HINTS):
        return True
    if "?" in lowered or "？" in lowered:
        return True
    # 多图基本是在求助（对比/流程/长截图），单图更可能是表情
    if len(images) >= 3:
        return True
    # 图够大（长截图/照片）基本是内容图
    for url in images:
        if isinstance(url, str) and len(url) > 25000:
            return True
    return False


def _to_data_url(url: str) -> str:
    """Turn a local file path into a base64 data URL for the vision API.

    第三方模型下载不了本地路径——直接把 file:///D:/... 塞进 image_urls，
    模型只会在上下文里看到一串路径文本，然后老实承认「只看到路径」。
    http(s) 和 data: 原样放行；本地文件读出来转 base64；读不到返回空串。

    Args:
        url: Image reference, either a remote URL or a local path.

    Returns:
        A URL the third-party API can actually consume, or "" if unreadable.
    """
    if not url or url.startswith(("http://", "https://", "data:")):
        return url
    lowered = url.lower()
    # file:///D:/x.jpg（三斜杠，本地盘）切 8 位；file://host/x 是网络主机，切 7 位
    if lowered.startswith("file:///"):
        path = Path(url[8:])
    elif lowered.startswith("file://"):
        path = Path(url[7:])
    else:
        path = Path(url)
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    suffix = path.suffix.lstrip(".").lower()
    if suffix == "jpg":
        suffix = "jpeg"
    if suffix not in ("jpeg", "png", "gif", "webp", "bmp"):
        suffix = "jpeg"
    return (
        f"data:image/{suffix};base64,"
        + base64.b64encode(raw).decode("ascii")
    )


RECENT_PIN_DIR = Path(r"D:\QQNTBOT\data\temp\peer_recent")
PIN_MAX_BYTES = 20 * 1024 * 1024   # 超过 20MB 的不快照
PIN_KEEP_FILES = 300               # 目录超这个数就清 6 小时前的旧快照


def _url_path(url: str) -> Path | None:
    """Resolve a file:/// URL or plain path to a local Path, else None."""
    if not url:
        return None
    lowered = url.lower()
    if lowered.startswith("file:///"):
        return Path(url[8:])
    if lowered.startswith("file://"):
        return Path(url[7:])
    if url.startswith(("http://", "https://", "data:")):
        return None
    return Path(url)


async def _pin_image(url: str) -> str:
    """Snapshot a volatile image to a stable local path and return that path.

    QQ 发来的图在 NapCat 侧是 file:/// 临时文件，几十秒内就会被清掉；
    多媒体 URL 的 rkey 也会过期。第一轮读得到、跨轮重读就 404——
    「你刚发的那个是啥」这种追问全断在这。第一轮就把字节复制进固定
    目录（文件名=内容 sha，重发同一张不重复存），跨轮记忆改存快照路径。
    已经在快照目录里或读不到的，原样返回。

    Args:
        url: Image reference (http URL, file:/// URL or plain local path).

    Returns:
        A stable local path (str), or the original ``url`` when pinning is
        unnecessary or impossible.
    """
    if not url or url.startswith("data:"):
        return url
    src = _url_path(url)
    if src is None:
        # 远程 URL：下载一份。失败就退回原 URL，第一轮还能看
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            raw = resp.content
        except Exception as exc:  # noqa: BLE001 - pinning is best effort
            logger.debug(f"[peer] pin download failed: {exc}")
            return url
    else:
        try:
            raw = src.read_bytes()
        except OSError:
            return url
        # 表情包目录里的图是常驻资产，不用快照
        try:
            if MEME_ROOT in src.parents or src.parent == MEME_ROOT:
                return url
        except OSError:
            pass
    if not raw or len(raw) > PIN_MAX_BYTES:
        return url
    try:
        RECENT_PIN_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()[:16]
        head = raw[:4]
        suffix = (
            ".gif" if head[:3] == b"GIF"
            else ".png" if head == b"\x89PNG"
            else ".webp" if head[:4] == b"RIFF"
            else ".jpg"
        )
        pinned = RECENT_PIN_DIR / f"{digest}{suffix}"
        if not pinned.exists():
            pinned.write_bytes(raw)
        _pin_gc()
        return str(pinned)
    except OSError as exc:
        logger.debug(f"[peer] pin write failed: {exc}")
        return url


def _pin_gc() -> None:
    """Keep the snapshot dir small: drop 6-hour-old files when it grows big."""
    try:
        files = [p for p in RECENT_PIN_DIR.iterdir() if p.is_file()]
        if len(files) <= PIN_KEEP_FILES:
            return
        now = time.time()
        for old in files:
            if now - old.stat().st_mtime > 21600:
                old.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------- 跨轮图片记忆 / 按需细看
# 表情包包里本来就有分好类的类型描述（manifest.json），识图缓存里也有每张图
# 的画面描述。群里刚出现过一张图时，先把这段「大概是什么」常驻进上下文
# （零成本），只有对方仔细追问才升级到真看图。这样「这表情包啥意思」不用
# 烧 token，「里面怎么是小狗啊」才真的去看画面。
RECENT_IMAGE_TTL = 300.0           # 「上文刚有图」的有效期（秒）
DETAIL_COOLDOWN = 90.0             # 同一张图仔细看完后多久内不重复升级
MEME_MANIFEST = MEME_ROOT / "furry" / "manifest.json"
_RECENT_IMAGE: dict[str, dict] = {}
_MEME_DESC: dict = {"mtime": 0.0, "data": {}}


def _meme_category_desc(category: str) -> str:
    """Return the sticker pack's own short description for one category.

    Args:
        category: Folder name under ``furry/memes``.

    Returns:
        The Chinese description from manifest.json, or "" when unavailable.
    """
    if not category:
        return ""
    try:
        stat = MEME_MANIFEST.stat()
        if _MEME_DESC["mtime"] != stat.st_mtime:
            raw = json.loads(MEME_MANIFEST.read_text(encoding="utf-8"))
            cats = raw.get("categories", {})
            _MEME_DESC["data"] = {
                name: str((info or {}).get("description", "")).strip()
                for name, info in cats.items()
            }
            _MEME_DESC["mtime"] = stat.st_mtime
    except Exception as exc:  # noqa: BLE001 - 包坏了不能挡住发图
        logger.debug(f"[peer] meme manifest unreadable: {exc}")
        return ""
    return _MEME_DESC["data"].get(category, "")


def _event_key(event) -> str:
    """A short per-session key for the recent-image memory."""
    try:
        group = str(event.get_group_id() or "").strip()
        if group:
            return "g:" + group
        return "u:" + str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        return ""


def _remember_image(key: str, url: str, brief: str, sha: str = "", sent_by: str = "user") -> None:
    """Record the most recent image in a session for later follow-ups.

    Args:
        key: Per-group / per-user session key.
        url: Original image reference (path or URL), kept so we can re-read it.
        brief: Free rough description (pack category text or cached caption).
        sha: Image fingerprint, used to pick up a later background caption.
        sent_by: "bot" when the bot itself sent this image, else "user".
    """
    if not key or not url:
        return
    _RECENT_IMAGE[key] = {
        "url": url,
        "sha": sha,
        "brief": brief or "一张刚出现过的图（还没细看过）",
        "ts": time.time(),
        "sent_by": sent_by,
    }
    if len(_RECENT_IMAGE) > 64:
        now = time.time()
        for old_key, rec in list(_RECENT_IMAGE.items()):
            if now - rec["ts"] > RECENT_IMAGE_TTL:
                _RECENT_IMAGE.pop(old_key, None)


def _recent_image(key: str) -> dict:
    """Return a still-fresh recent image record, or an empty dict."""
    rec = _RECENT_IMAGE.get(key)
    if not rec:
        return {}
    if time.time() - rec["ts"] > RECENT_IMAGE_TTL:
        _RECENT_IMAGE.pop(key, None)
        return {}
    return rec


def _wants_image_detail(event, text: str) -> bool:
    """Structural check for a close-up question, without any keyword list.

    被 @ / 唤醒，或带问号，就算「仔细问这张图」。这里刻意不用词表：
    VISION_HINTS 那种枚举词已经踩过误触（地图上/图鉴）和漏判（认下/念下）
    的坑，再加一张表只会继续打补丁。

    Args:
        event: The current message event.
        text: Message text.

    Returns:
        True when the caller is likely asking about picture details.
    """
    waked = bool(getattr(event, "is_at_or_wake_command", False))
    return bool(waked or "?" in (text or "") or "？" in (text or ""))


async def _upgrade_recent_image(event, req, rec: dict) -> None:
    """Re-read the earlier image on demand, split by the current chat mode.

    第三方模式（deepseek-flash 自己支持 image）：把原图补进 image_urls 让主模型
    一次看完作答；本地模式（9B 不看图）：让 8085 出一份详细档描述注入 prompt。
    两条路都不二次生成回复。

    Args:
        event: The current message event.
        req: The LLM request being assembled.
        rec: Recent-image record from ``_recent_image``.
    """
    key = _event_key(event)
    url = rec.get("url", "")
    if _vision_mode() == "off":
        req.prompt = (
            (req.prompt or "")
            + "\n[图片] 识图关着，看不到刚才那张图的细节，别猜，直说看不了"
        )
        logger.info(f"[peer] detail upgrade skipped (vision off): {key}")
        return
    if _third_party_mode():
        data = _to_data_url(url)
        if not data:
            req.prompt = (
                (req.prompt or "")
                + "\n[图片] 刚才那张图取不到了，别编画面细节"
            )
            logger.warning(f"[peer] detail upgrade: image unreadable {key}")
            return
        req.image_urls = [data]
        req.prompt = (
            (req.prompt or "")
            + "\n[图片] 上面是刚才那张图的原图，仔细看画面细节再回答，"
            "别只照搬前面的大概描述"
        )
        rec["detail_at"] = time.time()
        logger.info(f"[peer] detail upgrade: original image sent ({key})")
        return
    # 本地模式：主模型不吃图，让本地 VL 出详细档
    detail = await _caption_local(url, detail=True)
    if detail.startswith("（读图能力不可用"):
        hit = _vision_peek(rec["sha"]) if rec.get("sha") else ""
        if hit:
            req.prompt = (req.prompt or "") + "\n[刚才那张图·仔细看] " + hit
            rec["brief"] = hit
            logger.info(f"[peer] detail upgrade: reused cached caption ({key})")
        else:
            req.prompt = (
                (req.prompt or "")
                + "\n[图片] 本地读图没起来，看不了细节，别猜"
            )
            logger.warning(f"[peer] detail upgrade: local VL down ({key})")
            return
    else:
        req.prompt = (req.prompt or "") + "\n[刚才那张图·仔细看] " + detail
        # 详细档写回记忆：之后同图再问就免费复用，不用重跑 VL
        rec["brief"] = detail
        logger.info(f"[peer] detail upgrade: local VL caption ({key})")
    rec["detail_at"] = time.time()


def _vision_mode() -> str:
    """Return the configured vision backend: local, third_party or off.

    Returns:
        The mode string from ``data/vision_mode.json``.
    """
    import json
    from pathlib import Path as _Path

    state = _Path(r"D:\QQNTBOT\data\vision_mode.json")
    if not state.exists():
        return "local"
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("mode", "local")
    except Exception:  # noqa: BLE001
        return "local"


def _caption_plan(size: int, raw: bytes = b"") -> tuple[str, int, int]:
    """Pick an instruction and length cap based on how the image looks.

    表情包一句话就够；QQ空间截图、聊天记录这类一张图就是一段文案，60 字
    会把内容全裁掉，之后所有复用都拿到残缺描述。

    Args:
        size: Raw image size in bytes.
        raw: Raw bytes, used to measure the aspect ratio when possible.

    Returns:
        ``(instruction, max_tokens, char_cap)``.
    """
    tall = False
    if raw:
        try:
            import io

            from PIL import Image as _Img

            with _Img.open(io.BytesIO(raw)) as im:
                w, h = im.size
            tall = h / max(w, 1) >= 1.35 or w / max(h, 1) >= 2.5
        except Exception:  # noqa: BLE001 - 量不出尺寸就按体积判断
            tall = False

    if size > VISION_CONTENT_BYTES or tall:
        return (
            "这是截图或长图。请把图上所有文字按从上到下的顺序尽量完整地抄下来"
            "（保留换行），再用一句话说明画面。文字多就都抄，不要总结，"
            f"最多 {VISION_CONTENT_CHARS} 字。",
            600,
            VISION_CONTENT_CHARS,
        )
    return (
        "用中文一句话说清这张表情包：有文字就照抄，再说画面和情绪，"
        f"不超过 {VISION_STICKER_CHARS} 字。",
        160,
        VISION_STICKER_CHARS,
    )


async def _caption_local(url: str, detail: bool = False) -> str:
    """Caption one image with the local VL server.

    Args:
        url: Data URL or local file path of the image.
        detail: Force the long-form instruction (used when someone asks about
            picture details on a follow-up turn, where the 90-char sticker
            caption is too coarse).

    Returns:
        Caption text, or an explicit unavailable marker.
    """
    import base64
    from pathlib import Path as _Path

    import httpx

    port = 8085
    state_path = _Path(r"D:\QQNTBOT\data\vision_mode.json")
    try:
        import json as _json

        port = int(_json.loads(state_path.read_text(encoding="utf-8")).get("port", 8085))
    except Exception:  # noqa: BLE001
        pass

    raw = b""
    if not url.startswith("data:"):
        try:
            raw = _Path(url).read_bytes()
        except OSError:
            return "（读图能力不可用）"
        url = "data:image/png;base64," + base64.b64encode(raw).decode()

    if detail:
        instruction, max_tokens, cap = (
            "仔细看这张图，把画面细节说清楚：主体是什么（是猫是狗还是别的）、"
            "在做什么、颜色、姿势、图上的文字照抄、有没有容易看错的地方。"
            "逐项说，别只概括情绪，不超过 320 字。",
            600,
            VISION_CONTENT_CHARS,
        )
    else:
        instruction, max_tokens, cap = _caption_plan(len(raw), raw)
    payload = {
        "model": "Qwen3VL-4B-Instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer sk-vl-local"},
            )
            resp.raise_for_status()
            text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        return text[:cap] if text else "（读图能力不可用）"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[peer] local VL failed: {exc}")
        return "（读图能力不可用）"


# ---------------------------------------------------------------- 读图缓存
# 同一张表情包会被反复发，每发一次跑一次 11 秒的本地 VL 纯属浪费。
# 以「图片内容 sha256」为主键缓存描述：命中就 0 秒 0 token 返回。
VISION_CACHE_DB = Path(r"D://QQNTBOT//data//vision_cache.db")
VISION_STICKER_BYTES = 1536 * 1024   # 与 meme_manager 的表情包体积上限一致
VISION_KEEP_DAYS = 30                # 大图描述保留多久
VISION_KEEP_HITS = 2                 # 大图低于这个调用次数，过期就清
VISION_GC_INTERVAL = 3600            # 最快多久清理一次
VISION_FLUSH_SECONDS = 60            # 调用计数攒多久落一次库

# 热缓存：群里就那么几十张图在反复刷，挡住 SQLite 的读
_VISION_MEM: dict[str, str] = {}
_VISION_MEM_MAX = 512
# 连接只用一条：每次 connect + PRAGMA + commit 要 30ms，比查表本身贵两个数量级
_VISION_CON = None
_VISION_LOCK = threading.Lock()
# hits/last_used 只写内存，攒够条数或到时间再批量落库，热路径一次 IO 都没有
_VISION_PENDING: dict[str, float] = {}
_VISION_FLUSH_AT = 0.0
_VISION_GC_AT = 0.0
# (cmd_config mtime, 白名单群号)：判断后台识图会不会跑
_WATCH_CACHE: tuple[float, set[str] | None] = (0.0, None)


def _vision_db():
    """Return the singleton cache connection, creating it on first use.

    Returns:
        A shared ``sqlite3.Connection`` in WAL mode.
    """
    global _VISION_CON

    if _VISION_CON is not None:
        return _VISION_CON
    VISION_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：GC 在 to_thread 里跑，跟主线程共用连接，靠锁串行化
    con = sqlite3.connect(VISION_CACHE_DB, timeout=3, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    # 主键 sha 自带索引，查询是 O(log n)；last_used 索引供清理用
    con.execute(
        "CREATE TABLE IF NOT EXISTS captions("
        "sha TEXT PRIMARY KEY, caption TEXT NOT NULL, bytes INTEGER NOT NULL,"
        " hits INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,"
        " last_used REAL NOT NULL)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_last_used ON captions(last_used)")
    con.commit()
    _VISION_CON = con
    return con


def _image_blob(url: str) -> tuple[bytes, str]:
    """Decode an image payload and fingerprint it.

    Hashing happens on the decoded bytes, so a PNG sent as ``data:`` URL and
    the same PNG saved to disk collapse to one cache entry.

    Args:
        url: Data URL, local path or anything else the caller passes around.

    Returns:
        ``(raw_bytes, sha256_hex)``; both empty when the payload is unreadable.
    """
    import base64
    import hashlib

    try:
        if url.startswith("data:"):
            _head, _sep, b64 = url.partition(",")
            raw = base64.b64decode(b64, validate=False)
        else:
            raw = Path(url).read_bytes()
    except Exception:  # noqa: BLE001 - 读不到就退化成不缓存
        return b"", ""
    if not raw:
        return b"", ""
    return raw, hashlib.sha256(raw).hexdigest()


def _vision_flush() -> None:
    """Write buffered hit counters back in one transaction."""
    global _VISION_FLUSH_AT

    if not _VISION_PENDING:
        return
    batch = list(_VISION_PENDING.items())
    _VISION_PENDING.clear()
    _VISION_FLUSH_AT = time.time()
    try:
        with _VISION_LOCK:
            con = _vision_db()
            con.executemany(
                "UPDATE captions SET hits = hits + 1, last_used = ? WHERE sha = ?",
                [(ts, sha) for sha, ts in batch],
            )
            con.commit()
    except Exception as exc:  # noqa: BLE001 - 计数丢了不影响读图
        logger.debug(f"[peer] vision cache flush failed: {exc}")


def _vision_touch(sha: str) -> None:
    """Queue one usage for batching.

    Args:
        sha: SHA-256 of the decoded image.
    """
    _VISION_PENDING[sha] = time.time()
    if (
        len(_VISION_PENDING) >= 64
        or time.time() - _VISION_FLUSH_AT > VISION_FLUSH_SECONDS
    ):
        _vision_flush()


def _vision_lookup(sha: str) -> str:
    """Return a cached caption, counting the hit.

    Args:
        sha: SHA-256 of the decoded image.

    Returns:
        The caption, or an empty string when not cached.
    """
    hit = _VISION_MEM.get(sha)
    if hit:
        # 重复赋值只为了把 key 挪到 dict 末尾，让 pop 第一个时是真正的 LRU
        _VISION_MEM[sha] = hit
    else:
        try:
            with _VISION_LOCK:
                row = _vision_db().execute(
                    "SELECT caption FROM captions WHERE sha = ?", (sha,)
                ).fetchone()
        except Exception as exc:  # noqa: BLE001 - 缓存坏了不能挡读图
            logger.debug(f"[peer] vision cache read failed: {exc}")
            return ""
        if not row:
            return ""
        hit = row[0]
        if len(_VISION_MEM) >= _VISION_MEM_MAX:
            _VISION_MEM.pop(next(iter(_VISION_MEM)), None)
        _VISION_MEM[sha] = hit
    _vision_touch(sha)
    return hit


def _vision_peek(sha: str) -> str:
    """Read a cached caption without counting a hit.

    Used while waiting for the background captioner: a poll that fails must
    not inflate ``hits``, otherwise the GC would keep images nobody used.

    Args:
        sha: SHA-256 of the decoded image.

    Returns:
        The caption, or an empty string when not cached.
    """
    hit = _VISION_MEM.get(sha)
    if hit:
        return hit
    try:
        with _VISION_LOCK:
            row = _vision_db().execute(
                "SELECT caption FROM captions WHERE sha = ?", (sha,)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[peer] vision cache read failed: {exc}")
        return ""
    if not row:
        return ""
    return row[0]


def _image_brief(url: str, fallback: str = "") -> tuple[str, str]:
    """Build a rough description for the recent-image memory.

    Prefers a caption already in the vision cache (most accurate, free); falls
    back to the sticker pack's category text or a neutral placeholder.

    Args:
        url: Image reference (local path or data URL).
        fallback: Text to use when no cached caption exists (e.g. a category
            description for a sticker the bot just sent).

    Returns:
        ``(rough_description, sha256_hex)``; sha is "" when unreadable.
    """
    _raw, sha = _image_blob(url)
    caption = _vision_peek(sha) if sha else ""
    brief = caption or fallback or "一张刚出现过的图（还没细看过）"
    return brief, sha


def _split_images(
    urls: list[str], budget: int = VISION_PROMPT_CHARS
) -> tuple[list[tuple[str, str]], list[tuple[str, str, int]], int]:
    """Sort images into「已有描述」和「待看图」两堆。

    Args:
        urls: Image payloads on the request.
        budget: Remaining characters allowed for injected captions.

    Returns:
        ``(notes, unseen, budget_left)`` where ``notes`` holds
        ``(caption, sha)`` and ``unseen`` holds ``(url, sha, size)``.
    """
    notes: list[tuple[str, str]] = []
    unseen: list[tuple[str, str, int]] = []
    left = budget
    for url in urls:
        raw, sha = _image_blob(url)
        cached = _vision_peek(sha) if sha else ""
        if cached:
            if left - len(cached) < 0:
                continue
            notes.append((cached, sha))
            left -= len(cached) + 3
        else:
            unseen.append((url, sha, len(raw)))
    return notes, unseen, left


def _vision_watches(group_id: str) -> bool:
    """Check whether ``vision_context`` captions this group.

    It only watches groups in AstrBot's ``id_whitelist``. Outside that list no
    background caption is coming, so waiting for one would just add latency.

    Args:
        group_id: Group number as a string.

    Returns:
        True when the background captioner is expected to run for this group.
    """
    global _WATCH_CACHE
    cfg_path = Path(r"D:\QQNTBOT\apps\AstrBot\data\cmd_config.json")
    try:
        mtime = cfg_path.stat().st_mtime
    except OSError:
        return False
    stamp, groups = _WATCH_CACHE
    if mtime != stamp:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            entries = cfg["platform_settings"]["id_whitelist"]
            groups = {str(e) for e in entries if str(e).isdigit()}
        except Exception:  # noqa: BLE001
            groups = set()
        _WATCH_CACHE = (mtime, groups)
    return group_id in (groups or set())


_PRIV_WATCH_CACHE: tuple[float, tuple[bool, set[str]]] = (0.0, (False, set()))


def _private_vision_watches(qq: str) -> bool:
    """Check whether ``vision_context`` also captions this private chat.

    Same rules as the ``/私聊`` whitelist: when ``enable_id_white_list`` is
    off everyone is allowed; when on, only ``qq:FriendMessage:<qq>`` entries.
    """
    global _PRIV_WATCH_CACHE
    cfg_path = Path(r"D:\QQNTBOT\apps\AstrBot\data\cmd_config.json")
    try:
        mtime = cfg_path.stat().st_mtime
    except OSError:
        return False
    stamp, cached = _PRIV_WATCH_CACHE
    if mtime != stamp:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            ps = cfg["platform_settings"]
            entries = ps.get("id_whitelist", [])
            cached = (not ps.get("enable_id_white_list", False), {
                str(e).rsplit(":", 1)[-1]
                for e in entries
                if "FriendMessage" in str(e)
            })
        except Exception:  # noqa: BLE001
            cached = (False, set())
        _PRIV_WATCH_CACHE = (mtime, cached)
    open_all, privs = cached
    return open_all or qq in privs


async def _wait_for_captions(
    unseen: list[tuple[str, str, int]], group_id: str, sender_id: str = ""
) -> float:
    """Give the background captioner a few seconds before sending raw images.

    Sending an image costs 1000~2000 tokens; waiting ~2 s for the caption costs
    nothing and usually wins. Bounded by ``VISION_WAIT_SECONDS`` and skipped
    entirely when the captioner is not watching this chat (group or private).

    Args:
        unseen: ``(url, sha, size)`` triples with no cached caption yet.
        group_id: Current group number, empty in private chats.
        sender_id: Sender QQ in private chats (ignored for groups).

    Returns:
        Seconds actually spent waiting (0 when nothing was waited for).
    """
    if group_id:
        if not _vision_watches(group_id):
            return 0.0
    elif sender_id:
        if not _private_vision_watches(sender_id):
            return 0.0
    else:
        return 0.0
    # 云端兜底只补 ≤1.5 MiB 的图，更大的等也等不到
    shas = [sha for _u, sha, size in unseen if sha and size <= VISION_STICKER_BYTES]
    if not shas:
        return 0.0
    start = time.time()
    deadline = start + VISION_WAIT_SECONDS
    while time.time() < deadline:
        if all(_vision_peek(sha) for sha in shas):
            break
        await asyncio.sleep(VISION_POLL_SECONDS)
    return time.time() - start


def _vision_store(sha: str, caption: str, size: int) -> None:
    """Persist a fresh caption.

    Args:
        sha: SHA-256 of the decoded image.
        caption: Description produced by the VL model.
        size: Size of the decoded image in bytes.
    """
    now = time.time()
    try:
        with _VISION_LOCK:
            con = _vision_db()
            con.execute(
                "INSERT INTO captions(sha, caption, bytes, hits, created, last_used)"
                " VALUES(?,?,?,1,?,?)"
                " ON CONFLICT(sha) DO UPDATE SET caption = excluded.caption",
                (sha, caption, size, now, now),
            )
            con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[peer] vision cache write failed: {exc}")
        return
    if len(_VISION_MEM) >= _VISION_MEM_MAX:
        _VISION_MEM.pop(next(iter(_VISION_MEM)), None)
    _VISION_MEM[sha] = caption
    _VISION_PENDING.pop(sha, None)


def _vision_gc() -> int:
    """Drop stale captions for large images.

    Sticker-sized images are kept forever regardless of hit count — they are
    cheap to hold and the most likely thing someone asks about later.

    Returns:
        Number of rows removed.
    """
    _vision_flush()
    cutoff = time.time() - VISION_KEEP_DAYS * 86400
    try:
        with _VISION_LOCK:
            con = _vision_db()
            cur = con.execute(
                "DELETE FROM captions WHERE last_used < ? AND hits < ? AND bytes > ?",
                (cutoff, VISION_KEEP_HITS, VISION_STICKER_BYTES),
            )
            con.commit()
            return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[peer] vision cache gc failed: {exc}")
        return 0


async def _caption_cached(url: str) -> tuple[str, bool]:
    """Caption an image, reusing the stored description when possible.

    Args:
        url: Data URL or local path of the image.

    Returns:
        ``(caption, from_cache)``.
    """
    global _VISION_GC_AT

    raw, sha = _image_blob(url)
    if sha:
        hit = _vision_lookup(sha)
        if hit:
            return hit, True
    caption = await _caption_local(url)
    # 读图服务挂了的那句占位文案不能进库，否则会永久污染这张图
    if sha and caption and not caption.startswith("（读图能力不可用"):
        _vision_store(sha, caption, len(raw))
    now = time.time()
    if now - _VISION_GC_AT > VISION_GC_INTERVAL:
        _VISION_GC_AT = now
        removed = await asyncio.to_thread(_vision_gc)
        if removed:
            logger.info(f"[peer] vision cache gc removed {removed} stale caption(s)")
    return caption, False


MARKER_RE = re.compile(r"&&[^&\r\n]{1,100}&&")


def _hoist_markers(text: str) -> str:
    """Move every ``&&...&&`` marker to the end of the message.

    表情插件要求标记在句末；模型经常写在句中，这里统一挪到最后并去重。

    Args:
        text: Raw reply text that may contain markers.

    Returns:
        Text with markers collected at the end, body otherwise untouched.
    """
    markers = MARKER_RE.findall(text)
    if not markers:
        return text
    body = MARKER_RE.sub("", text)
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    unique: list[str] = []
    for marker in markers:
        if marker not in unique:
            unique.append(marker)
    return (body + "\n" + " ".join(unique)).strip()


class Main(star.Star):
    """Persona owner, mute filter, and idle-group chatter."""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        text = load_persona()
        if text:
            chars = _apply_persona(text)
            logger.info(f"[peer] persona applied ({chars} chars) from {PERSONA_FILE.name}")
        else:
            logger.warning("[peer] 人格为空，未覆盖数据库 system_prompt（现有设置保持不变）")
        self._watch_task: asyncio.Task | None = None
        self._last_water: dict[str, float] = {}
        self._busy: dict[str, float] = {}
        self._last_done: dict[str, float] = {}
        self._active = 0
        self._blocked = 0
        self._injected = 0
        self._prompt_logged = False

    def _release(self, umo: str) -> None:
        """Clear the busy flag for a session."""
        if self._busy.pop(umo, None) is not None:
            self._active = max(0, self._active - 1)
        self._last_done[umo] = time.time()

    @filter.on_waiting_llm_request()
    async def gate(self, event: AstrMessageEvent) -> None:
        """Block injection attempts, duplicate requests and rapid-fire replies."""
        umo = event.unified_msg_origin
        text = (event.message_str or "").strip()
        now = time.time()

        # 以斜杠开头的消息一律当指令处理，不送进大模型
        raw_msg = None
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            raw_msg = getattr(msg_obj, "raw_message", None)
        raw_text = ""
        if isinstance(raw_msg, dict):
            raw_text = str(raw_msg.get("raw_message") or "")
        elif isinstance(raw_msg, str):
            raw_text = raw_msg
        if raw_text.lstrip().startswith(("/", "／")) or text.startswith(("/", "／")):
            logger.info(f"[peer] slash command not sent to model: {text[:30]}")
            event.stop_event()
            return

        if INJECTION.search(text):
            self._injected += 1
            logger.warning(f"[peer] injection blocked: {text[:60]}")
            event.stop_event()
            return

        # 要表情包就直接发，别让模型去编"我没有表情包"
        if MEME_REQUEST.search(text):
            if await self._send_random_meme(event, text):
                event.stop_event()
                return

        # @ / 唤醒类消息：一律放行，交给 AstrBot 的会话锁排队
        if getattr(event, "is_at_or_wake_command", False):
            logger.info(f"[peer] at-mention queued: {text[:30]}")
            return

        started = self._busy.get(umo)
        if started is not None:
            if now - started < BUSY_TIMEOUT:
                self._blocked += 1
                logger.info(f"[peer] busy, dropped: {text[:30]}")
                event.stop_event()
                return
            self._release(umo)  # 卡死超时，视为空闲

        if now - self._last_done.get(umo, 0.0) < COOLDOWN_SECONDS:
            self._blocked += 1
            logger.info(f"[peer] cooldown ({int(now - self._last_done[umo])}s), dropped")
            event.stop_event()
            return

        if self._active >= MAX_CONCURRENT:
            self._blocked += 1
            logger.info(f"[peer] global cap {MAX_CONCURRENT} reached, dropped")
            event.stop_event()
            return

        self._busy[umo] = now
        self._active += 1

    @filter.on_llm_response()
    async def release_after_reply(self, event: AstrMessageEvent, response) -> None:
        """Release the slot as soon as a reply is produced."""
        self._release(event.unified_msg_origin)

    @filter.after_message_sent()
    async def release_after_send(self, event: AstrMessageEvent) -> None:
        """Safety net: release the slot when the message has been sent."""
        self._release(event.unified_msg_origin)

    @filter.on_astrbot_loaded()
    async def start_watch(self) -> None:
        """Start the idle-group watcher once AstrBot is up."""
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._idle_loop())
            logger.info("[peer] idle watcher started")

    async def _idle_loop(self) -> None:
        """Every minute, water a group that has gone quiet."""
        while True:
            await asyncio.sleep(60)
            try:
                await self._maybe_water()
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.warning(f"[peer] idle watcher error: {exc}")

    async def _maybe_water(self) -> None:
        """Send one opener to each allowlisted group that has been idle."""
        now = time.time()
        if datetime.now().hour not in ACTIVE_HOURS:
            return
        allowed = _allowed_groups()
        overrides = load_water_config()
        for group, last in _group_activity().items():
            if group not in allowed:
                continue
            conf = overrides.get(group, {})
            if not conf.get("enabled", True):
                continue
            idle_limit = int(conf.get("idle_min", DEFAULT_IDLE_MIN)) * 60
            if now - last < idle_limit:
                continue
            if now - self._last_water.get(group, 0.0) < WATER_COOLDOWN:
                continue
            umo = f"qq:GroupMessage:{group}"
            line = random.choice(OPENERS)
            try:
                await self.context.send_message(umo, MessageChain([Plain(line)]))
                self._last_water[group] = now
                logger.info(
                    f"[peer] watered {group} (idle {int((now - last) / 60)}min): {line}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[peer] water failed for {group}: {exc}")

    async def _send_random_meme(self, event: AstrMessageEvent, text: str) -> bool:
        """Send a random sticker from the furry pack, bypassing the model.

        Args:
            event: The group message asking for a sticker.
            text: The raw message text, used to pick a matching category.

        Returns:
            True when a sticker was sent.
        """
        memes = MEME_ROOT / "furry" / "memes"
        if not memes.is_dir():
            logger.warning(f"[peer] meme pack missing: {memes}")
            return False
        category = ""
        for words, name in MEME_CATEGORY_HINTS:
            if any(word in text for word in words) and (memes / name).is_dir():
                category = name
                break
        pool = []
        if category:
            pool = [p for p in (memes / category).iterdir() if p.is_file()]
        if not pool:
            pool = [p for p in memes.rglob("*") if p.is_file()]
        if not pool:
            logger.warning("[peer] meme pack empty")
            return False
        chosen = random.choice(pool)
        try:
            await event.send(event.image_result(str(chosen)))
            logger.info(f"[peer] sent meme {chosen.parent.name}/{chosen.name}")
            desc = _meme_category_desc(chosen.parent.name)
            brief, sha = _image_brief(
                str(chosen), desc or f"一张「{chosen.parent.name}」类表情包"
            )
            _remember_image(_event_key(event), str(chosen), brief, sha, sent_by="bot")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[peer] meme send failed: {exc}")
            return False

    @staticmethod
    def _meme_scores(text: str, user_text: str = "") -> dict[str, int]:
        """Score every sticker category against one exchange.

        The user's words count double: if they are annoyed and the reply is
        cheerful, the sticker should match the person, not the sentence.

        Args:
            text: The bot's reply.
            user_text: The message that triggered it.

        Returns:
            Category name to score, only for categories with a hit.
        """
        scores: dict[str, int] = {}
        for name, words in MEME_EMOTION_WORDS.items():
            if not words:
                continue
            hit_bot = sum(wt for w, wt in words.items() if w and w in (text or ""))
            hit_user = sum(wt for w, wt in words.items() if w and w in (user_text or ""))
            if hit_bot or hit_user:
                scores[name] = hit_bot + hit_user * 2
        return scores

    def _attach_meme(self, result, text: str, user_text: str = "", event=None) -> None:
        """Append a random sticker to an outgoing reply.

        Args:
            result: The message result about to be sent.
            text: The reply text, used to pick a matching category.
            user_text: The message that triggered the reply.
            event: Originating event, used to remember the sticker per group.
        """
        memes = MEME_ROOT / "furry" / "memes"
        if not memes.is_dir() or result is None or not result.chain:
            return
        # 用户消息权重 2，机器人回复权重 1：跟谁的情绪走更准
        scores = self._meme_scores(text, user_text)
        category = ""
        if scores:
            best = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            top_name, top_score = best[0]
            # 只碰到一个弱词（比如「烦」「香」）就配图，多半和语气对不上。
            # 以前没命中时还会从全部 86 张里随机抓一张，违和感主要来自这里。
            if top_score < 2:
                logger.info(f"[peer] meme skipped, weak mood {top_name}={top_score}")
                return
            if (memes / top_name).is_dir():
                category = top_name
            logger.info(f"[peer] meme mood {category} scores={dict(best[:3])}")
        else:
            return
        pool = [p for p in (memes / category).iterdir() if p.is_file()] if category else []
        if not pool:
            return
        chosen = random.choice(pool)
        try:
            result.chain.append(Image.fromFileSystem(str(chosen)))
            logger.info(f"[peer] attached meme {chosen.parent.name}/{chosen.name}")
            desc = _meme_category_desc(chosen.parent.name)
            brief, sha = _image_brief(
                str(chosen), desc or f"一张「{chosen.parent.name}」类表情包"
            )
            if event is not None:
                _remember_image(
                    _event_key(event), str(chosen), brief, sha, sent_by="bot"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[peer] attach meme failed: {exc}")

    @filter.on_llm_request()
    async def log_system_prompt(self, event: AstrMessageEvent, req) -> None:
        """Log the assembled system prompt once per boot, to spot conflicts."""
        # 主模型是纯文本的：把图片转成文字，别让 9B 收到 image 参数（会 500）
        # 本地 9B 不支持图片输入，会 500；第三方 DeepSeek 支持，但只在必要时读图
        recent_key = _event_key(event)
        if req.image_urls:
            # 记住本轮这张图。「你这个表情包里怎么是小狗」这类跨轮追问
            # 本轮不带图，只有靠这条记录才能找回原图和大概描述。
            # 先快照：QQ 临时文件/远程 URL 几十秒就失效，跨轮必须用稳定路径
            pinned = await _pin_image(req.image_urls[0])
            _brief, _sha = _image_brief(pinned)
            _remember_image(recent_key, pinned, _brief, _sha)
            if not _third_party_mode():
                logger.info(f"[peer] stripped {len(req.image_urls)} image(s) from request")
                req.image_urls = []
            else:
                mode = _vision_mode()
                waked = bool(getattr(event, "is_at_or_wake_command", False))
                wants_image = _should_read_images(
                    event.message_str or "", req.image_urls, waked
                )
                if mode == "off" or not wants_image:
                    if mode == "off" and wants_image:
                        # 关掉识图时别装看得见：留一句话，让它老实说自己不看图
                        req.prompt = (
                            (req.prompt or "")
                            + "\n[图片] 识图关着，你看不到图，别猜内容，可以直说不看"
                        )
                    logger.info(f"[peer] vision skipped ({mode}): {len(req.image_urls)} image(s)")
                    req.image_urls = []
                elif mode == "local":
                    # 本地 VL 转文字，第三方模型完全接触不到图片
                    captions = []
                    hits = 0
                    total = len(req.image_urls)
                    budget = VISION_PROMPT_CHARS
                    for url in req.image_urls[:VISION_MAX_IMAGES]:
                        caption, cached = await _caption_cached(url)
                        if budget - len(caption) < 0:
                            break
                        captions.append(caption)
                        budget -= len(caption) + 3
                        hits += 1 if cached else 0
                    req.prompt = (req.prompt or "") + "\n[图片内容] " + " / ".join(captions)
                    req.image_urls = []
                    dropped = total - len(captions)
                    if dropped > 0:
                        req.prompt = (
                            (req.prompt or "")
                            + f"\n[图片] 另外还有 {dropped} 张没看"
                            "（太多/太长），不想看就直说，别猜内容"
                        )
                    logger.info(
                        f"[peer] local VL captioned {len(captions)} image(s), "
                        f"{hits} from cache, {dropped} dropped, "
                        f"vision budget left {budget}"
                    )
                else:
                    # 第三方模式：见过的图直接用已经存下来的描述，
                    # 一张图就是 1000~2000 token，能省就省
                    total = len(req.image_urls)
                    group_id = str(event.get_group_id() or "")
                    sender_id = "" if group_id else str(event.get_sender_id() or "")
                    notes, unseen, budget = _split_images(req.image_urls)
                    if unseen:
                        # 有图就多等几秒：后台 flash 补描述通常 1.5 秒就回来，
                        # 等到了就不用把原图发出去（省 1500 token，答案还更稳）
                        # 私聊也等：vision_context 现在私聊也写缓存
                        waited = await _wait_for_captions(
                            unseen, group_id, sender_id
                        )
                        if waited >= 0.5:
                            logger.info(
                                f"[peer] waited {waited:.1f}s for background caption"
                            )
                            notes, unseen, budget = _split_images(req.image_urls)

                    sent = min(len(unseen), VISION_MAX_IMAGES)
                    dropped = total - len(notes) - sent
                    if len(unseen) > VISION_MAX_IMAGES:
                        logger.info(
                            f"[peer] vision capped: {len(unseen)} unseen -> "
                            f"{VISION_MAX_IMAGES} sent"
                        )
                    if notes:
                        for _text, sha in notes:
                            _vision_touch(sha)
                        req.prompt = (
                            (req.prompt or "")
                            + "\n[图片内容] "
                            + " / ".join(text for text, _sha in notes)
                        )
                    req.image_urls = [url for url, _sha, _size in unseen[:VISION_MAX_IMAGES]]
                    # 本地路径必须转 base64，否则第三方模型只看到一串路径文本；
                    # 读不到的图撤下来并入「没看」口径，别让路径文本污染上下文
                    sendable = []
                    for _url in req.image_urls:
                        converted = _to_data_url(_url)
                        if converted:
                            sendable.append(converted)
                    unreadable = len(req.image_urls) - len(sendable)
                    req.image_urls = sendable
                    sent = len(sendable)
                    dropped += unreadable
                    if unreadable:
                        logger.warning(
                            f"[peer] {unreadable} image(s) unreadable "
                            "(local path), dropped from request"
                        )
                    if dropped > 0:
                        # 被裁掉的图不能装没事：模型不知道有图就会答得牛头不对马嘴。
                        # 告诉它「还有几张没看，可以直说不想看」，比让它瞎猜强。
                        req.prompt = (
                            (req.prompt or "")
                            + f"\n[图片] 另外还有 {dropped} 张没看"
                            "（太多/太长），不想看就直说，别猜内容"
                        )
                    logger.info(
                        f"[peer] third-party vision: {len(notes)} from cache, "
                        f"{sent} sent to model, {dropped} dropped, "
                        f"caption budget left {budget}"
                    )
        else:
            # 本轮没带图，但上文刚出现过一张图（跨轮追问）。
            # 先把已有的大概描述免费利用上，只有仔细问才升级到真看图。
            rec = _recent_image(recent_key)
            if rec:
                brief = (rec.get("brief", "") or "")[:120]
                # vision_context 也会注入「群里最近发过的图」，两边别重复
                already = (req.prompt or "") + (req.system_prompt or "")
                if brief and brief not in already:
                    if rec.get("sent_by") == "bot":
                        # 这张是机器人自己发的：明确告诉模型它发了什么，
                        # 免得下一轮被问「你刚发的那个是啥」时一问三不知
                        req.prompt = (
                            (req.prompt or "")
                            + f"\n[你刚才发的表情包] {brief}"
                            "（大概内容；对方细问画面就说看不清）"
                        )
                    else:
                        req.prompt = (
                            (req.prompt or "")
                            + f"\n[刚才那张图] {brief}"
                            "（大概描述，细节别编；对方细问就说看不清）"
                        )
                cooling = time.time() - rec.get("detail_at", 0) < DETAIL_COOLDOWN
                if cooling:
                    # 刚仔细看过同一张图，模型上下文里已有上一条带图回复，
                    # 别反复把原图塞进来白烧 token
                    logger.info(f"[peer] follow-up detail in cooldown: {recent_key}")
                elif _wants_image_detail(event, event.message_str or ""):
                    await _upgrade_recent_image(event, req, rec)
                    logger.info(f"[peer] follow-up detail upgrade: {recent_key}")
                else:
                    logger.info(f"[peer] follow-up brief reused: {recent_key}")

        # 群聊里给当前消息标上说话人，和历史块的格式保持一致
        group_id = str(event.get_group_id() or "")
        if group_id and not (req.prompt or "").startswith("["):
            nick = event.get_sender_name() or event.get_sender_id() or "群友"
            req.prompt = f"[{nick}]: {req.prompt}"
        if group_id:
            req.system_prompt = (
                (req.system_prompt or "")
                + "\n\n（群聊格式：每条消息前 [昵称] 表示说话人，"
                "不同昵称是不同的人；你自己是 assistant）"
            )
        if self._prompt_logged:
            return
        self._prompt_logged = True
        head = (req.system_prompt or "").replace("\n", " ")[:260]
        logger.info(f"[peer] system prompt head: {head}")

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """Drop empty/None model output, then apply markers, meme and text rules.

        Args:
            event: The event whose result is about to be sent.
        """
        result = event.get_result()
        if result is None or not result.chain:
            return
        try:
            text = result.get_plain_text().strip()
        except Exception:  # noqa: BLE001 - non-text results are left alone
            return
        # 模型报错时 AstrBot 会把 None 当字符串发出去，这里直接拦掉
        if text.lower() in ("none", "null", "nan", ""):
            logger.info("[peer] dropped empty/None reply")
            result.chain.clear()
            event.stop_event()
            return
        # 先把 &&情绪&& 之类的标记挪到句末，再套正则，避免规则改坏标记
        for comp in result.chain:
            inner = getattr(comp, "text", None)
            if isinstance(inner, str):
                comp.text = _hoist_markers(inner)
        text = result.get_plain_text().strip()
        if random.random() < load_reply_config()["meme_chance"]:
            self._attach_meme(result, text, event.message_str or "", event)
        for rule in load_reply_config()["text_rules"]:
            try:
                pattern = re.compile(rule["pattern"])
            except re.error as exc:
                logger.warning(f"[peer] bad regex {rule['pattern']}: {exc}")
                continue
            for comp in result.chain:
                inner = getattr(comp, "text", None)
                if isinstance(inner, str):
                    comp.text = pattern.sub(rule["to"], inner)
