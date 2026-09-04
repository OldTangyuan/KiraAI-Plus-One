from core.plugin import BasePlugin, PluginContext, get_logger
from core.plugin import register
from core.chat import KiraMessageEvent, MessageChain
from core.chat.message_elements import Text, Sticker
from core.plugin import on, Priority

import asyncio
import hashlib
import random
import re
import time
from pathlib import Path

logger = get_logger('plugin-PlusOne', 'orange')

# 形如 "<!--PIR:...-->" / "<!--xxx-->" 的整段纯占位符文本，一般是媒体/贴纸消息
# 在转文本过程中留下的占位标记，不是真实可复读的聊天内容，禁止参与复读计数。
_PLACEHOLDER_ONLY_RE = re.compile(r"^\s*<!--[\s\S]*?-->\s*$")

# 会话状态空闲多久后清理（秒）
_STATE_IDLE_TTL = 30 * 60
# 复读目标(_last_triggered) 过期时间：AI 在这之后输出 +1 视为无效，不再发送（秒）
_TARGET_TTL = 10 * 60
# 后台清理任务间隔（秒）
_PRUNE_INTERVAL = 60


def _digest(s: str) -> str:
    """把任意字符串折叠为定长 md5 摘要，避免在会话状态中长期保存大段文本/二进制"""
    return hashlib.md5((s or "").encode("utf-8", errors="replace")).hexdigest()


def _is_repeatable_element(ele) -> bool:
    """可参与复读的元素：纯文本(Text) 或 表情包(Sticker)。
    图片/回复/转发/语音等元素无法原样重发，一律不参与复读。"""
    return isinstance(ele, (Text, Sticker))


def _is_repeatable(chain: MessageChain) -> bool:
    """消息链是否整体可复读：非空，且只包含可复读元素"""
    if chain.is_empty():
        return False
    return all(_is_repeatable_element(ele) for ele in chain)


def _extract_text(chain: MessageChain) -> str:
    """提取 MessageChain 中的纯文本内容"""
    return "".join(ele.text for ele in chain if isinstance(ele, Text))


def _is_placeholder_only_text(text: str) -> bool:
    """整条文本是否只是占位符注释（如 <!--PIR:...-->）"""
    return bool(_PLACEHOLDER_ONLY_RE.match(text or ""))


def _truncate(text: str, max_len: int = 60) -> str:
    """截断超长文本，避免刷爆提示词"""
    if text is None:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _clone_repeatable_chain(chain: MessageChain) -> MessageChain:
    """复制一条只含 Text/Sticker 的消息链，用于稍后原样重发。
    只复制可重发的元素，避免持有接收时的对象引用。"""
    elements = []
    for ele in chain:
        if isinstance(ele, Text):
            elements.append(Text(ele.text))
        elif isinstance(ele, Sticker):
            elements.append(Sticker(
                sticker_id=ele.sticker_id,
                sticker=ele.sticker,
                mime=ele.mime,
                caption=ele.caption,
            ))
    return MessageChain(elements)


async def _chain_signature(chain: MessageChain) -> tuple:
    """计算消息链的结构签名，用于判断两条消息是否“相同内容”。
    文本与表情包内容都折叠成定长 md5，避免把大段文本/贴纸 base64 常驻在会话状态里。
    注意：hash_image() 是 async 方法，必须 await；计算失败时回退为内容原文的摘要，
    不会因个别贴纸无法取码而导致钩子异常。"""
    sig = []
    for ele in chain:
        if isinstance(ele, Text):
            sig.append(("text", _digest(ele.text)))
        elif isinstance(ele, Sticker):
            digest = None
            try:
                digest = await ele.hash_image()
            except Exception:
                digest = None
            # 兜底：hash 失败时对原始内容做摘要，保证可判等且不持有大体积原文
            sig.append(("sticker", str(ele.sticker_id) if ele.sticker_id is not None else "",
                        digest or _digest(ele.sticker or "")))
    return tuple(sig)


