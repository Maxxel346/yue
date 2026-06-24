"""
Music cog for the Discord bot.

Key changes from the old version (music_cog_copy.py):

1. Per-guild state. The old cog kept ONE queue / ONE voice client for the
   entire bot, so it could only work correctly in a single server at a time.
   Everything here is now stored per guild in `GuildState`.

2. Download-first playback. Instead of handing FFmpeg a raw YouTube URL and
   hoping the stream survives the whole song (the #1 source of the
   stuttering/dropouts you were seeing), we now download the audio fully to
   a local temp file with yt-dlp and play that local file. Local files don't
   suffer from network hiccups, YouTube throttling, or expiring signed URLs.

3. Background "prefetching". While a song is playing, the next song in the
   queue is downloaded in the background, so there's no gap waiting for a
   download once the current song ends.

4. A bigger toolkit: /play, /pause, /resume, /skip, /stop, /queue,
   /nowplaying, /loop, /shuffle, /remove, /move, /volume, /join — plus a
   small button panel on the "now playing" message.
"""

import asyncio
import logging
import os
import random
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from yt_dlp import YoutubeDL

logger = logging.getLogger("music_cog")

# Path to the ffmpeg executable. Override with the FFMPEG_PATH env var if
# ffmpeg isn't on your system PATH.
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

# Where downloaded audio lives before/while it's played.
CACHE_DIR = os.path.join(tempfile.gettempdir(), "discord_music_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Cap how many songs can be downloading at once across the whole bot, so we
# don't hammer YouTube (or the disk) when several guilds use the bot at once.
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)

YDL_LOOKUP_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "nocheckcertificate": True,
    "skip_download": True,
}

FFMPEG_PLAY_OPTIONS = {
    # We're playing a local file now, so none of the old "-reconnect" flags
    # (which only matter for a live remote stream) are needed anymore.
    "options": "-vn",
}


def _is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


class LoopMode:
    OFF = "off"
    SONG = "song"
    QUEUE = "queue"


@dataclass
class Song:
    title: str
    webpage_url: str
    duration: Optional[int]
    requester_name: str
    thumbnail: Optional[str] = None
    filepath: Optional[str] = None  # populated once downloaded to disk


@dataclass
class GuildState:
    queue: List[Song] = field(default_factory=list)
    voice_client: Optional[discord.VoiceClient] = None
    current: Optional[Song] = None
    text_channel: Optional[discord.abc.Messageable] = None
    loop_mode: str = LoopMode.OFF
    volume: float = 1.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prefetch_task: Optional[asyncio.Task] = None


