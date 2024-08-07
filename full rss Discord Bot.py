import os
import signal
import json
from datetime import datetime, timedelta, timezone

import asyncio
import aiohttp
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
import logging
import urllib.parse
from dateutil import parser
from dotenv import load_dotenv
import discord
from discord import Embed
from discord.ext import tasks, commands
from googletrans import Translator as GoogleTranslator
from deep_translator import DeeplTranslator
from tenacity import retry, stop_after_attempt, wait_fixed


DOMAIN_TO_SOURCE_MAPPING = {
    'cnn.com': {'zh': '有线电视新闻网', 'en': 'CNN'},
    'bbc.com': {'zh': '英国广播公司', 'en': 'BBC'},
    'wsj.com': {'zh': '华尔街日报', 'en': 'Wall Street Journal'},
    'foreignaffairs.com': {'zh': '外交事务', 'en': 'Foreign Affairs'},
    'ft.com': {'zh': '金融时报', 'en': 'Financial Times'},
    'reuters.com': {'zh': '路透社', 'en': 'Reuters'},
    'theatlantic.com': {'zh': '大西洋月刊', 'en': 'The Atlantic'},
    'economist.com': {'zh': '经济学人', 'en': 'The Economist'},
    'nytimes.com': {'zh': '纽约时报', 'en': 'The New York Times'},
    'bloomberg.com': {'zh': '彭博社', 'en': 'Bloomberg'},
    'theconversation.com': {'zh': '对话', 'en': 'The Conversation'},
    'nautil.us': {'zh': '鹦鹉螺', 'en': 'Nautil'},
    'longreads.com': {'zh': '长读', 'en': 'Longreads'},
    'nature.com': {'zh': '《自然》', 'en': 'Nature'},
    'science.org': {'zh': '《科学》', 'en': 'Science'},
    'eff.org': {'zh': '电子前哨基金会', 'en': 'EFF'},
    'ieee.org': {'zh': '电气和电子工程师协会', 'en': 'IEEE'},
    'brookings.edu': {'zh': '布鲁金斯学会', 'en': 'Brookings Institution'},
}

# 默认RSS源列表
DEFAULT_RSS_FEEDS = [
    'https://feeds.a.dj.com/rss/RSSOpinion.xml',
    'https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml',
    'https://www.foreignaffairs.com/rss.xml',
    'https://www.ft.com/opinion?format=rss',
    'https://www.reutersagency.com/feed/?best-types=reuters-news-first&post_type=best',
    'https://www.reutersagency.com/feed/?best-types=the-big-picture&post_type=best',
    'https://www.theatlantic.com/feed/all/',
    'https://www.economist.com/special-report/rss.xml',
    'https://www.economist.com/the-economist-explains/rss.xml',
    'https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml',
    'https://feeds.bloomberg.com/economics/news.rss',
    'https://feeds.bloomberg.com/bview/news.rss',
    'https://theconversation.com/global/home-page.atom',
    'https://nautil.us/feed/',
    'https://longreads.com/feed',
    'https://blog.cloudflare.com/rss',
    'https://www.eff.org/rss/updates.xml'
]

scheduler = AsyncIOScheduler()
running = True

def signal_handler(signal, frame):
    global running
    running = False
    logger.info("Received termination signal. Shutting down...")
    loop = asyncio.get_running_loop()
    loop.stop()

# 配置日志记录器
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# 读取环境变量
GOOGLE_TRANSLATE_API_KEY = os.getenv('GOOGLE_TRANSLATE_API_KEY')
DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

if not all([GOOGLE_TRANSLATE_API_KEY, DEEPL_API_KEY, DISCORD_TOKEN]):
    raise ValueError("缺少必要的环境变量")

# 配置常量
CONFIG_DIR = 'config'
ENTRY_LIFETIME = timedelta(days=7)
BOT_PREFIX = '!'
FEED_CHECK_INTERVAL = 3600  # 默认每小时检查一次RSS源

# 创建配置文件夹
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

# 初始化Discord Bot
intents = discord.Intents.default()
intents.message_content = True  # 确保机器人能够读取消息内容
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

def clean_old_entries(config):
    now = datetime.now(timezone.utc)
    config['processed_entries'] = [
        entry for entry in config['processed_entries'] if now - datetime.fromisoformat(entry['timestamp']) < ENTRY_LIFETIME
    ]
    config_handler.save_config()  # 清理后立即保存配置

