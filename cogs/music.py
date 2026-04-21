"""
Music cog: handles voice connection, queue, and playback for YouTube + Spotify links.

Spotify note: Spotify's API does not allow direct audio streaming. We pull track
metadata (title + artist) from the Spotify URL and search YouTube for a match,
which is the standard pattern every music bot uses.
"""
import os
import re
import asyncio
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# Spotify is optional; bot still works for YouTube without it.
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except ImportError:
    SPOTIFY_AVAILABLE = False


YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com/album/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_RE = re.compile(r"[?&]list=([a-zA-Z0-9_-]+)")


def _format_duration(seconds):
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class Track:
    """Represents one queued item. `query` is either a URL or a search string."""
    def __init__(self, query, requester, title=None, duration=None, thumbnail=None):
        self.query = query
        self.requester = requester
        self.title = title or query
        self.duration = duration
        self.thumbnail = thumbnail

    async def resolve_stream(self):
        """Fetch the actual audio stream URL via yt-dlp. Runs in executor to avoid blocking."""
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            data = await loop.run_in_executor(
                None, lambda: ydl.extract_info(self.query, download=False)
            )
        if "entries" in data:
            data = data["entries"][0]
        self.title = data.get("title", self.title)
        self.duration = data.get("duration", self.duration)
        self.thumbnail = data.get("thumbnail", self.thumbnail)
        return data["url"]


