from ast import alias
import discord
from discord import app_commands
from discord.ext import commands
from youtubesearchpython import VideosSearch
from yt_dlp import YoutubeDL
import asyncio
import os
from typing import Optional, List, Tuple, Dict

current_dir = os.path.dirname(__file__)
print(current_dir)
# add ffmpeg_master/bin/ffmpeg.exe to the ffmpegPath
# ffmpegPath = os.path.join(current_dir, "ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe")
# ffmpeg-2025-02-26-git-99e2af4e78-full_build\ffmpeg-2025-02-26-git-99e2af4e78-full_build\bin\ffmpeg.exe
# ffmpegPath = os.path.join(current_dir, "ffmpeg-2025-02-26-git-99e2af4e78-full_build/ffmpeg-2025-02-26-git-99e2af4e78-full_build/bin/ffmpeg.exe")
FFMPEG_PATH = "ffmpeg"

# print(ffmpegPath)

bot = commands.Bot(command_prefix='/', intents=discord.Intents.all())

class music_cog(commands.Cog):
    """Music bot cog handling playing, queue, and control."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Playback state
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.is_looping: bool = False

        # Queue of tuples (song info dict, voice channel)
        self.music_queue: List[Tuple[Dict[str, str], discord.VoiceChannel]] = []

        # YoutubeDL and ffmpeg options
        self.ydl_opts = {'format': 'bestaudio/best', 'noplaylist': True}
        self.ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel quiet -hwaccel qsv",
            "options": "-vn"
        }
        self.ytdl = YoutubeDL(self.ydl_opts)

        # Voice client
        self.vc: Optional[discord.VoiceClient] = None

    def search_youtube(self, query: str) -> Optional[Dict[str, str]]:
        """
        Search for a video on YouTube and return a dict with 'source' (url) and 'title'.
        If query is a direct URL, extract title without download.
        """
        if query.startswith("https://") or query.startswith("http://"):
            try:
                info = self.ytdl.extract_info(query, download=False)
                title = info.get("title", None)
                if not title:
                    return None
                return {"source": query, "title": title}
            except Exception as e:
                print(f"Failed to extract info from URL {query}: {e}")
                return None

        # Search by keywords
        try:
            # print("Searching YouTube for:", query)
            # videos_search = VideosSearch(query, limit=1)
            # print(videos_search.result())
            # result = videos_search.result()
            # first_result = result.get("result", [None])[0]
            # print(first_result)
            # if not first_result:
            #     return None
            # return {"source": first_result["link"], "title": first_result["title"]}
            ydl_opts = {'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True}
            with YoutubeDL(ydl_opts) as ydl:
                # The actual search query with ytsearch1:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                if 'entries' in info and len(info['entries']) > 0:
                    video = info['entries'][0]
                    return {"source": video['webpage_url'], "title": video['title']}
            return None
        except Exception as e:
            print(f"Failed to search YouTube for {query}: {e}")
            return None

    async def ensure_voice(self, channel: discord.VoiceChannel) -> Optional[discord.VoiceClient]:
        """
        Connects to the given voice channel or moves if already connected.
        """
        if self.vc and self.vc.is_connected():
            if self.vc.channel != channel:
                try:
                    await self.vc.move_to(channel)
                except Exception as e:
                    print(f"Error moving voice client: {e}")
                    return None
            return self.vc
        else:
            try:
                self.vc = await channel.connect()
                return self.vc
            except Exception as e:
                print(f"Failed to connect to voice channel: {e}")
                return None

    def after_song(self, error):
        """
        Called after a song finishes playing.
        Beware: This is called from another thread, so use run_coroutine_threadsafe.
        """
        if error:
            print(f"Player error: {error}")

        fut = asyncio.run_coroutine_threadsafe(self.play_next_song(), self.bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Error in after_song callback: {e}")

    async def play_next_song(self):
        """Play the next song in the queue if available."""
        if self.is_looping and self.vc and self.vc.is_playing():
            # Do not advance if looping enabled
            return

        if len(self.music_queue) == 0:
            self.is_playing = False
            return

        song_info, voice_channel = self.music_queue.pop(0)
        self.is_playing = True

        # Connect or move the bot to the channel
        vc = await self.ensure_voice(voice_channel)
        if not vc:
            # Failed to connect, skip this song
            await self.bot.get_channel(voice_channel.guild.system_channel.id).send("Failed to connect to voice channel.")
            self.is_playing = False
            return

        # Extract info with yt_dlp in executor to avoid blocking
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: self.ytdl.extract_info(song_info["source"], download=False)
            )
        except Exception as e:
            print(f"Failed to download/stream info: {e}")
            self.is_playing = False
            return

        # URL to audio stream
        audio_url = data.get("url")
        if not audio_url:
            self.is_playing = False
            return

        source = discord.FFmpegPCMAudio(audio_url, executable=FFMPEG_PATH, **self.ffmpeg_options)
        vc.play(source, after=self.after_song)

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{self.bot.user} has connected.")

    @app_commands.command(name="plays", description="Play a song from URL or search term")
    @app_commands.describe(source="YouTube link or search keywords")
    async def plays(self, interaction: discord.Interaction, source: str):
        """Adds a song to queue and plays if not already playing."""
        await interaction.response.defer()  # defer so we can do async work
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.followup.send("You need to be connected to a voice channel.")
            return

        song = self.search_youtube(source)
        if not song:
            await interaction.followup.send("Could not find the song with that query.")
            return

        self.music_queue.append((song, voice_state.channel))

        if self.is_playing or self.is_paused:
            await interaction.followup.send(f"**Added to queue: {song['title']}** (Position {len(self.music_queue)})")
        else:
            await interaction.followup.send(f"**Playing: {song['title']}**")
            await self.play_next_song()

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        if not self.vc or not self.vc.is_playing():
            await interaction.response.send_message("Nothing is playing right now.")
            return

        self.vc.pause()
        self.is_paused = True
        self.is_playing = False
        await interaction.response.send_message("Music paused.")

    @app_commands.command(name="resume", description="Resume paused music")
    async def resume(self, interaction: discord.Interaction):
        if not self.vc or not self.is_paused:
            await interaction.response.send_message("Music is not paused.")
            return

        self.vc.resume()
        self.is_paused = False
        self.is_playing = True
        await interaction.response.send_message("Music resumed.")

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        if self.vc and self.vc.is_playing():
            self.vc.stop()  # after callback triggers next song
            await interaction.response.send_message("Skipped current song.")
        else:
            await interaction.response.send_message("Nothing is playing to skip.")

    @app_commands.command(name="loop", description="Toggle looping for the current queue")
    async def toggle_loop(self, interaction: discord.Interaction):
        self.is_looping = not self.is_looping
        status = "enabled" if self.is_looping else "disabled"
        await interaction.response.send_message(f"Looping {status}.")

    @app_commands.command(name="queue", description="Show current song queue")
    async def show_queue(self, interaction: discord.Interaction):
        if not self.music_queue:
            await interaction.response.send_message("The queue is empty.")
            return

        queue_list = "\n".join(
            f"#{idx+1}: {song['title']}" for idx, (song, _) in enumerate(self.music_queue)
        )
        await interaction.response.send_message(f"Current queue:\n{queue_list}")

    @app_commands.command(name="clear", description="Clear the music queue and stop playback")
    async def clear_queue(self, interaction: discord.Interaction):
        self.music_queue.clear()
        if self.vc and self.vc.is_playing():
            self.vc.stop()
        self.is_playing = False
        self.is_paused = False
        await interaction.response.send_message("Queue cleared and playback stopped.")

    @app_commands.command(name="stop", description="Stop playback and disconnect bot")
    async def stop(self, interaction: discord.Interaction):
        self.music_queue.clear()
        if self.vc:
            await self.vc.disconnect()
            self.vc = None
        self.is_playing = False
        self.is_paused = False
        await interaction.response.send_message("Stopped playback and disconnected.")

    @app_commands.command(name="remove", description="Remove the last song from queue")
    async def remove_last(self, interaction: discord.Interaction):
        if not self.music_queue:
            await interaction.response.send_message("Queue is already empty.")
            return

        removed_song, _ = self.music_queue.pop()
        await interaction.response.send_message(f"Removed last song: {removed_song['title']}")

    @app_commands.command(name="join", description="Make bot join your voice channel")
    async def join(self, interaction: discord.Interaction):
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.response.send_message("You must be connected to a voice channel for me to join.")
            return

        try:
            await voice_state.channel.connect()
            await interaction.response.send_message(f"Joined {voice_state.channel.name}")
        except Exception as e:
            await interaction.response.send_message(f"Failed to join voice channel: {e}")