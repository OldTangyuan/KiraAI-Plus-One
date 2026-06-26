from core.plugin import BasePlugin, PluginContext, get_logger
from core.plugin import register
from core.chat import KiraMessageEvent, MessageChain
from core.chat.message_elements import Text
from core.plugin import on, Priority

import random
from pathlib import Path

logger = get_logger('plugin-PlusOne', 'orange')


def _is_text_only(chain: MessageChain) -> bool:
    """判断消息是否仅包含 Text 元素（只有纯文本消息才能安全复读）"""
    return all(isinstance(ele, Text) for ele in chain)


def _extract_text(chain: MessageChain) -> str:
    """提取 MessageChain 中的纯文本内容"""
    return "".join(ele.text for ele in chain if isinstance(ele, Text))


class PlusOne(BasePlugin):
    def __init__(self, ctx: PluginContext, cfg: dict):
        super().__init__(ctx, cfg)
        self.data_dir: Path = None
        self.output_dir: Path = None

    async def initialize(self):
        """插件加载时调用，在此初始化资源、注册事件等"""
        self.data_dir = self.ctx.get_plugin_data_dir()
        self.output_dir = self.data_dir / "data.json"

        self.min_nums = self.plugin_cfg.get("min_nums", 2)
        self.max_nums = self.plugin_cfg.get("max_nums", 5)
        self.mode = self.plugin_cfg.get("mode", "random")
        self.disallowed_sessions = self.plugin_cfg.get("disallowed_sessions", [])

        # key=session_id, value=会话状态
        self._session_states: dict[str, dict] = {}
        # 最近一次触发复读的目标，供 +1 tag 使用
        self._last_triggered: dict = None

        logger.info('PlusOne 插件加载完成！')

    async def terminate(self):
        pass

    # ── 会话状态管理 ──────────────────────────────

    def _get_state(self, session_id: str) -> dict:
        """获取或创建指定会话的复读状态"""
        if session_id not in self._session_states:
            self._session_states[session_id] = {
                "count": 1,
                "cache_output": None,        # 上一条收到的消息（纯文本）
                "cached_cache_output": None,  # 上上条消息，即"被复读的内容"（纯文本）
                "threshold": self._calc_threshold(),
            }
        return self._session_states[session_id]

    def _calc_threshold(self) -> int:
        return random.randint(self.min_nums, self.max_nums) if self.mode == "random" else self.min_nums

    def _reset_state(self, state: dict):
        """触发复读后将当前消息归档为 cached，重置计数"""
        state["cached_cache_output"] = state["cache_output"]
        state["cache_output"] = None
        state["count"] = 1
        state["threshold"] = self._calc_threshold()

    # ── 消息发送 ──────────────────────────────

    async def send_notice(self, session_str: str, content: str):
        """session_str 格式: adapter_name:gm|dm:id"""
        chain = MessageChain([Text(content)])
        await self.ctx.publish_notice(
            session=session_str,
            chain=chain,
            is_mentioned=True,
        )

    async def send_to_group(self, ada_name, group_id, text: str):
        """向群组发送纯文本消息"""
        chain = MessageChain([Text(text)])
        await self.ctx.adapter_mgr.get_adapter(ada_name).send_group_message(
            group_id=group_id,
            send_message_obj=chain,
        )

    async def plus_one(self, target: dict):
        """执行 +1 复读，向 target 发送缓存的被复读文本"""
        if not target:
            logger.warning("+1 被调用但无可用的复读目标")
            return
        text = target.get("text", "").strip()
        if not text:
            logger.warning("+1 被调用但复读内容为空")
            return
        logger.info("+1")
        await self.send_to_group(
            target["ada_name"],
            target["group_id"],
            text,
        )

    # ── 事件处理 ──────────────────────────────

    @on.im_message(priority=Priority.HIGH)
    async def on_message(self, event: KiraMessageEvent, *args, **kwargs):
        session_id = event.session.session_id
        if session_id in self.disallowed_sessions:
            return

        # 只对纯文本消息进行复读检测，避免图片/回复/转发等不可重新发送的元素
        if not _is_text_only(event.message.chain):
            return

        current = _extract_text(event.message.chain)
        if not current.strip():
            return

        state = self._get_state(session_id)

        # 与已触发的复读内容相同 → 不参与计数
        if state["cached_cache_output"] is not None and \
           state["cached_cache_output"] == current:
            return

        # 与上一条消息相同 → 计数 +1；否则新内容，重置计数
        if state["cache_output"] is not None and \
           state["cache_output"] == current:
            state["count"] += 1
        else:
            state["count"] = 1

        state["cache_output"] = current

        if state["count"] >= state["threshold"]:
            target = {
                "ada_name": event.session.adapter_name,
                "group_id": event.message.group.group_id,
                "text": state["cached_cache_output"],
            }
            self._last_triggered = target
            await self.send_notice(
                str(event.session),
                f'[System: 群友正在复读输出"{event.message_repr}"，如需加入，请使用<plus1>Tag加入复读]',
            )
            self._reset_state(state)

    @register.tag(name="plus1", description="使用<plus1>Tag可以进行+1复读操作，与群友一起快乐地复读，输出“<msg>\n\t<plus1>Yes</plus1>\n</msg>”时表示进行+1操作，外部的Tag要和正常消息一样")
    async def handle_plus_one_tag(self, value: str, **kwargs) -> list:
        if "yes" in value.lower():
            await self.plus_one(self._last_triggered)
        return []
