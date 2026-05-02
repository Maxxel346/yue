from discord.ext import commands
import os, asyncio
import discord

#import all of the cogs
from help_cog import help_cog
from music_cog_copy import music_cog
from config import get_api_key

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='/', intents=intents)

#remove the default help command so that we can write out own
bot.remove_command('help')

async def main():
    async with bot:
        await bot.add_cog(help_cog(bot))
        await bot.add_cog(music_cog(bot))
        await bot.start(get_api_key())

asyncio.run(main())

# import maniac

# if __name__ == "__main__":
#     maniac.run_bot()