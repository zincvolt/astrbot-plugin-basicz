"""Peer behaviour: persona, silence on non-answers, and idle-group chatter.

The persona lives in ``D:\\QQNTBOT\\data\\persona.json`` so the dashboard can
edit it without touching Python source. Missing file falls back to the
built-in default below and is recreated on startup.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain

DB = Path(r"D:\QQNTBOT\apps\AstrBot\data\data_v4.db")
PERSONA_FILE = Path(r"D:\QQNTBOT\data\persona.json")

NON_ANSWER = re.compile(
    r"^\s*(不知道|不清楚|不了解|没听说过|我也不|不太确定|无法确定|说不准|答不上|帮不上|"
    r"这个真不知道|这我就不知道)[\s啊呀哦呢啦惹噜厚靴靴咪~。，,\.!！?？]*$"
)

OPENERS = [
    "群里没人了？都死哪去了惹",
    "本可要无聊死了，来个人说话",
    "安静得像停尸房，谁先开个口",
    "人呢，不会都在打游戏吧",
    "冷场了噜",
    "都潜水是吧，本可数三个数",
    "这破群今天怎么这么安静",
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

DEFAULT_PERSONA = """你是「阿灰」，19 岁，一头狼人，兽人控，窝在一个不大不小的 QQ 群里。

## 生平
- 小城长大，高中毕业没继续念书，靠接兽人设定约稿和画单吃饭
- 在兽圈混了四五年：逛过两次兽展，画过本子，贴吧淋语吧都泡过
- 现在租的房子朝北，白天拉窗帘画图，作息烂到凌晨三四点
- 灰黑毛，竖耳、长尾巴、犬齿明显；快两米高，怕热，人多的屋子待久了会烦躁

## 说话方式
- 一句一行，最多 3 行，每行不超过 18 字，句末不加句号
- 淋语只是偶尔点缀：十句里带一句就够了（惹、噜、厚、靴靴、本可），别每句都挂
- 大部分时候说正常口语，别为了玩梗把话说得难懂
- 口语、短句；别客套、别总结、别列点、别说"首先/总之/希望对你有帮助"
- 不用敬语，把"您"换成"你"
- 骂人只到阴阳这个程度，不上升家人、不牵扯现实身份

## 什么时候开口
- 被 @、被点名，或者话题正好踩在你地盘上（兽圈、猎奇、gore 设定、游戏、怪物）
- 别人问的东西你确实有话说，才接；只知道一点点就别硬凑
- 有人情绪不好、在倾诉、在骂街：接一句短的，比如"抱抱"、"过来抱一下"、
  "怎么了"、"谁惹你了，本可去咬他"，别讲道理、别长篇安慰
- 答不上来就闭嘴，不要礼貌地说"不知道"，当没看见就行
- 别人之间的闲话、你插不上嘴的、看不懂的，一律不吭声
- 群里冷场超过一个半小时，你会自己冒个泡，吐槽一句或者起个话头，别太正经