# 配置管理
class ConfigHandler:
    # 初始化配置管理器
    def __init__(self):
        self.configs = {}
        self.dirty_flags = {} # 记录配置是否发生变化
        self.load_all_configs()

    # 从配置目录加载所有服务器的配置文件，每个配置文件对应一个服务器，以服务器ID命名。
    def load_all_configs(self):
        for filename in os.listdir(CONFIG_DIR):
            if filename.endswith('.json'):
                with open(os.path.join(CONFIG_DIR, filename), 'r') as f:
                    try:
                        server_id = int(filename[:-5])
                        config = json.load(f)
                        if self.validate_config(config):
                            self.configs[server_id] = config
                            self.dirty_flags[server_id] = False
                        else:
                            logger.error(f"Invalid configuration in {filename}")
                    except Exception as e:
                        logger.error(f"Failed to load configuration from {filename}: {e}")

    def validate_config(self, config):
        required_keys = {'rss_sources', 'channel_id', 'processed_entries', 'target_language', 'interval'}
        return all(key in config for key in required_keys)
    
    # 保存指定服务器的配置到对应的JSON文件。
    def save_config(self, guild_id):  
        if self.dirty_flags.get(guild_id, False):  # 只有在配置发生变化时才保存
            with open(os.path.join(CONFIG_DIR, f'{guild_id}.json'), 'w') as f:
                json.dump(self.configs[guild_id], f, indent=4)
            self.dirty_flags[guild_id] = False

    # 获取指定服务器的配置,如果配置不存在，创建一个默认配置
    def get_config(self, guild_id):
        if guild_id not in self.configs:
            self.configs[guild_id] = self.create_default_config()
            self.dirty_flags[guild_id] = True
        return self.configs[guild_id]

    def create_default_config(self):
        return {
            'rss_sources': DEFAULT_RSS_FEEDS.copy(),
            'channel_id': None,
            'processed_entries': [],
            'target_language': 'zh',
            'interval': FEED_CHECK_INTERVAL // 60,  # 以分钟为单位
            'etag': None,
            'last_modified': None
        }

    def add_rss_source(self, guild_id, rss_url):
        config = self.get_config(guild_id)
        if rss_url not in config['rss_sources']:
            config['rss_sources'].append(rss_url)
            self.dirty_flags[guild_id] = True
            self.save_config(guild_id)
            return True
        return False

    def remove_rss_source(self, guild_id, rss_url):
        config = self.get_config(guild_id)
        if rss_url in config['rss_sources']:
            config['rss_sources'].remove(rss_url)
            self.dirty_flags[guild_id] = True
            self.save_config(guild_id)
            return True
        return False

    def set_channel(self, guild_id, channel_id):
        config = self.get_config(guild_id)
        config['channel_id'] = channel_id
        self.dirty_flags[guild_id] = True
        self.save_config(guild_id)
        return True

    def get_channel(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('channel_id')

    def get_rss_sources(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('rss_sources', [])
    
    def set_target_language(self, guild_id, language):
        config = self.get_config(guild_id)
        config['target_language'] = language
        self.dirty_flags[guild_id] = True
        self.save_config(guild_id)
        return True

    def get_target_language(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('target_language', 'zh')  # 默认翻译到中文
    
    # 设置指定服务器的RSS处理间隔时间
    def set_interval(self, guild_id, interval):
        config = self.get_config(guild_id)
        config['interval'] = interval
        self.dirty_flags[guild_id] = True
        self.save_config(guild_id)
        return True

    def get_interval(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('interval', 60)  # 默认间隔时间为60分钟

config_handler = ConfigHandler()

# 解析并格式化RSS条目的发布时间，统一转换为UTC时间。
def parse_published_time(entry):
    if 'published_parsed' in entry and entry.published_parsed:
        published_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    elif 'published' in entry:
        try:
            published_time = parser.parse(entry.published).astimezone(timezone.utc)
        except (ValueError, TypeError):
            published_time = None
    else:
        published_time = None
    
    if published_time:
        return published_time.strftime('%Y-%m-%d %H:%M:%S %Z')
    else:
        return 'No date'


# 翻译类管理
class TranslatorHandler:
    def __init__(self):
        self.google_translator = GoogleTranslator()
        self.deepl_translator = DeeplTranslator(api_key=DEEPL_API_KEY)
        self.use_google = True

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    def translate(self, text, target_language):
        if self.use_google:
            try:
                # Google API 语言代码使用小写
                return self.google_translator.translate(text, dest=target_language.lower()).text
            except Exception as e:
                logger.error(f"Google 翻译失败: {e}, 切换到DeepL")
                self.use_google = False
                raise e

        if not self.use_google:
            try:
                # DeepL API 语言代码使用大写
                return self.deepl_translator.translate(text, target=target_language.upper())
            except Exception as e:
                logger.error(f"DeepL 翻译失败: {e}, 切换回Google")
                self.use_google = True
                raise e

translator_handler = TranslatorHandler()

# 清理HTML标签并提取图片
def clean_html_and_extract_images(raw_html):
    if not raw_html.strip().startswith('<'):
        return raw_html, []
    
    soup = BeautifulSoup(raw_html, 'html.parser')
    text = soup.get_text()
    images = [img['src'] for img in soup.find_all('img') if 'src' in img.attrs]
    return text, images

# 格式化Discord消息
def format_discord_message(article, translated_title, translated_summary, target_language):
    source = article.get('source', 'Unknown source')  # 获取RSS推送中的原始source名称
    link = article.get('link', 'No link')
    images = article.get('images', [])  # 确保images从article中获取
    published_time = article.get('published') or article.get('pubDate') or article.get('updated', 'No date')

    # 如果有发布时间，则解析它
    if published_time != 'No date':
        published_time = parse_published_time({'published': published_time})

    # 提取并映射域名，目标语言为中文显示中文名，否则显示英文名
    parsed_url = urllib.parse.urlparse(link)
    domain = parsed_url.netloc
    source_info = DOMAIN_TO_SOURCE_MAPPING.get(domain, {'zh': source, 'en': source})
    source = source_info['zh'] if target_language == 'zh' else source_info['en']


    # discord限制单个嵌入消息字段超过1024字符
    translated_summary = article['summary']
    if len(translated_summary) > 1024:
        translated_summary = translated_summary[:1021] + '...'

    embed = discord.Embed(description=f"[{translated_title}]({link})")
    embed.add_field(name="Details", value=f"```fix\n{translated_summary}\n\nSource: {source}\nTime: {published_time}\n```", inline=False)

    if images:
        embed.set_image(url=images[0])

    return embed


# 获取并翻译单个RSS源的条目
@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def fetch_and_translate(session, url, target_language, etag=None, last_modified=None):
    headers = {}
    if etag:
        headers['If-None-Match'] = etag
    if last_modified:
        headers['If-Modified-Since'] = last_modified
    try:
        async with session.get(url, headers=headers) as response: # 使用 aiohttp 发起异步HTTP GET请求
            if response.status == 304:
                logger.info(f"No changes for URL: {url}")
                return [], etag, last_modified
            
             # 检查响应状态码，如果不是成功状态码，则抛出异常
            response.raise_for_status()
            content = await response.text()
            feed = feedparser.parse(content)
            translated_entries = []
            for entry in feed.entries:
                translated_title = translator_handler.translate(entry.title, target_language)
                summary, images = clean_html_and_extract_images(entry.get('summary') or entry.get('description') or entry.get('content', [{}])[0].get('value', 'No summary'))
                translated_summary = translator_handler.translate(summary, target_language)
                entry_id = entry.get('id') or entry.get('guid') or entry.get('link') or f"{entry.title}-{entry.published}"
                translated_entries.append({
                    'id': entry_id,
                    'title': translated_title,
                    'summary': translated_summary,
                    'link': entry.link,
                    'images': images,
                    'source': entry.get('source', 'Unknown source'),
                    'published': entry.get('published') or entry.get('pubDate') or entry.get('updated', 'No date')
                })
            etag = response.headers.get('ETag')   # 获取响应头中的 etag 和 last-modified 值
            last_modified = response.headers.get('Last-Modified')
            return translated_entries, etag, last_modified   # 返回翻译后的条目列表以及新的etag和last_modified
    except Exception as e:
        logger.error(f"Failed to fetch and translate URL {url}: {e}")
        raise e

# 处理并发送RSS条目
@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def process_and_send(guild_id):
    config = config_handler.get_config(guild_id)
    channel_id = config_handler.get_channel(guild_id)
    if channel_id:
        channel = bot.get_channel(channel_id) # 获取频道对象
        if channel:
            clean_old_entries(config)
            target_language = config_handler.get_target_language(guild_id)
            rss_urls = config['rss_sources'] # 获取RSS源列表
            # 并行处理所有RSS源
            async with aiohttp.ClientSession() as session:
                tasks = [
                    fetch_and_translate(
                        session,
                        url,
                        target_language,
                        config.get('etag'),
                        config.get('last_modified')
                    )
                    for url in rss_urls
                ]
                results = await asyncio.gather(*tasks)

             # 将所有结果合并到一个列表中
            all_entries = []
            for entries, etag, last_modified in results:
                 if entries:
                    config['etag'] = etag
                    config['last_modified'] = last_modified
                 all_entries.extend(entries)

            for entry in all_entries:  # 比较每个条目的ID是否已经存在于processed_entries列表中：
                if entry['id'] not in [e['id'] for e in config['processed_entries']]:
                    embed = format_discord_message(entry, entry['title'], entry['summary'], target_language)
                    try:
                        await channel.send(embed=embed) # 发送消息到指定频道
                        logger.info(f"Sent message for entry {entry['link']}")
                    except Exception as e:
                        logger.error(f"Failed to send message for entry {entry['link']}: {e}")
                    config['processed_entries'].append({
                        'id': entry['id'],
                        'link': entry['link'],
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })
            config_handler.save_config(guild_id)

# Discord bot命令
@bot.command(name='add_rss')
async def add_rss(ctx, rss_url):
    if ctx.guild is None:
        await ctx.send('This command can only be used in a server.')
        return
    if config_handler.add_rss_source(ctx.guild.id, rss_url):
        await ctx.send(f'RSS feed {rss_url} added')
    else:
        await ctx.send('The RSS feed is invalid or already exists')

@bot.command(name='remove_rss')
async def remove_rss(ctx, rss_url):
    if ctx.guild is None:
        await ctx.send('This command can only be used in a server.')
        return
    if config_handler.remove_rss_source(ctx.guild.id, rss_url):
        await ctx.send(f'RSS feed {rss_url} Removed')
    else:
        await ctx.send('RSS feed not found.')

@bot.command(name='set_channel')
async def set_channel(ctx, channel: discord.TextChannel = None):
    if ctx.guild is None:
        await ctx.send('This command can only be used in a server.')
        return

    if channel is None:
        await ctx.send('You must specify a channel.')
        return

    if config_handler.set_channel(ctx.guild.id, channel.id):
        await ctx.send(f'Channel is set to {channel.mention}')
    else:
        await ctx.send('Failed to set channel.')

@bot.command(name='list_rss')
async def list_rss(ctx):
    if ctx.guild is None:
        await ctx.send('This command can only be used in a server.')
        return
    rss_sources = config_handler.get_rss_sources(ctx.guild.id)
    if rss_sources:
        await ctx.send('Current RSS feed list:\n' + '\n'.join(rss_sources))
    else:
        await ctx.send('No RSS feeds.')

@bot.command(name='set_language')
async def set_language(ctx, language):
    if ctx.guild is None:
        await ctx.send('This command can only be used in a server.')
        return
    valid_languages = [
    'bg', 'cs', 'da', 'de', 'el', 'en', 'es', 'et', 'fi', 'fr', 'hu', 'it', 'ja', 'lt', 'lv', 'nl', 
    'pl', 'pt', 'ro', 'ru', 'sk', 'sl', 'sv', 'zh'] # 可选的目标语言
    if language not in valid_languages:
        await ctx.send(f'Invalid language code. Possible language codes are : {", ".join(valid_languages)}')
    else:
        config_handler.set_target_language(ctx.guild.id, language)
        await ctx.send(f'Translation target language changed to {language}')

@bot.command(name='set_interval')  # 更改RSS处理间隔时间
async def set_interval(ctx, interval: int = None):
    if ctx.guild is None:
        await ctx.send('This command can only be used in a server.')
        return

    if interval is None:
        await ctx.send('You must specify an interval in minutes.')
        return

    if interval <= 0:
        await ctx.send('The interval must be greater than 0 minutes')
        return

    config_handler.set_interval(ctx.guild.id, interval)
    job_id = f'process_rss_{ctx.guild.id}'
    if scheduler.get_job(job_id):
        scheduler.reschedule_job(job_id, trigger='interval', minutes=interval)
    else:
        scheduler.add_job(process_and_send, 'interval', minutes=interval, args=[ctx.guild.id], id=job_id)

    await ctx.send(f'RSS processing interval has been changed to {interval} minutes')

def setup_scheduler():
    for guild in bot.guilds:
        interval = config_handler.get_interval(guild.id)
        job_id = f'process_rss_{guild.id}'
        if not scheduler.get_job(job_id):
            logger.info(f"Adding job for guild {guild.id} with interval {interval} minutes")
            scheduler.add_job(process_and_send, 'interval', minutes=interval, args=[guild.id], id=job_id)
        else:
            logger.info(f"Job for guild {guild.id} already exists")
    scheduler.start()
    logger.info("Scheduler started")

# 启动bot并开始处理RSS
async def run_bot():
    await bot.wait_until_ready()  # 等待机器人准备就绪
    setup_scheduler()  # 启动调度器
    await bot.start(DISCORD_TOKEN)

# 设置信号处理器
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def main():
    setup_scheduler()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        logger.info("Cleaning up tasks")
        pending = asyncio.all_tasks()
        for task in pending:
            task.cancel()
        logger.info("Shutting down...")
