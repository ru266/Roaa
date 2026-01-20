import asyncio
import os
import re
import json
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum
from pathlib import Path

import aiofiles
from telethon import TelegramClient, events, Button
from telethon.tl import functions, types
from telethon.tl.functions.stories import GetStoriesByIDRequest
from telethon.tl.types import StoryItem, User, Channel
from telethon.errors import FloodWaitError
from dataclasses import dataclass, field
from collections import defaultdict
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiohttp
from PIL import Image
import io

# ==================== CONFIGURATION ====================
class Config:
    API_ID = os.getenv("API_ID", "YOUR_API_ID")
    API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
    
    # Database paths
    DATA_DIR = Path("data")
    USERS_DB = DATA_DIR / "users.json"
    SUBSCRIPTIONS_DB = DATA_DIR / "subscriptions.json"
    CODES_DB = DATA_DIR / "codes.json"
    LOGS_DB = DATA_DIR / "logs.json"
    SESSIONS_DB = DATA_DIR / "sessions.json"
    
    # Rate limiting
    FREE_DAILY_LIMIT = 5
    PREMIUM_DAILY_LIMIT = 50
    FREE_DOWNLOAD_DELAY = 5
    PREMIUM_DOWNLOAD_DELAY = 2
    ULTRA_DOWNLOAD_DELAY = 1
    
    # Concurrent downloads
    FREE_CONCURRENT = 1
    PREMIUM_CONCURRENT = 3
    ULTRA_CONCURRENT = 10
    
    # Developer settings
    DEVELOPER_IDS = [123456789]  # Add your Telegram ID
    LOG_CHANNEL = -1001234567890  # Your private log channel
    
    # Cache settings
    CACHE_DURATION = timedelta(hours=24)
    
    # Image for welcome message
    WELCOME_IMAGE = "https://i.imgur.com/a2THbEa_d.webp?maxwidth=760&fidelity=grand"

# ==================== ENUMS & DATA CLASSES ====================
class SubscriptionTier(Enum):
    FREE = "free"
    PREMIUM = "premium"
    ULTRA = "ultra"