def _chain_has_sticker(chain: MessageChain) -> bool:
    return any(isinstance(ele, Sticker) for ele in chain)


def _describe_chain(chain: MessageChain) -> str:
    """生成给 AI 看的复读内容描述（纯文本截断；表情包用占位说明，不给乱码/二进制）"""
    text = _extract_text(chain).strip()
    if _chain_has_sticker(chain):
        if text:
            return f"{_truncate(text)}（并带表情包）"
        return "表情包(Sticker)"
    return _truncate(text)


class PlusOne(BasePlugin):
    def __init__(self, ctx: PluginContext, cfg: dict):
        super().__init__(ctx, cfg)
        self.data_dir: Path = None
        self.output_dir: Path = None
        self._task: asyncio.Task = None

    async def initialize(self):
        """插件加载时调用，在此初始化资源、注册事件等"""
        self.data_dir = self.ctx.get_plugin_data_dir()
        self.output_dir = self.data_dir / "data.json"

        self.min_nums = self.plugin_cfg.get("min_nums", 2)
        self.max_nums = self.plugin_cfg.get("max_nums", 5)
        self.mode = self.plugin_cfg.get("mode", "random")
        self.disallowed_sessions = self.plugin_cfg.get("disallowed_sessions", [])
        self.blacklist = [w.strip() for w in (self.plugin_cfg.get("blacklist", []) or []) if isinstance(w, str) and w.strip()]
        self.enable_interrupt = bool(self.plugin_cfg.get("enable_interrupt", False))
        self.interrupt_message = self.plugin_cfg.get("interrupt_message", "打断！")

        # key=sid ("adapter_name:gm:group_id"), value=会话状态
        # 状态只保存小体积字段：计数、阈值、最近/已触发内容的“签名”(定长 md5)、最后活跃时间
        self._session_states: dict[str, dict] = {}
        # 最近一次触发复读的群目标（含待重发的消息链克隆），供 +1/打断 tag 使用
        self._last_triggered: dict = None

        # 后台定期清理空闲会话状态与过期复读目标
        self._task = asyncio.create_task(self._prune_loop())

        # 依据配置动态更新 plus1 tag 描述，控制 AI 是否知道可以打断复读
        self._update_tag_description()

        logger.info('PlusOne 插件加载完成！')

    async def terminate(self):
        # 必须可重入：热重载会再次调用
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._session_states.clear()
        self._last_triggered = None

    # ── 后台清理 ──────────────────────────────

    async def _prune_loop(self):
        try:
            while True:
                await asyncio.sleep(_PRUNE_INTERVAL)
                self._prune()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("后台清理任务出错")

    def _prune(self):
        """清理空闲过久的会话状态，以及过期未消费的复读目标（释放其中的贴纸克隆等大对象）"""
        now = time.time()
        for sid in [s for s, st in self._session_states.items()
                    if now - st.get("ts", 0) > _STATE_IDLE_TTL]:
            self._session_states.pop(sid, None)
            logger.debug(f"清理空闲会话状态: {sid}")
        if self._last_triggered and now - self._last_triggered.get("ts", 0) > _TARGET_TTL:
            self._last_triggered = None

    # ── plus1 tag 描述管理 ──────────────────────────────

    def _build_plus1_description(self) -> str:
        """构建 plus1 tag 描述，依据 enable_interrupt 决定是否提示 AI 可用 No 打断复读"""
        desc = (
            "使用<plus1>Tag可以进行+1复读操作，与群友一起快乐地复读，"
            "输出“<msg>\n\t<plus1>Yes</plus1>\n</msg>”时表示进行+1操作"
        )
        if self.enable_interrupt:
            desc += (
                f"，输出“<msg>\n\t<plus1>No</plus1>\n</msg>”时表示不加入复读，"
                f"改为发送“{self.interrupt_message}”来打断复读"
            )
        return desc

    def _update_tag_description(self):
        """按配置改写已注册 plus1 tag 的描述（需在 core 正式注册 tag 前调用才生效）"""
        try:
            plugin_mgr = self.ctx.plugin_mgr
            if plugin_mgr is None:
                return
            plugin_id = plugin_mgr.get_plugin_id_for_module(__name__)
            comp = plugin_mgr.get_plugin_components().get(plugin_id)
            if comp is None:
                return
            for tag in comp.tags:
                if tag["name"] == "plus1":
                    tag["description"] = self._build_plus1_description()
                    return
        except Exception as e:
            logger.warning(f"更新 plus1 tag 描述失败: {e}")

    # ── 内容过滤 ──────────────────────────────

    def _is_blacklisted(self, content: str) -> bool:
        """内容是否包含黑名单词（子串匹配，忽略大小写）"""
        if not self.blacklist:
            return False
        lower_content = content.lower()
        return any(word.lower() in lower_content for word in self.blacklist)

    # ── 会话状态管理 ──────────────────────────────

    def _get_state(self, sid: str) -> dict:
        """获取或创建指定会话的复读状态"""
        if sid not in self._session_states:
            self._session_states[sid] = {
                "count": 1,
                # cache = 最近一条可复读消息的“签名”（定长，无大对象）
                # cached = 已触发过复读的内容签名，防止同一内容反复触发
                "cache": None,
                "cached": None,
                "threshold": self._calc_threshold(),
                "ts": time.time(),
            }
        return self._session_states[sid]

    def _calc_threshold(self) -> int:
        return random.randint(self.min_nums, self.max_nums) if self.mode == "random" else self.min_nums

    def _reset_state(self, state: dict, sig: tuple):
        """触发复读后将当前内容签名归档为 cached，重置计数。
        只保留定长签名，不保留任何大体积内容。"""
        state["cached"] = sig
        state["cache"] = None
        state["count"] = 1
        state["threshold"] = self._calc_threshold()

    def _is_disallowed_session(self, event: KiraMessageEvent) -> bool:
        """命中禁止复读的会话配置（兼容 sid / 裸 id 两种写法）"""
        if not self.disallowed_sessions:
            return False
        sid = str(event.session)
        ids = {str(event.session.session_id)}
        if event.message.group is not None:
            ids.add(str(event.message.group.group_id))
        for rule in self.disallowed_sessions:
            rule = str(rule).strip()
            if rule and (rule == sid or rule in ids):
                return True
        return False

    # ── 消息发送 ──────────────────────────────

    async def send_notice(self, session_str: str, content: str):
        """session_str 格式: adapter_name:gm|dm:id"""
        chain = MessageChain([Text(content)])
        await self.ctx.publish_notice(
            session=session_str,
            chain=chain,
            is_mentioned=True,
        )

    async def send_to_group(self, ada_name: str, group_id: str, chain: MessageChain):
        """向群组发送消息链（纯文本或表情包均可原样发送）"""
        if chain.is_empty():
            return
        await self.ctx.adapter_mgr.get_adapter(ada_name).send_group_message(
            group_id=group_id,
            send_message_obj=chain,
        )

    async def plus_one(self, target: dict):
        """执行 +1 复读：把待复读的消息链（文本或表情包）原样发到群里"""
        if not target:
            logger.warning("+1 被调用但无可用的复读目标")
            return
        # 目标过期（AI 回复太晚）→ 不再发送
        if time.time() - target.get("ts", 0) > _TARGET_TTL:
            logger.info("+1 目标已过期，忽略")
            return
        chain = target.get("chain")
        if chain is None:
            # 兼容旧的纯文本 target
            text = (target.get("text") or "").strip()
            if not text:
                logger.warning("+1 被调用但复读内容为空")
                return
            chain = MessageChain([Text(text)])
        if chain.is_empty():
            logger.warning("+1 被调用但复读内容为空")
            return
        logger.info("+1")
        await self.send_to_group(
            target["ada_name"],
            target["group_id"],
            chain,
        )

    async def interrupt(self, target: dict):
        """AI 选择打断复读时，向 target 所在的群发送自定义的打断内容"""
        if not target:
            logger.warning("打断被调用但无可用的复读目标")
            return
        if time.time() - target.get("ts", 0) > _TARGET_TTL:
            logger.info("打断目标已过期，忽略")
            return
        logger.info("打断复读")
        await self.send_to_group(
            target["ada_name"],
            target["group_id"],
            MessageChain([Text(self.interrupt_message)]),
        )

    # ── 事件处理 ──────────────────────────────

    @on.im_message(priority=Priority.HIGH)
    async def on_message(self, event: KiraMessageEvent, *args, **kwargs):
        # 只处理群聊：私聊/临时会话不参与复读，避免 group=None 引发的异常
        if not event.is_group_message():
            return
        if event.message.group is None:
            return

        sid = str(event.session)
        if self._is_disallowed_session(event):
            return

        # 跳过系统通知类消息，避免自循环导致状态污染
        if event.is_notice:
            return

        chain = event.message.chain

        # 只对可原样重发的内容（纯文本 / 表情包）进行复读检测；
        # 图片、回复、转发等不可重新发送的元素直接跳过。
        if not _is_repeatable(chain):
            return

        text = _extract_text(chain)

        # 既没有文字也没有表情包 → 无实际内容可复读
        if not text.strip() and not _chain_has_sticker(chain):
            return

        # 整条消息只是媒体占位符注释（如 <!--PIR:...-->）→ 不参与复读
        if _is_placeholder_only_text(text):
            return

        # 黑名单检测：包含黑名单词的消息不参与复读计数
        if self._is_blacklisted(text):
            return

        state = self._get_state(sid)
        state["ts"] = time.time()

        # 签名只含定长 md5，会话状态不会长期持有大段文本/贴纸 base64
        sig = await _chain_signature(chain)

        # 与已触发过的复读内容相同 → 不参与计数
        if state["cached"] is not None and state["cached"] == sig:
            return

        # 与上一条消息相同 → 计数 +1；否则视为新内容，重置计数
        if state["cache"] is not None and state["cache"] == sig:
            state["count"] += 1
        else:
            state["count"] = 1

        state["cache"] = sig

        if state["count"] >= state["threshold"]:
            # 只在触发时才克隆整条内容用于重发（当前消息与“被复读内容”相同），
            # 未触发时内存里只有签名，不缓存贴纸/文本本体。
            payload = _clone_repeatable_chain(chain)
            self._last_triggered = {
                "ada_name": event.session.adapter_name,
                "group_id": event.message.group.group_id,
                "chain": payload,
                "ts": time.time(),
            }
            is_sticker = _chain_has_sticker(payload)
            desc = f'群友正在复读“{_describe_chain(chain)}”'
            if is_sticker:
                desc += '，这是一个表情包，加入后插件会原样发送该表情包'
            await self.send_notice(
                sid,
                f'[System: {desc}，如需加入，请仅输出“<msg>\n\t<plus1>Yes</plus1>\n</msg>”加入复读]',
            )
            self._reset_state(state, sig)

    @register.tag(name="plus1", description="使用<plus1>Tag可以进行+1复读操作，与群友一起快乐地复读，输出“<msg>\n\t<plus1>Yes</plus1>\n</msg>”时表示进行+1操作")
    async def handle_plus_one_tag(self, value: str, **kwargs) -> list:
        value = (value or "").lower()
        if "yes" in value:
            target = self._last_triggered
            self._last_triggered = None
            await self.plus_one(target)
        elif "no" in value:
            # 无论是否启用“打断”，输出 No 都视为放弃加入 → 清空目标
            target = self._last_triggered
            self._last_triggered = None
            if self.enable_interrupt:
                await self.interrupt(target)
        return []