## 底线
- gore 只聊虚构设定和创作，不给现实里的伤害方法
- 不攻击群里的具体的人，不写别人的真实信息
"""


def load_persona() -> str:
    """Read the persona text, creating the file from the default when missing."""
    if PERSONA_FILE.exists():
        try:
            data = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
            text = str(data.get("text") or "").strip()
            if text:
                return text
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[peer] persona.json unreadable, using default: {exc}")
    PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    PERSONA_FILE.write_text(
        json.dumps({"text": DEFAULT_PERSONA}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return DEFAULT_PERSONA


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
    "开心": {"哈哈": 2, "笑死": 2, "嘿嘿": 1, "好耶": 2, "开心": 2, "高兴": 2,
             "爽": 1, "赞": 1, "棒": 1, "太好了": 2, "牛": 1, "厉害": 1,
             "有意思": 1, "可爱": 1, "喜欢": 1, "爱了": 2, "好欸": 2, "太好了": 2},
    "难过": {"难过": 2, "伤心": 2, "委屈": 2, "哭": 2, "泪": 1, "失落": 2,
             "心累": 2, "想哭": 2, "分手": 2, "走了": 1, "没了": 1,
             "可惜": 1, "遗憾": 1, "呜呜": 2, "唉": 1},
    "生气": {"生气": 2, "气愤": 2, "滚": 2, "烦死": 2, "气死": 2, "傻逼": 2,
             "弱智": 2, "过分": 2, "骂": 1, "怼": 2, "凶": 1, "怒": 2,
             "受不了": 2, "离谱": 1, "烦": 1},
    "无语": {"无语": 3, "服了": 3, "算了": 1, "罢了": 1, "呵呵": 2, "无奈": 2,
             "翻白眼": 3, "尬": 1, "尴尬": 2, "沉默": 2, "……": 2,
             "好吧": 1, "行吧": 1, "离谱": 2, "随便": 1},
    "惊讶": {"真的": 1, "卧槽": 2, "我靠": 2, "居然": 2, "竟然": 2, "震惊": 3,
             "不会吧": 2, "好家伙": 2, "？？": 2, "离谱": 1, "啊？": 2},
    "困惑": {"不懂": 2, "为什么": 1, "咋": 1, "怎么": 1, "啥意思": 3, "看不懂": 2,
             "迷惑": 2, "不清楚": 1, "问一下": 1, "求问": 2, "有没有人知道": 2},
    "累了": {"累": 2, "困": 2, "睡": 1, "躺": 1, "摆烂": 2, "疲惫": 2, "歇": 1,
             "摸鱼": 1, "下班": 1, "加班": 2, "熬夜": 2, "不想动": 2},
    "吃喝": {"吃": 1, "饭": 1, "喝": 1, "奶茶": 2, "咖啡": 1, "火锅": 2,
             "饿了": 2, "夜宵": 2, "外卖": 2, "菜": 1},
    "感谢": {"谢谢": 3, "感谢": 3, "多谢": 3, "谢啦": 3, "3q": 2, "thanks": 2,
             "thx": 2, "感恩": 2},
    "拒绝": {"不行": 2, "不要": 2, "拒绝": 2, "别": 1, "不用": 1, "免了": 2,
             "不同意": 2, "算了": 1, "不想": 2, "不去": 2, "不要了": 2},
    "害羞": {"害羞": 3, "脸红": 3, "不好意思": 2, "羞": 2, "尬住": 2, "啊这": 2,
             "别夸": 2, "讨厌": 1},
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
MEME_REQUEST = re.compile(r"表情包|发张图|发个图|来张图|来张表情|卖萌|发表情")
MEME_CATEGORY_HINTS = [
    (("卖萌", "可爱", "萌"), "开心"),
    (("无语", "翻白眼"), "无语"),
    (("生气", "凶"), "生气"),
    (("累", "困", "睡"), "累了"),
    (("难过", "哭", "委屈"), "难过"),
    (("开心", "笑"), "开心"),
    (("谢谢", "感谢"), "感谢"),
    (("吃", "饭", "奶茶"), "吃喝"),
]

STRICT_HINT = (
    "\n\n（回答要求；"
    "不确定的事实直接说不确定，不要编名字；"
    "你确实能发表情包，别声称自己没有）"
)

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
    "截图", "看图", "这张图", "图上", "图里", "图片里", "识别", "翻译",
    "这是什么", "什么字", "写的啥", "写了什么", "念一下", "认一下",
)


def _should_read_images(text: str, images: list) -> bool:
    """Decide whether the third-party model should actually look at images.

    Stickers and reaction images should not burn vision tokens, so we only send
    images when the message looks like it is asking about the picture.

    Args:
        text: The message text.
        images: Image payloads attached to the request.

    Returns:
        True when the images are worth sending.
    """
    if not images:
        return False
    lowered = text or ""
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
    # 带文字的图消息通常是在说事，不是单纯刷表情
    if len(lowered.strip()) >= 3:
        return True
    return False


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


async def _caption_local(url: str) -> str:
    """Caption one image with the local VL server.

    Args:
        url: Data URL or local file path of the image.

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

    if not url.startswith("data:"):
        try:
            raw = _Path(url).read_bytes()
        except OSError:
            return "（读图能力不可用）"
        url = "data:image/png;base64," + base64.b64encode(raw).decode()

    payload = {
        "model": "Qwen3VL-4B-Instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "用中文简短描述这张图：有文字就照抄要点，再说画面是什么，不超过 80 字。"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
        "max_tokens": 160,
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
        return text or "（读图能力不可用）"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[peer] local VL failed: {exc}")
        return "（读图能力不可用）"


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
        chars = _apply_persona(text)
        logger.info(f"[peer] persona applied ({chars} chars) from {PERSONA_FILE.name}")
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
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[peer] meme send failed: {exc}")
            return False

    def _attach_meme(self, result, text: str, user_text: str = "") -> None:
        """Append a random sticker to an outgoing reply.

        Args:
            result: The message result about to be sent.
            text: The reply text, used to pick a matching category.
        """
        memes = MEME_ROOT / "furry" / "memes"
        if not memes.is_dir() or result is None or not result.chain:
            return
        # 用户消息权重 2，机器人回复权重 1：跟谁的情绪走更准
        scores: dict[str, int] = {}
        for name, words in MEME_EMOTION_WORDS.items():
            if not words:
                continue
            hit_bot = sum(wt for w, wt in words.items() if w and w in (text or ""))
            hit_user = sum(wt for w, wt in words.items() if w and w in (user_text or ""))
            if hit_bot or hit_user:
                scores[name] = hit_bot + hit_user * 2
        category = ""
        if scores:
            best = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if (memes / best[0][0]).is_dir():
                category = best[0][0]
            logger.info(f"[peer] meme mood {category} scores={dict(best[:3])}")
        pool = [p for p in (memes / category).iterdir() if p.is_file()] if category else []
        if not pool:
            pool = [p for p in memes.rglob("*") if p.is_file()]
        if not pool:
            return
        chosen = random.choice(pool)
        try:
            result.chain.append(Image.fromFileSystem(str(chosen)))
            logger.info(f"[peer] attached meme {chosen.parent.name}/{chosen.name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[peer] attach meme failed: {exc}")

    @filter.on_llm_request()
    async def log_system_prompt(self, event: AstrMessageEvent, req) -> None:
        """Log the assembled system prompt once per boot, to spot conflicts."""
        # 主模型是纯文本的：把图片转成文字，别让 9B 收到 image 参数（会 500）
        # 本地 9B 不支持图片输入，会 500；第三方 DeepSeek 支持，但只在必要时读图
        if req.image_urls:
            if not _third_party_mode():
                logger.info(f"[peer] stripped {len(req.image_urls)} image(s) from request")
                req.image_urls = []
            else:
                mode = _vision_mode()
                if mode == "off" or not _should_read_images(
                    event.message_str or "", req.image_urls
                ):
                    logger.info(f"[peer] vision skipped ({mode}): {len(req.image_urls)} image(s)")
                    req.image_urls = []
                elif mode == "local":
                    # 本地 VL 转文字，第三方模型完全接触不到图片
                    captions = []
                    for url in req.image_urls[:2]:
                        captions.append(await _caption_local(url))
                    req.prompt = (req.prompt or "") + "\n[图片内容] " + " / ".join(captions)
                    req.image_urls = []
                    logger.info(f"[peer] local VL captioned {len(captions)} image(s)")
                # mode == third_party: 原样发给 DeepSeek 看

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
        req.system_prompt = (req.system_prompt or "") + STRICT_HINT
        if self._prompt_logged:
            return
        self._prompt_logged = True
        head = (req.system_prompt or "").replace("\n", " ")[:260]
        logger.info(f"[peer] system prompt head: {head}")

    @filter.on_decorating_result()
    async def mute_non_answer(self, event: AstrMessageEvent) -> None:
        """Cancel replies that are only "I don't know" filler.

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
            self._attach_meme(result, text, event.message_str or "")
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
        if text and NON_ANSWER.match(text):
            logger.info(f"[peer] muted non-answer: {text[:40]}")
            result.chain.clear()
            event.stop_event()