class NowPlayingView(discord.ui.View):
    """Small control panel attached to the 'now playing' message."""

    def __init__(self, cog: "music_cog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    async def _state_if_allowed(self, interaction: discord.Interaction) -> Optional[GuildState]:
        state = self.cog.get_state(self.guild_id)
        user_voice = interaction.user.voice
        if not user_voice or (state.voice_client and user_voice.channel != state.voice_client.channel):
            await interaction.response.send_message(
                "Join the bot's voice channel to use these controls.", ephemeral=True
            )
            return None
        return state

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._state_if_allowed(interaction)
        if not state or not state.voice_client:
            return
        if state.voice_client.is_playing():
            state.voice_client.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)
        elif state.voice_client.is_paused():
            state.voice_client.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to pause/resume.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._state_if_allowed(interaction)
        if not state or not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        state.voice_client.stop()  # triggers the after-callback -> plays next
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._state_if_allowed(interaction)
        if not state:
            return
        state.queue.clear()
        if state.voice_client:
            state.voice_client.stop()
            await state.voice_client.disconnect()
            state.voice_client = None
        state.current = None
        await interaction.response.send_message("Stopped and left the channel.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._state_if_allowed(interaction)
        if not state:
            return
        order = [LoopMode.OFF, LoopMode.QUEUE, LoopMode.SONG]
        state.loop_mode = order[(order.index(state.loop_mode) + 1) % len(order)]
        await interaction.response.send_message(f"Loop mode set to **{state.loop_mode}**.", ephemeral=True)


class music_cog(commands.Cog):
    """Music bot cog: per-guild queues with download-then-play buffering."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: Dict[int, GuildState] = {}
        # Clear any leftover cache files from a previous crashed run.
        for f in os.listdir(CACHE_DIR):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except OSError:
                pass

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    def get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildState()
        return self.states[guild_id]

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        if not seconds:
            return "live/unknown"
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    async def _require_same_voice(self, interaction: discord.Interaction, state: GuildState) -> bool:
        """Make sure the command user is in the bot's voice channel before
        letting them control playback. Returns True if allowed."""
        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            await interaction.response.send_message("You need to be in a voice channel.", ephemeral=True)
            return False
        if state.voice_client and state.voice_client.channel != user_voice.channel:
            await interaction.response.send_message("You need to be in the same voice channel as the bot.", ephemeral=True)
            return False
        return True

    async def _lookup(self, query: str) -> Optional[Song]:
        """Resolve a search term or URL into song metadata. No download yet —
        this just needs to be fast so /play can confirm what it found."""
        loop = asyncio.get_event_loop()
        search_target = query if _is_url(query) else f"ytsearch1:{query}"

        def _extract():
            with YoutubeDL(YDL_LOOKUP_OPTS) as ydl:
                info = ydl.extract_info(search_target, download=False)
                if info and "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    info = entries[0] if entries else None
                return info

        try:
            info = await loop.run_in_executor(None, _extract)
        except Exception as e:
            logger.warning(f"Lookup failed for '{query}': {e}")
            return None

        if not info:
            return None

        thumbs = info.get("thumbnails") or []
        return Song(
            title=info.get("title", "Unknown title"),
            webpage_url=info.get("webpage_url") or info.get("url") or query,
            duration=info.get("duration"),
            requester_name="",
            thumbnail=thumbs[-1].get("url") if thumbs else None,
        )

    async def _download(self, song: Song) -> bool:
        """Fully download a song's audio to a local file so it can be played
        without depending on a live network stream. Returns True on success."""
        if song.filepath and os.path.exists(song.filepath):
            return True

        loop = asyncio.get_event_loop()
        out_template = os.path.join(CACHE_DIR, f"{uuid.uuid4().hex}.%(ext)s")

        def _do_download() -> Optional[str]:
            opts = dict(YDL_LOOKUP_OPTS)
            opts.pop("skip_download", None)
            opts.update(
                {
                    "outtmpl": out_template,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "opus",
                            "preferredquality": "192",
                        }
                    ],
                }
            )
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(song.webpage_url, download=True)
                if info and "entries" in info:
                    info = info["entries"][0]
                base, _ = os.path.splitext(ydl.prepare_filename(info))
                opus_path = base + ".opus"
                return opus_path if os.path.exists(opus_path) else None

        async with DOWNLOAD_SEMAPHORE:
            try:
                path = await loop.run_in_executor(None, _do_download)
            except Exception as e:
                logger.warning(f"Download failed for '{song.title}': {e}")
                return False

        if not path:
            return False
        song.filepath = path
        return True

    @staticmethod
    def _cleanup_file(path: Optional[str]):
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.debug(f"Could not remove cached file {path}: {e}")

    async def ensure_voice(self, channel: discord.VoiceChannel, state: GuildState) -> Optional[discord.VoiceClient]:
        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel != channel:
                try:
                    await state.voice_client.move_to(channel)
                except Exception as e:
                    logger.warning(f"Failed moving voice client: {e}")
                    return None
            return state.voice_client
        try:
            state.voice_client = await channel.connect(reconnect=True, timeout=15)
            return state.voice_client
        except Exception as e:
            logger.warning(f"Failed to connect to voice channel: {e}")
            return None

    def _start_prefetch(self, guild_id: int):
        """Kick off (if not already running) a background download of the
        next queued song so it's ready the instant the current one ends."""
        state = self.get_state(guild_id)
        if not state.queue:
            return
        if state.prefetch_task and not state.prefetch_task.done():
            return

        async def _prefetch():
            await self._download(state.queue[0])

        state.prefetch_task = asyncio.create_task(_prefetch())

    async def play_next(self, guild_id: int):
        state = self.get_state(guild_id)
        async with state.lock:
            if state.loop_mode == LoopMode.SONG and state.current:
                next_song = state.current
            else:
                old_current = state.current
                if state.loop_mode == LoopMode.QUEUE and old_current:
                    state.queue.append(old_current)
                elif old_current:
                    self._cleanup_file(old_current.filepath)

                if not state.queue:
                    state.current = None
                    return
                next_song = state.queue.pop(0)

            state.current = next_song

            if not next_song.filepath or not os.path.exists(next_song.filepath):
                ok = await self._download(next_song)
                if not ok:
                    if state.text_channel:
                        await state.text_channel.send(f"⚠️ Couldn't download **{next_song.title}** — skipping it.")
                    state.current = None
                    asyncio.create_task(self.play_next(guild_id))
                    return

            if not state.voice_client or not state.voice_client.is_connected():
                state.current = None
                return

            source = discord.FFmpegPCMAudio(next_song.filepath, executable=FFMPEG_PATH, **FFMPEG_PLAY_OPTIONS)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)

            def _after(error: Optional[Exception]):
                if error:
                    logger.warning(f"Player error: {error}")
                fut = asyncio.run_coroutine_threadsafe(self.play_next(guild_id), self.bot.loop)
                try:
                    fut.result()
                except Exception as e:
                    logger.warning(f"Error advancing queue: {e}")

            state.voice_client.play(source, after=_after)

            if state.text_channel:
                embed = discord.Embed(
                    title="🎶 Now playing",
                    description=f"**{next_song.title}**",
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Duration", value=self._format_duration(next_song.duration))
                if next_song.requester_name:
                    embed.add_field(name="Requested by", value=next_song.requester_name)
                if next_song.thumbnail:
                    embed.set_thumbnail(url=next_song.thumbnail)
                asyncio.create_task(state.text_channel.send(embed=embed, view=NowPlayingView(self, guild_id)))

        self._start_prefetch(guild_id)

    # ---------------------------------------------------------------- #
    # Listeners
    # ---------------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{self.bot.user} (music) has connected.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        # Auto-disconnect if the bot ends up alone in its voice channel for
        # too long, so it doesn't sit connected (and burning CPU) forever.
        if member.bot:
            return
        for guild_id, state in list(self.states.items()):
            vc = state.voice_client
            if not vc or not vc.channel:
                continue
            humans = [m for m in vc.channel.members if not m.bot]
            if humans:
                continue
            await asyncio.sleep(30)
            vc = state.voice_client
            if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
                await vc.disconnect()
                state.voice_client = None
                state.current = None
                state.queue.clear()

    def cog_unload(self):
        for state in self.states.values():
            if state.prefetch_task:
                state.prefetch_task.cancel()

    # ---------------------------------------------------------------- #
    # Commands
    # ---------------------------------------------------------------- #

    @app_commands.command(name="play", description="Play a song from a URL or search term")
    @app_commands.describe(query="YouTube link or search keywords")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            await interaction.followup.send("You need to be in a voice channel first.")
            return

        song = await self._lookup(query)
        if not song:
            await interaction.followup.send("Couldn't find anything for that query.")
            return
        song.requester_name = interaction.user.display_name

        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        vc = await self.ensure_voice(user_voice.channel, state)
        if not vc:
            await interaction.followup.send("Failed to connect to your voice channel.")
            return

        state.queue.append(song)

        if vc.is_playing() or vc.is_paused() or state.current:
            await interaction.followup.send(f"➕ Added to queue: **{song.title}** (position {len(state.queue)})")
            self._start_prefetch(interaction.guild_id)
        else:
            await interaction.followup.send(f"⏳ Downloading **{song.title}**...")
            await self.play_next(interaction.guild_id)

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        if not state.voice_client or not state.voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing right now.")
            return
        state.voice_client.pause()
        await interaction.response.send_message("⏸️ Paused.")

    @app_commands.command(name="resume", description="Resume paused music")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        if not state.voice_client or not state.voice_client.is_paused():
            await interaction.response.send_message("Music is not paused.")
            return
        state.voice_client.resume()
        await interaction.response.send_message("▶️ Resumed.")

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing to skip.")

    @app_commands.command(name="stop", description="Stop playback, clear the queue, and leave the channel")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        state.queue.clear()
        if state.voice_client:
            state.voice_client.stop()
            await state.voice_client.disconnect()
            state.voice_client = None
        if state.current:
            self._cleanup_file(state.current.filepath)
        state.current = None
        await interaction.response.send_message("⏹️ Stopped playback and left the channel.")

    @app_commands.command(name="clear", description="Clear the upcoming queue (keeps the current song playing)")
    async def clear_queue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        removed = len(state.queue)
        for s in state.queue:
            self._cleanup_file(s.filepath)
        state.queue.clear()
        await interaction.response.send_message(f"🧹 Cleared {removed} song(s) from the queue.")

    @app_commands.command(name="queue", description="Show the current song queue")
    async def show_queue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current and not state.queue:
            await interaction.response.send_message("The queue is empty.")
            return

        lines = []
        if state.current:
            lines.append(f"**Now playing:** {state.current.title} ({self._format_duration(state.current.duration)})")
        if state.queue:
            lines.append("")
            lines.append("**Up next:**")
            for idx, song in enumerate(state.queue[:10], start=1):
                lines.append(f"`{idx}.` {song.title} ({self._format_duration(song.duration)})")
            if len(state.queue) > 10:
                lines.append(f"...and {len(state.queue) - 10} more.")
        lines.append(f"\nLoop: **{state.loop_mode}**")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Show details about the current song")
    async def now_playing(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message("Nothing is playing right now.")
            return
        song = state.current
        embed = discord.Embed(title="🎶 Now playing", description=f"**{song.title}**", color=discord.Color.blurple())
        embed.add_field(name="Duration", value=self._format_duration(song.duration))
        if song.requester_name:
            embed.add_field(name="Requested by", value=song.requester_name)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="loop", description="Set the loop mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value=LoopMode.OFF),
            app_commands.Choice(name="Loop current song", value=LoopMode.SONG),
            app_commands.Choice(name="Loop whole queue", value=LoopMode.QUEUE),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        state.loop_mode = mode.value
        await interaction.response.send_message(f"🔁 Loop mode set to **{mode.name}**.")

    @app_commands.command(name="shuffle", description="Shuffle the upcoming queue")
    async def shuffle(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        if len(state.queue) < 2:
            await interaction.response.send_message("Not enough songs in the queue to shuffle.")
            return
        random.shuffle(state.queue)
        await interaction.response.send_message("🔀 Queue shuffled.")

    @app_commands.command(name="remove", description="Remove a song from the queue by its position")
    @app_commands.describe(position="Position in /queue to remove (1 = next up)")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message(f"Pick a position between 1 and {len(state.queue)}.")
            return
        removed = state.queue.pop(position - 1)
        self._cleanup_file(removed.filepath)
        await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from the queue.")

    @app_commands.command(name="move", description="Move a song to a different position in the queue")
    @app_commands.describe(frm="Current position", to="New position")
    async def move(self, interaction: discord.Interaction, frm: int, to: int):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        if frm < 1 or frm > len(state.queue) or to < 1 or to > len(state.queue):
            await interaction.response.send_message(f"Both positions must be between 1 and {len(state.queue)}.")
            return
        song = state.queue.pop(frm - 1)
        state.queue.insert(to - 1, song)
        await interaction.response.send_message(f"↕️ Moved **{song.title}** to position {to}.")

    @app_commands.command(name="volume", description="Set playback volume (0-200%)")
    @app_commands.describe(percent="Volume percentage, 0-200")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
        state = self.get_state(interaction.guild_id)
        if not await self._require_same_voice(interaction, state):
            return
        state.volume = percent / 100
        if state.voice_client and isinstance(state.voice_client.source, discord.PCMVolumeTransformer):
            state.voice_client.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to {percent}%.")

    @app_commands.command(name="join", description="Make the bot join your voice channel")
    async def join(self, interaction: discord.Interaction):
        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            await interaction.response.send_message("You must be connected to a voice channel for me to join.")
            return
        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel
        vc = await self.ensure_voice(user_voice.channel, state)
        if vc:
            await interaction.response.send_message(f"Joined {user_voice.channel.name}.")
        else:
            await interaction.response.send_message("Failed to join your voice channel.")