class DurationUnit(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"

@dataclass
class UserData:
    user_id: int
    username: str
    subscription_tier: SubscriptionTier = SubscriptionTier.FREE
    subscription_ends: Optional[datetime] = None
    daily_downloads: int = 0
    total_downloads: int = 0
    last_reset: datetime = field(default_factory=datetime.now)
    followed_accounts: List[str] = field(default_factory=list)
    settings: Dict = field(default_factory=lambda: {"silent_mode": False, "quality": "best"})

@dataclass
class SubscriptionCode:
    code: str
    tier: SubscriptionTier
    duration: int  # in days
    max_uses: int
    used_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None

@dataclass
class DownloadTask:
    user_id: int
    story_url: str
    task_id: str
    status: str = "pending"
    progress: float = 0.0
    started_at: datetime = field(default_factory=datetime.now)

# ==================== DATABASE MANAGER ====================
class DatabaseManager:
    def __init__(self):
        self.data_dir = Config.DATA_DIR
        self.data_dir.mkdir(exist_ok=True)
        
    async def load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        async with aiofiles.open(path, 'r') as f:
            return json.loads(await f.read())
    
    async def save_json(self, path: Path, data: dict):
        async with aiofiles.open(path, 'w') as f:
            await f.write(json.dumps(data, default=str, indent=2))
    
    async def get_user(self, user_id: int) -> Optional[UserData]:
        data = await self.load_json(Config.USERS_DB)
        user_data = data.get(str(user_id))
        if user_data:
            user_data['subscription_tier'] = SubscriptionTier(user_data['subscription_tier'])
            if user_data.get('subscription_ends'):
                user_data['subscription_ends'] = datetime.fromisoformat(user_data['subscription_ends'])
            user_data['last_reset'] = datetime.fromisoformat(user_data['last_reset'])
            return UserData(**user_data)
        return None
    
    async def save_user(self, user_data: UserData):
        data = await self.load_json(Config.USERS_DB)
        user_dict = {
            'user_id': user_data.user_id,
            'username': user_data.username,
            'subscription_tier': user_data.subscription_tier.value,
            'subscription_ends': user_data.subscription_ends.isoformat() if user_data.subscription_ends else None,
            'daily_downloads': user_data.daily_downloads,
            'total_downloads': user_data.total_downloads,
            'last_reset': user_data.last_reset.isoformat(),
            'followed_accounts': user_data.followed_accounts,
            'settings': user_data.settings
        }
        data[str(user_data.user_id)] = user_dict
        await self.save_json(Config.USERS_DB, data)
    
    async def get_code(self, code: str) -> Optional[SubscriptionCode]:
        data = await self.load_json(Config.CODES_DB)
        code_data = data.get(code)
        if code_data:
            code_data['tier'] = SubscriptionTier(code_data['tier'])
            code_data['created_at'] = datetime.fromisoformat(code_data['created_at'])
            if code_data.get('expires_at'):
                code_data['expires_at'] = datetime.fromisoformat(code_data['expires_at'])
            return SubscriptionCode(**code_data)
        return None
    
    async def save_code(self, code: SubscriptionCode):
        data = await self.load_json(Config.CODES_DB)
        code_dict = {
            'code': code.code,
            'tier': code.tier.value,
            'duration': code.duration,
            'max_uses': code.max_uses,
            'used_count': code.used_count,
            'created_at': code.created_at.isoformat(),
            'expires_at': code.expires_at.isoformat() if code.expires_at else None
        }
        data[code.code] = code_dict
        await self.save_json(Config.CODES_DB, data)

# ==================== SUBSCRIPTION MANAGER ====================
class SubscriptionManager:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.scheduler = AsyncIOScheduler()
    
    async def check_subscription(self, user_id: int) -> Tuple[SubscriptionTier, Optional[datetime]]:
        user_data = await self.db.get_user(user_id)
        if not user_data:
            return SubscriptionTier.FREE, None
        
        # Check if subscription expired
        if user_data.subscription_ends and datetime.now() > user_data.subscription_ends:
            user_data.subscription_tier = SubscriptionTier.FREE
            user_data.subscription_ends = None
            await self.db.save_user(user_data)
        
        # Reset daily downloads
        if (datetime.now() - user_data.last_reset).days >= 1:
            user_data.daily_downloads = 0
            user_data.last_reset = datetime.now()
            await self.db.save_user(user_data)
        
        return user_data.subscription_tier, user_data.subscription_ends
    
    async def activate_code(self, user_id: int, code: str) -> Tuple[bool, str]:
        code_data = await self.db.get_code(code)
        if not code_data:
            return False, "❌ Invalid code"
        
        if code_data.expires_at and datetime.now() > code_data.expires_at:
            return False, "❌ Code has expired"
        
        if code_data.used_count >= code_data.max_uses:
            return False, "❌ Code usage limit reached"
        
        user_data = await self.db.get_user(user_id)
        if not user_data:
            return False, "❌ User not found"
        
        # Update user subscription
        user_data.subscription_tier = code_data.tier
        user_data.subscription_ends = datetime.now() + timedelta(days=code_data.duration)
        
        # Update code usage
        code_data.used_count += 1
        await self.db.save_code(code_data)
        await self.db.save_user(user_data)
        
        return True, f"✅ {code_data.tier.value.capitalize()} subscription activated for {code_data.duration} days!"
    
    async def create_code(self, tier: SubscriptionTier, duration_days: int, max_uses: int = 1, expires_in_days: Optional[int] = None) -> str:
        code = hashlib.sha256(f"{tier}{duration_days}{datetime.now()}".encode()).hexdigest()[:12].upper()
        
        expires_at = None
        if expires_in_days:
            expires_at = datetime.now() + timedelta(days=expires_in_days)
        
        code_data = SubscriptionCode(
            code=code,
            tier=tier,
            duration=duration_days,
            max_uses=max_uses,
            expires_at=expires_at
        )
        
        await self.db.save_code(code_data)
        return code

# ==================== SESSION MANAGER ====================
class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, TelegramClient] = {}
        self.active_sessions: List[str] = []
        self.session_rotation_index = 0
    
    async def add_session(self, session_string: str, name: str) -> bool:
        try:
            client = TelegramClient(StringSession(session_string), Config.API_ID, Config.API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                self.sessions[name] = client
                self.active_sessions.append(name)
                return True
            return False
        except Exception as e:
            logging.error(f"Failed to add session {name}: {e}")
            return False
    
    async def get_next_client(self) -> Optional[TelegramClient]:
        if not self.active_sessions:
            return None
        
        self.session_rotation_index = (self.session_rotation_index + 1) % len(self.active_sessions)
        session_name = self.active_sessions[self.session_rotation_index]
        return self.sessions.get(session_name)
    
    async def remove_session(self, name: str) -> bool:
        if name in self.sessions:
            await self.sessions[name].disconnect()
            del self.sessions[name]
            if name in self.active_sessions:
                self.active_sessions.remove(name)
            return True
        return False

# ==================== STORY DOWNLOADER ====================
class StoryDownloader:
    def __init__(self, session_manager: SessionManager, db: DatabaseManager):
        self.session_manager = session_manager
        self.db = db
        self.cache = {}
        self.download_tasks: Dict[str, DownloadTask] = {}
        
    def parse_story_url(self, url: str) -> Tuple[Optional[str], Optional[int]]:
        patterns = [
            r"https?://t\.me/([a-zA-Z0-9_]+)/s/(\d+)",
            r"https?://t\.me/([a-zA-Z0-9_]+)/(\d+)"
        ]
        
        for pattern in patterns:
            match = re.match(pattern, url)
            if match:
                return match.group(1), int(match.group(2))
        return None, None
    
    async def fetch_stories(self, username: str) -> List[Tuple[int, StoryItem]]:
        client = await self.session_manager.get_next_client()
        if not client:
            raise Exception("No active sessions available")
        
        try:
            entity = await client.get_entity(username)
            stories = []
            
            # Get user's stories
            async for story in client.iter_stories(entity):
                stories.append((story.id, story))
            
            return stories
        except Exception as e:
            logging.error(f"Error fetching stories from {username}: {e}")
            return []
    
    async def download_story(self, user_id: int, username: str, story_id: int, quality: str = "best") -> Optional[Path]:
        user_data = await self.db.get_user(user_id)
        if not user_data:
            return None
        
        # Check download limits
        tier, _ = await SubscriptionManager(self.db).check_subscription(user_id)
        
        limits = {
            SubscriptionTier.FREE: Config.FREE_DAILY_LIMIT,
            SubscriptionTier.PREMIUM: Config.PREMIUM_DAILY_LIMIT,
            SubscriptionTier.ULTRA: float('inf')
        }
        
        if user_data.daily_downloads >= limits[tier]:
            raise Exception("Daily download limit reached")
        
        # Perform download
        client = await self.session_manager.get_next_client()
        if not client:
            raise Exception("No active sessions available")
        
        try:
            entity = await client.get_entity(username)
            result = await client(GetStoriesByIDRequest(
                peer=entity,
                id=[story_id]
            ))
            
            if not result.stories:
                raise Exception("Story not found")
            
            story = result.stories[0]
            
            # Determine download path
            download_dir = Path(f"downloads/{user_id}")
            download_dir.mkdir(parents=True, exist_ok=True)
            
            filename = f"{username}_{story_id}_{int(datetime.now().timestamp())}"
            
            if hasattr(story.media, 'photo'):
                filename += ".jpg"
                file_path = download_dir / filename
                await client.download_media(story.media.photo, file=file_path)
            elif hasattr(story.media, 'document'):
                # Determine file extension
                doc = story.media.document
                mime_type = doc.mime_type or "bin"
                ext = mime_type.split('/')[-1]
                if ext == 'octet-stream':
                    ext = 'mp4' if any(attr in doc.attributes for attr in ['Video', 'Audio']) else 'bin'
                filename += f".{ext}"
                file_path = download_dir / filename
                await client.download_media(story.media.document, file=file_path)
            else:
                raise Exception("Unsupported media type")
            
            # Update user stats
            user_data.daily_downloads += 1
            user_data.total_downloads += 1
            await self.db.save_user(user_data)
            
            return file_path
            
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            return await self.download_story(user_id, username, story_id, quality)
        except Exception as e:
            logging.error(f"Error downloading story: {e}")
            return None

# ==================== BOT UI MANAGER ====================
class UIManager:
    @staticmethod
    def create_progress_bar(progress: float, length: int = 20) -> str:
        filled = int(length * progress)
        bar = "█" * filled + "░" * (length - filled)
        return f"[{bar}] {progress*100:.1f}%"
    
    @staticmethod
    def get_subscription_icon(tier: SubscriptionTier) -> str:
        icons = {
            SubscriptionTier.FREE: "🆓",
            SubscriptionTier.PREMIUM: "⭐",
            SubscriptionTier.ULTRA: "👑"
        }
        return icons.get(tier, "❓")
    
    @staticmethod
    async def create_welcome_message(user_id: int, username: str) -> str:
        db = DatabaseManager()
        sub_manager = SubscriptionManager(db)
        
        tier, ends = await sub_manager.check_subscription(user_id)
        icon = UIManager.get_subscription_icon(tier)
        
        message = f"""
🎬 **Welcome to StoryDownloader Pro!** 🚀

👤 **User:** @{username}
{icon} **Plan:** {tier.value.upper()}

📥 **Daily Downloads:** {await sub_manager.get_remaining_downloads(user_id)}
⚡ **Speed:** {await sub_manager.get_download_speed(tier)}
🔢 **Concurrent:** {await sub_manager.get_concurrent_limit(tier)}

✨ **Features:**
{await sub_manager.get_features_list(tier)}

📌 **How to use:**
1. Send a username (e.g., `@username`)
2. Send a story link (e.g., `t.me/username/s/123`)
3. Use buttons to preview and download

💎 **Upgrade your plan for more features!**
        """
        return message

# ==================== MAIN BOT CLASS ====================
class StoryBot:
    def __init__(self):
        self.bot = TelegramClient("bot", Config.API_ID, Config.API_HASH)
        self.db = DatabaseManager()
        self.sub_manager = SubscriptionManager(self.db)
        self.session_manager = SessionManager()
        self.downloader = StoryDownloader(self.session_manager, self.db)
        self.ui = UIManager()
        self.scheduler = AsyncIOScheduler()
        
        # Active downloads tracking
        self.active_downloads: Dict[int, List[str]] = defaultdict(list)
        
        # Register event handlers
        self.register_handlers()
    
    def register_handlers(self):
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await self.handle_start(event)
        
        @self.bot.on(events.NewMessage(pattern='/help'))
        async def help_handler(event):
            await self.handle_help(event)
        
        @self.bot.on(events.NewMessage(pattern='/panel'))
        async def panel_handler(event):
            await self.handle_panel(event)
        
        @self.bot.on(events.NewMessage(pattern='/subscribe'))
        async def subscribe_handler(event):
            await self.handle_subscribe(event)
        
        @self.bot.on(events.NewMessage(pattern='/stats'))
        async def stats_handler(event):
            await self.handle_stats(event)
        
        @self.bot.on(events.NewMessage(pattern='/myplan'))
        async def myplan_handler(event):
            await self.handle_myplan(event)
        
        @self.bot.on(events.NewMessage)
        async def message_handler(event):
            await self.handle_message(event)
        
        @self.bot.on(events.CallbackQuery)
        async def callback_handler(event):
            await self.handle_callback(event)
    
    async def handle_start(self, event):
        """Handle /start command with welcome message"""
        user = await event.get_sender()
        
        # Create or update user in database
        user_data = await self.db.get_user(user.id)
        if not user_data:
            user_data = UserData(user_id=user.id, username=user.username or str(user.id))
            await self.db.save_user(user_data)
        
        # Send welcome message with image
        welcome_text = await self.ui.create_welcome_message(user.id, user.username or str(user.id))
        
        buttons = [
            [Button.inline("📥 Download Stories", b"download_help"),
             Button.inline("⭐ Upgrade Plan", b"upgrade")],
            [Button.inline("⚙️ Settings", b"settings"),
             Button.inline("📊 Statistics", b"stats")],
            [Button.inline("🆘 Help", b"help"),
             Button.inline("👑 Premium", b"premium_info")]
        ]
        
        # Try to send with image
        try:
            await event.reply(
                file=Config.WELCOME_IMAGE,
                message=welcome_text,
                buttons=buttons,
                parse_mode='md'
            )
        except:
            # Fallback to text only
            await event.reply(
                welcome_text,
                buttons=buttons,
                parse_mode='md'
            )
    
    async def handle_message(self, event):
        """Handle regular messages (story links or usernames)"""
        if not event.is_private:
            return
        
        text = event.text.strip()
        
        # Check if it's a username or story URL
        if text.startswith('@'):
            username = text[1:]
            await self.show_story_preview(event, username)
        elif 't.me/' in text:
            username, story_id = self.downloader.parse_story_url(text)
            if username and story_id:
                await self.download_single_story(event, username, story_id)
    
    async def show_story_preview(self, event, username: str):
        """Show preview of all available stories for a username"""
        try:
            msg = await event.reply(f"🔍 Fetching stories from @{username}...")
            
            stories = await self.downloader.fetch_stories(username)
            
            if not stories:
                await msg.edit("❌ No stories found or account is private.")
                return
            
            # Create preview message
            preview_text = f"📱 **Stories from @{username}**\n\n"
            
            buttons = []
            for idx, (story_id, story) in enumerate(stories[:10], 1):  # Limit to 10
                date = story.date.strftime("%H:%M")
                preview_text += f"{idx}. Story #{story_id} 📅 {date}\n"
                
                row = [
                    Button.inline(f"👁️ Preview {idx}", f"preview:{username}:{story_id}"),
                    Button.inline(f"⬇️ Download {idx}", f"dl_single:{username}:{story_id}")
                ]
                buttons.append(row)
            
            # Add batch download option for premium/ultra
            user_data = await self.db.get_user(event.sender_id)
            if user_data and user_data.subscription_tier != SubscriptionTier.FREE:
                buttons.append([Button.inline("📦 Download All", f"dl_all:{username}")])
            
            await msg.edit(preview_text, buttons=buttons)
            
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")
    
    async def download_single_story(self, event, username: str, story_id: int):
        """Download a single story"""
        user_id = event.sender_id
        
        # Check subscription status
        tier, ends = await self.sub_manager.check_subscription(user_id)
        
        # Check concurrent downloads
        if len(self.active_downloads.get(user_id, [])) >= {
            SubscriptionTier.FREE: Config.FREE_CONCURRENT,
            SubscriptionTier.PREMIUM: Config.PREMIUM_CONCURRENT,
            SubscriptionTier.ULTRA: Config.ULTRA_CONCURRENT
        }[tier]:
            await event.reply("⚠️ You have too many active downloads. Please wait.")
            return
        
        # Create task ID
        task_id = f"{user_id}_{username}_{story_id}_{int(datetime.now().timestamp())}"
        
        # Send progress message
        progress_msg = await event.reply(f"⏳ Preparing download...\n{UIManager.create_progress_bar(0)}")
        
        # Add to active downloads
        self.active_downloads[user_id].append(task_id)
        
        try:
            # Download the story
            file_path = await self.downloader.download_story(user_id, username, story_id)
            
            if file_path:
                # Send the file
                await self.bot.send_file(
                    event.chat_id,
                    file_path,
                    caption=f"✅ Downloaded from @{username}\n📅 Story #{story_id}",
                    progress_callback=lambda c, t: self.update_progress(progress_msg, c, t)
                )
                
                # Cleanup
                file_path.unlink()
                
            else:
                await progress_msg.edit("❌ Failed to download story.")
                
        except Exception as e:
            await progress_msg.edit(f"❌ Error: {str(e)}")
            
        finally:
            # Remove from active downloads
            if user_id in self.active_downloads:
                self.active_downloads[user_id].remove(task_id)
    
    async def update_progress(self, message, current, total):
        """Update download progress in message"""
        if total > 0:
            progress = current / total
            bar = UIManager.create_progress_bar(progress)
            
            # Update message every 10% or when complete
            if progress == 1 or int(progress * 100) % 10 == 0:
                try:
                    await message.edit(f"⬇️ Downloading...\n{bar}")
                except:
                    pass
    
    async def handle_callback(self, event):
        """Handle button callbacks"""
        data = event.data.decode()
        user_id = event.sender_id
        
        if data.startswith("preview:"):
            _, username, story_id = data.split(":")
            await self.preview_story(event, username, int(story_id))
        
        elif data.startswith("dl_single:"):
            _, username, story_id = data.split(":")
            await event.answer("Starting download...")
            await self.download_single_story(event, username, int(story_id))
        
        elif data == "upgrade":
            await self.show_upgrade_options(event)
        
        elif data == "stats":
            await self.show_user_stats(event)
        
        elif data == "settings":
            await self.show_settings(event)
        
        elif data == "help":
            await self.show_help(event)
        
        elif data == "premium_info":
            await self.show_premium_info(event)
        
        elif data == "download_help":
            await event.answer(
                "Send:\n• @username - to see all stories\n• t.me/username/s/123 - to download specific story",
                alert=True
            )
    
    async def preview_story(self, event, username: str, story_id: int):
        """Preview a story (send as photo/video)"""
        try:
            await event.answer("Loading preview...")
            
            # Fetch the story
            stories = await self.downloader.fetch_stories(username)
            target_story = None
            
            for sid, story in stories:
                if sid == story_id:
                    target_story = story
                    break
            
            if not target_story:
                await event.answer("Story not found", alert=True)
                return
            
            # Send preview
            if hasattr(target_story.media, 'photo'):
                await self.bot.send_file(
                    event.chat_id,
                    target_story.media.photo,
                    caption=f"👁️ Preview of @{username}'s story #{story_id}"
                )
            elif hasattr(target_story.media, 'document'):
                # For videos, send as document preview
                await event.reply(
                    f"🎥 **Video Preview**\n"
                    f"Username: @{username}\n"
                    f"Story ID: #{story_id}\n\n"
                    f"Use the download button to get full video."
                )
            
        except Exception as e:
            await event.answer(f"Error: {str(e)}", alert=True)
    
    async def show_upgrade_options(self, event):
        """Show subscription upgrade options"""
        text = """
💎 **Subscription Plans**

🆓 **FREE TIER**
• 5 downloads per day
• Basic speed
• Single downloads only
• No alerts

⭐ **PREMIUM TIER** - $9.99/month
• 50 downloads per day
• High speed
• Batch downloads (up to 5)
• Story alerts
• Priority support

👑 **ULTRA TIER** - $19.99/month
• Unlimited downloads
• Maximum speed
• One-click download all
• Unlimited concurrent
• Full alerts system
• Best quality
• No restrictions

To upgrade, contact @admin with your preferred plan.
        """
        
        buttons = [
            [Button.inline("🆓 Free Features", b"free_info"),
             Button.inline("⭐ Premium Features", b"premium_info")],
            [Button.inline("👑 Ultra Features", b"ultra_info"),
             Button.inline("🔑 Enter Code", b"enter_code")],
            [Button.inline("📞 Contact Admin", b"contact_admin")]
        ]
        
        await event.edit(text, buttons=buttons)
    
    async def show_user_stats(self, event):
        """Show user statistics"""
        user_data = await self.db.get_user(event.sender_id)
        
        if not user_data:
            await event.answer("User not found", alert=True)
            return
        
        tier, ends = await self.sub_manager.check_subscription(event.sender_id)
        
        text = f"""
📊 **Your Statistics**

👤 User ID: `{user_data.user_id}`
{self.ui.get_subscription_icon(tier)} Plan: **{tier.value.upper()}**
📅 Plan ends: {ends.strftime('%Y-%m-%d') if ends else 'Never'}

📥 **Downloads:**
• Today: {user_data.daily_downloads}/{{
    SubscriptionTier.FREE: Config.FREE_DAILY_LIMIT,
    SubscriptionTier.PREMIUM: Config.PREMIUM_DAILY_LIMIT,
    SubscriptionTier.ULTRA: '∞'
}}[tier]
• Total: {user_data.total_downloads}

⚡ **Speed:** {{
    SubscriptionTier.FREE: 'Normal',
    SubscriptionTier.PREMIUM: 'Fast',
    SubscriptionTier.ULTRA: 'Ultra Fast'
}}[tier]
🔢 **Concurrent:** {{
    SubscriptionTier.FREE: Config.FREE_CONCURRENT,
    SubscriptionTier.PREMIUM: Config.PREMIUM_CONCURRENT,
    SubscriptionTier.ULTRA: Config.ULTRA_CONCURRENT
}}[tier]

📈 **Followed Accounts:** {len(user_data.followed_accounts)}
        """
        
        await event.edit(text, parse_mode='md')
    
    # ==================== ADMIN/DEVELOPER FEATURES ====================
    
    async def handle_panel(self, event):
        """Developer control panel"""
        if event.sender_id not in Config.DEVELOPER_IDS:
            await event.reply("❌ Access denied.")
            return
        
        text = """
🔧 **Developer Control Panel**

Choose an option:
        """
        
        buttons = [
            [Button.inline("📊 System Stats", b"admin_stats"),
             Button.inline("👥 Users", b"admin_users")],
            [Button.inline("🔑 Generate Code", b"admin_gen_code"),
             Button.inline("📋 Active Subs", b"admin_subs")],
            [Button.inline("📡 Sessions", b"admin_sessions"),
             Button.inline("📨 Broadcast", b"admin_broadcast")],
            [Button.inline("📜 Logs", b"admin_logs"),
             Button.inline("⚙️ Settings", b"admin_settings")]
        ]
        
        await event.reply(text, buttons=buttons)
    
    async def handle_stats(self, event):
        """System statistics"""
        if event.sender_id not in Config.DEVELOPER_IDS:
            await event.reply("❌ Access denied.")
            return
        
        # Load all users
        users_data = await self.db.load_json(Config.USERS_DB)
        
        # Calculate statistics
        total_users = len(users_data)
        free_users = sum(1 for u in users_data.values() if u.get('subscription_tier') == 'free')
        premium_users = sum(1 for u in users_data.values() if u.get('subscription_tier') == 'premium')
        ultra_users = sum(1 for u in users_data.values() if u.get('subscription_tier') == 'ultra')
        
        total_downloads = sum(int(u.get('total_downloads', 0)) for u in users_data.values())
        
        text = f"""
📈 **System Statistics**

👥 **Users:**
• Total: {total_users}
• Free: {free_users}
• Premium: {premium_users}
• Ultra: {ultra_users}

📥 **Downloads:**
• Total: {total_downloads}
• Today: {sum(1 for u in users_data.values() if datetime.fromisoformat(u.get('last_reset')).date() == datetime.now().date())}

🖥️ **System:**
• Active sessions: {len(self.session_manager.active_sessions)}
• Active downloads: {sum(len(v) for v in self.active_downloads.values())}
• Cache size: {len(self.downloader.cache)} items

💾 **Database:**
• Users: {total_users} records
• Codes: {len(await self.db.load_json(Config.CODES_DB))}
• Logs: {len(await self.db.load_json(Config.LOGS_DB))}
        """
        
        await event.reply(text)
    
    async def handle_help(self, event):
        """Help command with distributed sections"""
        args = event.text.split()[1:] if len(event.text.split()) > 1 else []
        
        if not args:
            text = """
🆘 **Help Center**

Choose a help section:
/help bot - What the bot does
/help systems - Free/Premium/Ultra systems
/help subscription - How subscriptions work
/help alerts - How alerts work
/help download - How to download stories
/help commands - All available commands

Or use buttons below:
            """
            buttons = [
                [Button.inline("🤖 Bot Info", b"help_bot"),
                 Button.inline("⭐ Systems", b"help_systems")],
                [Button.inline("💎 Subscription", b"help_subscription"),
                 Button.inline("🔔 Alerts", b"help_alerts")],
                [Button.inline("📥 Download", b"help_download"),
                 Button.inline("📋 Commands", b"help_commands")]
            ]
            await event.reply(text, buttons=buttons)
        
        elif args[0] == "bot":
            await event.reply("""
🤖 **StoryDownloader Pro**

A powerful Telegram bot for downloading stories from public Telegram accounts.

**Features:**
• Download stories as photos/videos
• Preview before download
• Multiple quality options
• Subscription system
• Story alerts
• Developer dashboard
• And much more!

**Privacy:**
• We don't store your downloaded files
• We respect Telegram's ToS
• All downloads are client-side
            """)
        
        # ... (other help sections)

# ==================== SCHEDULED TASKS ====================
async def scheduled_tasks(bot: StoryBot):
    """Run scheduled tasks"""
    # Reset daily downloads
    users_data = await bot.db.load_json(Config.USERS_DB)
    for user_id, user_data in users_data.items():
        last_reset = datetime.fromisoformat(user_data.get('last_reset'))
        if (datetime.now() - last_reset).days >= 1:
            user_data['daily_downloads'] = 0
            user_data['last_reset'] = datetime.now().isoformat()
    await bot.db.save_json(Config.USERS_DB, users_data)
    
    # Check for expired subscriptions
    for user_id, user_data in users_data.items():
        if user_data.get('subscription_ends'):
            ends = datetime.fromisoformat(user_data['subscription_ends'])
            if datetime.now() > ends:
                user_data['subscription_tier'] = 'free'
                user_data['subscription_ends'] = None
                # Notify user
                try:
                    await bot.bot.send_message(
                        int(user_id),
                        "⚠️ Your subscription has expired. You've been downgraded to Free tier."
                    )
                except:
                    pass
    
    # Save updated users
    await bot.db.save_json(Config.USERS_DB, users_data)
    
    # Clean old cache
    bot.downloader.cache = {
        k: v for k, v in bot.downloader.cache.items()
        if datetime.now() - v['timestamp'] < Config.CACHE_DURATION
    }

# ==================== MAIN ENTRY POINT ====================
async def main():
    """Main entry point"""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Initialize bot
    bot = StoryBot()
    
    # Start scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_tasks,
        CronTrigger(hour=0, minute=0),  # Run daily at midnight
        args=[bot]
    )
    scheduler.start()
    
    # Start the bot
    await bot.bot.start(bot_token=Config.BOT_TOKEN)
    
    # Load existing sessions
    sessions_data = await bot.db.load_json(Config.SESSIONS_DB)
    for name, session_string in sessions_data.items():
        await bot.session_manager.add_session(session_string, name)
    
    print("🚀 StoryDownloader Pro is running!")
    print(f"🤖 Bot: @{(await bot.bot.get_me()).username}")
    print(f"🔧 Active sessions: {len(bot.session_manager.active_sessions)}")
    
    # Run until disconnected
    await bot.bot.run_until_disconnected()

if __name__ == "__main__":
    # Create necessary directories
    Path("downloads").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    Path("cache").mkdir(exist_ok=True)
    
    # Run the bot
    asyncio.run(main())