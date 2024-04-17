"""
MIT License

Copyright (c) 2020-2023 PhenoM4n4n
Copyright (c) 2023-present japandotorg

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
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

log: logging.Logger = logging.getLogger("red.picklebump.core")

RequestType: TypeAlias = Literal["discord_deleted_user", "owner", "user", "user_strict"]

DISCORD_BOT_ID: Final[int] = 302050872383242240
LOCK_REASON: Final[str] = "Picklebump auto-lock"
MENTION_RE: Pattern[str] = re.compile(r"<@!?(\d{15,20})>")
BUMP_RE: Pattern[str] = re.compile(r"!d bump\b")

DEFAULT_GUILD_MESSAGE: Final[str] = (
    "It's been 2 hours since the last successful bump, could someone run </bump:947088344167366698>?"
)
DEFAULT_GUILD_THANKYOU_MESSAGE: Final[str] = (
    "{member(mention)} thank you for bumping! Make sure to leave a review at <https://disboard.org/server/{guild(id)}>."
)


class Picklebump(commands.Cog):
    """
    Set a reminder to bump on PickleJar on Runescape Discord.
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
            bot.add_dev_env_value("picklebump", lambda x: self)
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
            self.bot.remove_dev_env_value("picklebump")
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
    ) -> Dict[str, Any]:
        output = self.tagscript_engine.process(content, seed_variables)
        kwargs: Dict[str, Any] = {}
        if output.body:
            kwargs["content"] = output.body[:2000]
        if embed := output.actions.get("embed"):
            kwargs["embed"] = embed
        return kwargs

    async def initialize(self) -> None:
        async for guild_id, guild_data in AsyncIter(
            (await self.config.all_guilds()).items(), steps=100
        ):
            if not guild_id or not guild_data:
                continue
            channel_id: Optional[int] = guild_data.get("channel")
            if channel_id:
                self.channel_cache[guild_id] = channel_id

    async def bump_check_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            async for guild_id, guild_data in AsyncIter(
                (await self.config.all_guilds()).items(), steps=100
            ):
                if not guild_id or not guild_data:
                    continue
                channel_id: Optional[int] = guild_data.get("channel")
                role_id: Optional[int] = guild_data.get("role")
                message: str = guild_data.get("message", DEFAULT_GUILD_MESSAGE)
                ty_message: str = guild_data.get(
                    "ty_message", DEFAULT_GUILD_THANKYOU_MESSAGE
                )
                if not channel_id:
                    continue

                channel: Optional[discord.TextChannel] = self.bot.get_channel(channel_id)
                if not channel:
                    log.warning(
                        "Cannot find channel %s in guild %s. Skipping bump.",
                        channel_id,
                        guild_id,
                    )
                    continue

                role: Optional[discord.Role] = (
                    channel.guild.get_role(role_id) if role_id else None
                )

                if not role:
                    log.warning(
                        "Cannot find role %s in guild %s. Skipping bump.",
                        role_id,
                        guild_id,
                    )
                    continue

                now: datetime = datetime.now(timezone.utc)
                next_bump: Optional[datetime] = guild_data.get("next_bump")
                if not next_bump or now >= next_bump:
                    bump_task: asyncio.Task[discord.Message] = self.create_task(
                        self.bump(guild_id, channel, role, message, ty_message), name="Bump Task"
                    )
                    self.bump_tasks[guild_id]["bump"] = bump_task
                    await bump_task
                    await asyncio.sleep(1)
                    continue

                delta: float = (next_bump - now).total_seconds()
                await asyncio.sleep(delta)

    async def bump(
        self,
        guild_id: int,
        channel: discord.TextChannel,
        role: discord.Role,
        message: str,
        ty_message: str,
    ) -> discord.Message:
        if not channel.permissions_for(channel.guild.me).send_messages:
            log.warning(
                "I don't have permission to send messages to %s in guild %s. Skipping bump.",
                channel.id,
                guild_id,
            )
            return

        log.info(
            "Sending bump message to %s in guild %s. Role: %s, Message: %s",
            channel.id,
            guild_id,
            role.name,
            message,
        )

        async with channel.typing():
            bump_message: Optional[discord.Message] = None
            try:
                bump_message = await channel.send(
                    content=message, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.Forbidden:
                log.warning(
                    "I don't have permission to send messages to %s in guild %s. Skipping bump.",
                    channel.id,
                    guild_id,
                )
                return

            bump_task: asyncio.Task[discord.Message] = self.create_task(
                self.wait_for_bump(guild_id, bump_message), name="Wait for Bump Task"
            )
            self.bump_tasks[guild_id]["wait"] = bump_task
            await bump_task
            await asyncio.sleep(1)

            async for member in AsyncIter(
                role.members, steps=10, exceptions=discord.Forbidden
            ):
                try:
                    await member.send(
                        content=self.process_tagscript(ty_message, seed_variables={"guild": guild}),
                        allowed_mentions=discord.AllowedMentions(users=[member]),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

            next_bump: datetime = datetime.now(timezone.utc)  # type: ignore
            next_bump += timedelta(hours=2)
            await self.config.guild_from_id(guild_id).next_bump.set(next_bump)
            return bump_message

    async def wait_for_bump(
        self, guild_id: int, bump_message: discord.Message
    ) -> Optional[discord.Message]:
        def check(m: discord.Message) -> bool:
            return m.author.id == DISCORD_BOT_ID and BUMP_RE.search(m.content)

        try:
            bump = await self.bot.wait_for("message", check=check, timeout=15 * 60)
        except asyncio.TimeoutError:
            log.warning(
                "No bump detected for message %s in guild %s. Stopping.",
                bump_message.id,
                guild_id,
            )
            return

        return bump

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id == DISCORD_BOT_ID and BUMP_RE.search(message.content):
            log.info(
                "Disboard bump message detected in %s. Starting cooldown...",
                message.channel.id,
            )
            await asyncio.sleep(2 * 60 * 60)
            log.info("Cooldown ended for %s. Bump ready.", message.channel.id)
            await message.add_reaction("🍆")

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_channels=True)
    async def picklebumpset(self, ctx: commands.Context) -> None:
        """
        Set up picklebump.
        """
        pass

    @picklebumpset.command(name="channel")
    async def set_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """
        Set the channel where picklebump will send reminders.
        """
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"Picklebump channel set to {channel.mention}.")

    @picklebumpset.command(name="role")
    async def set_role(self, ctx: commands.Context, role: FuzzyRole) -> None:
        """
        Set the role to ping when sending reminders.
        """
        await self.config.guild(ctx.guild).role.set(role.id)
        await ctx.send(f"Picklebump role set to {role.name}.")

    @commands.command()
    async def pbump(self, ctx: commands.Context) -> None:
        """
        Bump your server.
        """
        await ctx.send("No longer supported.")


def setup(bot: Red) -> None:
    cog = Picklebump(bot)
    bot.add_cog(cog)

