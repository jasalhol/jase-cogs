import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import (
    Any,
    Coroutine,
    DefaultDict,
    Dict,
    Final,
    List,
    Literal,
    Match,
    Optional,
    Pattern,
    TypeAlias,
    Union,
)

import discord
import TagScriptEngine as tse
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import box

from .converters import FuzzyRole
from .models import LocalizedMessageValidator

log: logging.Logger = logging.getLogger("red.seina.picklebumper")

RequestType: TypeAlias = Literal["discord_deleted_user", "owner", "user", "user_strict"]

DISCORD_BOT_ID: Final[int] = 302050872383242240
LOCK_REASON: Final[str] = "Picklebumper auto-lock"
MENTION_RE: Pattern[str] = re.compile(r"<@!?(\d{15,20})>")
BUMP_RE: Pattern[str] = re.compile(r"!d bump\b")

DEFAULT_GUILD_MESSAGE: Final[str] = (
    "It's been 2 hours since the last successful bump, could someone run </bump:947088344167366698>?"
)
DEFAULT_GUILD_THANKYOU_MESSAGE: Final[str] = (
    "{member(mention)} thank you for bumping! Make sure to leave a review at <https://disboard.org/server/{guild(id)}>."
)


class Picklebumper(commands.Cog):
    """
    Set a reminder to bump on Disboard.
    """

    __version__: Final[str] = "1.3.7"
    __author__: Final[List[str]] = ["inthedark.org", "Phenom4n4n"]

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(
            self,
            identifier=69_420_666,
            force_registration=True,
        )
        default_guild: Dict[str, Union[Optional[int], str, bool]] = {
            "channel": None,
            "role": None,
            "message": DEFAULT_GUILD_MESSAGE,
            "ty_message": DEFAULT_GUILD_THANKYOU_MESSAGE,
            "next_bump": None,
            "lock": False,
            "clean": False,
        }
        self.config.register_guild(**default_guild)

        self.channel_cache: Dict[int, int] = {}
        self.bump_tasks: DefaultDict[int, Dict[str, asyncio.Task]] = defaultdict(dict)

        try:
            bot.add_dev_env_value("picklebumper", lambda x: self)
        except RuntimeError:
            pass

        blocks: List[tse.Block] = [
            tse.LooseVariableGetterBlock(),
            tse.AssignmentBlock(),
            tse.IfBlock(),
            tse.EmbedBlock(),
        ]
        self.tagscript_engine: tse.Interpreter = tse.Interpreter(blocks)

        self.bump_loop: asyncio.Task[Any] = self.create_task(self.bump_check_loop())
        self.initialize_task: asyncio.Task[Any] = self.create_task(self.initialize())

    async def cog_unload(self) -> None:
        try:
            self.__unload()
        except Exception as exc:
            log.exception(
                "An error occurred while unloading the cog. Version: %s",
                self.__version__,
                exc_info=exc,
            )

    def __unload(self) -> None:
        try:
            self.bot.remove_dev_env_value("picklebumper")
        except KeyError:
            pass
        if self.bump_loop:
            self.bump_loop.cancel()
        if self.initialize_task:
            self.initialize_task.cancel()
        for tasks in self.bump_tasks.values():
            for task in tasks.values():
                task.cancel()

    @staticmethod
    def task_done_callback(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            log.exception("Task failed.", exc_info=error)

    @staticmethod
    async def set_my_permissions(
        guild: discord.Guild, channel: discord.TextChannel, my_perms: discord.Permissions
    ) -> None:
        if not my_perms.send_messages:
            my_perms.update(send_messages=True)
            await channel.set_permissions(guild.me, overwrite=my_perms, reason=LOCK_REASON)  # type: ignore

    def create_task(
        self, coroutine: Coroutine, *, name: Optional[str] = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(self.task_done_callback)
        return task

    def process_tagscript(
        self, content: str, *, seed_variables: Dict[str, Any] = {}
    ) -> Union[str, discord.Embed]:
        try:
            result = self.tagscript_engine.run(content, seed_variables=seed_variables)
            if isinstance(result, discord.Embed):
                return result
            return str(result)
        except Exception as exc:
            log.error("An error occurred while processing TagScript.", exc_info=exc)
            return "An error occurred while processing the TagScript content."

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.check_bump(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None:
            return
        channel_id = payload.channel_id
        guild_id = payload.guild_id
        channel: discord.TextChannel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        try:
            message: discord.Message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        if not message.author.bot:
            await self.check_bump(message)

    async def check_bump(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if message.guild.me is None:
            return
        if message.guild.me.id != message.author.id:
            return
        if BUMP_RE.search(message.content):
            await self.bump(message)

    async def bump(self, message: discord.Message) -> None:
        channel: discord.TextChannel = message.channel
        guild: discord.Guild = channel.guild
        log.debug("Bump detected in channel %s of guild %s.", channel.id, guild.id)
        try:
            await message.add_reaction("👍")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass
        try:
            bump_task: asyncio.Task = self.bump_tasks[guild.id][channel.id]
            bump_task.cancel()
        except KeyError:
            pass
        self.bump_tasks[guild.id][channel.id] = self.create_task(self.set_next_bump(guild, channel))

    async def set_next_bump(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        async with self.config.guild(guild).next_bump() as next_bump:
            next_bump = datetime.utcnow() + timedelta(hours=2)
        await self.update_bump(guild, channel, next_bump)

    async def update_bump(
        self, guild: discord.Guild, channel: discord.TextChannel, next_bump: datetime
    ) -> None:
        bump_time = next_bump - datetime.utcnow()
        await asyncio.sleep(bump_time.total_seconds())
        await self.send_reminder(guild, channel)

    async def send_reminder(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        bump_message = await self.config.guild(guild).message()
        if bump_message is None:
            return
        await channel.send(bump_message)

    @commands.group(name="picklebump", aliases=["pb"])
    async def _pickle_bump(self, _: commands.Context):
        """
        Set a reminder to bump on Disboard.

        This sends a reminder to bump in a specified channel 2 hours after someone successfully bumps, thus making it more accurate than a repeating schedule.
        """

    @_pickle_bump.command(name="start")
    async def pickle_bump_start(
        self,
        ctx: commands.Context,
        interval: int,
        channel: discord.TextChannel,
        *,
        message: str,
    ) -> None:
        """
        Start a reminder for bumping.
        """
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await self.config.guild(ctx.guild).message.set(message)
        await ctx.send(
            f"The reminder has been started! I'll send '{message}' to {channel.mention} every {interval} hours."
        )

    @_pickle_bump.command(name="stop")
    async def pickle_bump_stop(self, ctx: commands.Context) -> None:
        """
        Stop the reminder for bumping.
        """
        await self.config.guild(ctx.guild).channel.clear()
        await self.config.guild(ctx.guild).message.clear()
        await ctx.send("The reminder has been stopped!")

    async def initialize(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.initialize_guild(guild)

    async def initialize_guild(self, guild: discord.Guild) -> None:
        if guild.me is None:
            return
        try:
            channel_id = await self.config.guild(guild).channel()
            if channel_id:
                channel: discord.TextChannel = guild.get_channel(channel_id)
                if channel:
                    bump_message = await self.config.guild(guild).message()
                    interval = await self.config.guild(guild).interval()
                    if bump_message and interval:
                        self.bump_tasks[guild.id][channel.id] = self.create_task(
                            self.set_next_bump(guild, channel)
                        )
        except Exception as exc:
            log.exception(
                "An error occurred while initializing the guild %s. Version: %s",
                guild.id,
                self.__version__,
                exc_info=exc,
            )