class GuildPlayer:
    """One per-guild queue + player loop."""
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.voice: discord.VoiceClient | None = None
        self.text_channel: discord.TextChannel | None = None
        self.next_event = asyncio.Event()
        self.volume = 0.5
        self.loop_one = False
        self._task = bot.loop.create_task(self._player_loop())

    async def _player_loop(self):
        while True:
            self.next_event.clear()

            # Idle disconnect after 5 minutes with nothing queued.
            if not self.queue:
                try:
                    await asyncio.wait_for(self._wait_for_track(), timeout=300)
                except asyncio.TimeoutError:
                    if self.voice and self.voice.is_connected():
                        await self.voice.disconnect()
                    self.voice = None
                    self.current = None
                    return

            track = self.queue.popleft()
            self.current = track

            try:
                stream_url = await track.resolve_stream()
            except Exception as e:
                if self.text_channel:
                    await self.text_channel.send(f"Couldn't load **{track.title}**: `{e}`")
                continue

            if not self.voice or not self.voice.is_connected():
                self.current = None
                return

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
                volume=self.volume,
            )
            self.voice.play(
                source,
                after=lambda _: self.bot.loop.call_soon_threadsafe(self.next_event.set),
            )

            if self.text_channel:
                embed = discord.Embed(
                    title="Now playing",
                    description=f"[{track.title}]({track.query if track.query.startswith('http') else ''})",
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Duration", value=_format_duration(track.duration))
                embed.add_field(name="Requested by", value=track.requester.mention)
                if track.thumbnail:
                    embed.set_thumbnail(url=track.thumbnail)
                await self.text_channel.send(embed=embed)

            await self.next_event.wait()

            if self.loop_one and self.current:
                self.queue.appendleft(self.current)
            self.current = None

    async def _wait_for_track(self):
        while not self.queue:
            await asyncio.sleep(1)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.spotify = None
        if SPOTIFY_AVAILABLE:
            cid = os.getenv("SPOTIFY_CLIENT_ID")
            secret = os.getenv("SPOTIFY_CLIENT_SECRET")
            if cid and secret:
                self.spotify = spotipy.Spotify(
                    auth_manager=SpotifyClientCredentials(client_id=cid, client_secret=secret)
                )

    def get_player(self, guild_id) -> GuildPlayer:
        player = self.players.get(guild_id)
        if not player or player._task.done():
            player = GuildPlayer(self.bot, guild_id)
            self.players[guild_id] = player
        return player

    async def _ensure_voice(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You need to be in a voice channel first.")
            return None
        channel = interaction.user.voice.channel
        player = self.get_player(interaction.guild.id)
        player.text_channel = interaction.channel

        if player.voice and player.voice.is_connected():
            if player.voice.channel != channel:
                await player.voice.move_to(channel)
        else:
            player.voice = await channel.connect()
        return player

    async def _resolve_input(self, query: str, requester) -> list[Track]:
        """Turn user input into one or more Track objects."""
        # Spotify
        if "open.spotify.com" in query:
            if not self.spotify:
                raise RuntimeError(
                    "Spotify links require SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env."
                )
            return await asyncio.get_event_loop().run_in_executor(
                None, self._resolve_spotify, query, requester
            )

        # YouTube playlist
        if YOUTUBE_PLAYLIST_RE.search(query):
            opts = {**YDL_OPTIONS, "noplaylist": False, "extract_flat": True}
            loop = asyncio.get_event_loop()
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(query, download=False)
                )
            tracks = []
            for entry in data.get("entries", []):
                if not entry:
                    continue
                url = entry.get("url") or entry.get("webpage_url")
                if entry.get("ie_key") == "Youtube" and url and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={url}"
                tracks.append(Track(
                    query=url,
                    requester=requester,
                    title=entry.get("title", "Unknown"),
                    duration=entry.get("duration"),
                ))
            return tracks

        # Single URL or search
        return [Track(query=query, requester=requester)]

    def _resolve_spotify(self, url, requester) -> list[Track]:
        tracks = []
        if m := SPOTIFY_TRACK_RE.search(url):
            t = self.spotify.track(m.group(1))
            tracks.append(self._spotify_track_to_track(t, requester))
        elif m := SPOTIFY_PLAYLIST_RE.search(url):
            results = self.spotify.playlist_items(m.group(1))
            for item in results["items"]:
                if item.get("track"):
                    tracks.append(self._spotify_track_to_track(item["track"], requester))
        elif m := SPOTIFY_ALBUM_RE.search(url):
            results = self.spotify.album_tracks(m.group(1))
            for item in results["items"]:
                tracks.append(self._spotify_track_to_track(item, requester))
        return tracks

    def _spotify_track_to_track(self, sp_track, requester) -> Track:
        artist = sp_track["artists"][0]["name"]
        name = sp_track["name"]
        # yt-dlp will treat a non-URL string as a YouTube search.
        return Track(
            query=f"ytsearch:{artist} {name} audio",
            requester=requester,
            title=f"{artist} - {name}",
            duration=(sp_track.get("duration_ms") or 0) // 1000,
        )

    # ---------- Slash commands ----------

    @app_commands.command(name="play", description="Play a YouTube or Spotify link, or a search query.")
    @app_commands.describe(query="URL or search terms")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        player = await self._ensure_voice(interaction)
        if not player:
            return
        try:
            tracks = await self._resolve_input(query, interaction.user)
        except Exception as e:
            await interaction.followup.send(f"Failed to load: `{e}`")
            return
        if not tracks:
            await interaction.followup.send("Nothing found for that query.")
            return
        for t in tracks:
            player.queue.append(t)
        if len(tracks) == 1:
            await interaction.followup.send(f"Queued **{tracks[0].title}**.")
        else:
            await interaction.followup.send(f"Queued **{len(tracks)}** tracks.")

    @app_commands.command(name="skip", description="Skip the current track.")
    async def skip(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if not player or not player.voice or not player.voice.is_playing():
            await interaction.response.send_message("Nothing is playing.")
            return
        player.voice.stop()
        await interaction.response.send_message("Skipped.")

    @app_commands.command(name="pause", description="Pause playback.")
    async def pause(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if player and player.voice and player.voice.is_playing():
            player.voice.pause()
            await interaction.response.send_message("Paused.")
        else:
            await interaction.response.send_message("Nothing playing.")

    @app_commands.command(name="resume", description="Resume playback.")
    async def resume(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if player and player.voice and player.voice.is_paused():
            player.voice.resume()
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message("Not paused.")

    @app_commands.command(name="stop", description="Stop and clear the queue, then leave.")
    async def stop(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if not player:
            await interaction.response.send_message("Not connected.")
            return
        player.queue.clear()
        if player.voice:
            player.voice.stop()
            await player.voice.disconnect()
            player.voice = None
        await interaction.response.send_message("Stopped and disconnected.")

    @app_commands.command(name="queue", description="Show the upcoming tracks.")
    async def queue_cmd(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if not player or (not player.queue and not player.current):
            await interaction.response.send_message("Queue is empty.")
            return
        lines = []
        if player.current:
            lines.append(f"**Now:** {player.current.title}")
        for i, t in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"`{i}.` {t.title}")
        if len(player.queue) > 10:
            lines.append(f"...and {len(player.queue) - 10} more")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Show the current track.")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if not player or not player.current:
            await interaction.response.send_message("Nothing playing.")
            return
        t = player.current
        embed = discord.Embed(title="Now playing", description=t.title, color=discord.Color.green())
        embed.add_field(name="Duration", value=_format_duration(t.duration))
        embed.add_field(name="Requested by", value=t.requester.mention)
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Set volume (0-200).")
    async def volume(self, interaction: discord.Interaction, level: int):
        player = self.players.get(interaction.guild.id)
        if not player or not player.voice:
            await interaction.response.send_message("Not connected.")
            return
        level = max(0, min(200, level))
        player.volume = level / 100
        if player.voice.source:
            player.voice.source.volume = player.volume
        await interaction.response.send_message(f"Volume set to {level}%.")

    @app_commands.command(name="loop", description="Toggle looping the current track.")
    async def loop(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if not player:
            await interaction.response.send_message("Not connected.")
            return
        player.loop_one = not player.loop_one
        await interaction.response.send_message(
            f"Loop is now **{'on' if player.loop_one else 'off'}**."
        )


async def setup(bot):
    await bot.add_cog(Music(bot))
