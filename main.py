from core.plugin import BasePlugin, PluginContext, get_logger
from core.plugin import register
from core.chat import KiraMessageEvent, MessageChain, KiraMessageBatchEvent
from core.chat.message_elements import Text, At, Image
from core.plugin import on, Priority

import asyncio
import sys
import random
from pathlib import Path
from typing import Optional

logger = get_logger('plugin-PlusOne', 'orange')

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

        self.cache_output = None
        self.reset_counts()

        logger.info('PlusOne 插件加载完成！（浏览器检测将在后台进行）')

    async def terminate(self):
        pass

    def reset_counts(self):
        self.counts = 1
        self.cached_cache_output = self.cache_output
        self.cache_output = None
        self.random = random.randint(self.min_nums, self.max_nums) if self.mode == "random" else self.min_nums

    async def send_notice(self, session_id: str, content: str):
        chain = MessageChain([Text(content)])
        await self.ctx.publish_notice(
            session=session_id,
            chain=chain,
            is_mentioned=True
        )

    async def send_to_group(self, ada_name, group_id, content):
        chain = content if isinstance(content, MessageChain) else MessageChain([Text(content)])
        await self.ctx.adapter_mgr.get_adapter(ada_name).send_group_message(
            group_id=group_id,
            send_message_obj=chain
        )

    async def plus_one(self):
        # 这里可以实现你的+1逻辑，比如发送消息到群组
        logger.info("+1")
        # 示例：发送消息到指定群组
        await self.send_to_group(self.ada_name, self.group_id, self.cached_cache_output)

    
    @on.im_message(priority=Priority.HIGH)
    async def on_message(self, event: KiraMessageEvent, *args, **kwargs):

        if event.session.session_id in self.disallowed_sessions:
            return
        if self.cached_cache_output == event.message.chain:
            return
        if self.cache_output == event.message.chain:
            self.counts += 1
        self.cache_output = event.message.chain
        if self.counts >= self.random:
            self.ada_name = event.session.adapter_name
            self.group_id = event.message.group.group_id
            await self.send_notice(event.session.session_id, f'[System: 群友正在复读输出“{event.message_repr}”，如需加入，请使用<+1>Tag加入复读]')
            self.reset_counts()

    @register.tag(name="+1", description="使用<+1>Tag可以进行+1复读操作，与群友一起快乐地复读，输出“<msg>\n\t<+1>Yes</+1>\n</msg>”时表示进行+1操作，外部的Tag要和正常消息一样")
    async def handle_cancel_tag(self, value: str, **kwargs) -> list:
        # value 是标签内容，如 <my_tag>value</my_tag>
        # 返回 list[BaseMessageElement]
        if 'yes' in value.lower():
            await self.plus_one()

        return []
