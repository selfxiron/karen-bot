import os
import asyncio
import discord
from discord import app_commands
import aiosqlite 
from dotenv import load_dotenv
from groq import AsyncGroq

# 1. ENVIRONMENT & CONFIGURATION
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

# Initialize the Groq AI client as an asynchronous object.
# This ensures network requests to Groq do not freeze the Discord bot.
ai_client = AsyncGroq(api_key=GROQ_API_KEY)

# Define Discord intents.
# Intents are subscriptions to specific events. message_content allows reading normal text.
intents = discord.Intents.default()
# intents.message_content = True

# 2. THE DATABASE ENGINE (PERSISTENT STATE)
class AsyncChatMemory:
    """
    Manages long-term conversational memory using an asynchronous SQLite database.
    This ensures the bot remembers context even if the server reboots.
    """
    def __init__(self, db_path="chat.db", max_history=5):
        self.db_path = db_path
        self.max_history = max_history
        
        # The System Prompt is hardcoded here rather than saved in the database.
        # This saves storage space. It will be injected dynamically before every AI call.
        self.system_prompt = {
            "role": "system", 
            "content": "You are a helpful assistant. Keep your answers concise and under 2 paragraphs unless asked for details."
        }

    async def setup(self):
        """Creates the database and table if they do not exist on the hard drive."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER,
                    role TEXT,
                    content TEXT
                )
            ''')
            await db.commit()

    async def add_message(self, channel_id: int, role: str, content: str):
        """Saves a new message to the database and enforces the sliding window limit."""
        async with aiosqlite.connect(self.db_path) as db:
            # Insert the new message safely using parameterization (?) to prevent SQL injection
            await db.execute(
                'INSERT INTO messages (channel_id, role, content) VALUES (?, ?, ?)', 
                (channel_id, role, content)
            )
            
            # The Sliding Window logic: 
            # Delete older messages if the channel exceeds the max_history limit.
            await db.execute('''
                DELETE FROM messages
                WHERE channel_id = ? AND id NOT IN (
                    SELECT id FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?
                )
            ''', (channel_id, channel_id, self.max_history))
            
            await db.commit()

    async def get_history(self, channel_id: int):
        """Fetches the conversation history for a specific channel and injects the system prompt."""
        async with aiosqlite.connect(self.db_path) as db:
            # Retrieve messages ordered by ID (oldest to newest)
            async with db.execute('SELECT role, content FROM messages WHERE channel_id = ? ORDER BY id ASC', (channel_id,)) as cursor:
                rows = await cursor.fetchall()

        # Build the final history payload for the LLM
        history = [self.system_prompt]
        for row in rows:
            history.append({"role": row[0], "content": row[1]})

        return history

# Instantiate the memory manager globally
memory = AsyncChatMemory(max_history=10)

# 3. THE DISCORD CLIENT & QUEUE WORKER
class MyClient(discord.Client):
    """
    The core Discord client. This handles the connection to Discord's servers,
    manages slash commands, and runs the background consumer loop.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # The Async Queue: A buffer that holds user requests.
        # This protects the bot from being overwhelmed by simultaneous messages.
        self.message_queue = asyncio.Queue()
        
        # The Command Tree: Required to register modern Slash Commands (e.g., /ask)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        """Runs once when the bot boots up, before connecting to Discord."""
        # 1. Initialize the SQLite database
        await memory.setup()
        
        # 2. Start the infinite background loop (The Consumer)
        self.loop.create_task(self.background_worker())
        
        # 3. Sync our slash commands to Discord's servers
        await self.tree.sync()

    async def on_ready(self):
        """Triggers when the WebSocket connection to Discord is fully established."""
        print(f'Logged on securely as {self.user}!')

    async def background_worker(self):
        """
        The Consumer Loop. This runs forever in the background.
        It pulls one request from the queue at a time, queries the AI, and sends the reply.
        """
        await self.wait_until_ready()
        print("Background worker is running and waiting for slash commands...")

        while not self.is_closed():
            # 1. Dequeue: Wait here until a user puts a command into the queue
            interaction, prompt = await self.message_queue.get()
            
            try:
                channel_id = interaction.channel_id
                
                # 2. State Management: Save the user's prompt to SQLite and fetch context
                await memory.add_message(channel_id, "user", prompt)
                chat_history = await memory.get_history(channel_id)
                
                # 3. Compute: Send the history to Groq (Llama 3.1) asynchronously
                response = await ai_client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=chat_history,
                    max_tokens=500
                )
                
                # Extract the actual text from the API response
                bot_reply = response.choices[0].message.content
                
                # 4. State Management: Save the AI's reply to SQLite so it remembers it later
                await memory.add_message(channel_id, "assistant", bot_reply)
                
                # 5. Output Management: Chunking
                # Discord rejects messages > 2000 characters. We slice the reply into chunks.
                chunk_size = 1900 
                for i in range(0, len(bot_reply), chunk_size):
                    chunk = bot_reply[i:i+chunk_size]
                    
                    if i == 0:
                        # The very first chunk MUST use interaction.followup.send() 
                        # to resolve the native "Bot is thinking..." UI state.
                        await interaction.followup.send(chunk)
                    else:
                        # Any remaining chunks are sent as normal text messages
                        await interaction.channel.send(chunk)
                        
            except Exception as e:
                # Failsafe: Prevent the bot from silently hanging if the API crashes
                print(f"Error processing command: {e}")
                await interaction.followup.send("Sorry, my brain encountered an error.")
                
            finally:
                # 6. Housekeeping: Tell the queue this specific task is fully complete
                self.message_queue.task_done()

# Instantiate the client
client = MyClient(intents=intents)

# 4. SLASH COMMANDS (THE PRODUCER)
@client.tree.command(name="ask", description="Ask the AI a question")
@app_commands.describe(prompt="What do you want to ask the AI?")
async def ask_command(interaction: discord.Interaction, prompt: str):
    """
    The Producer. When a user types /ask, this function catches it.
    It instantly defers the interaction, then puts the request into the background queue.
    """
    # Instantly tell Discord the bot is working on it (shows "thinking..." to the user)
    # This gives us 15 minutes to process the request instead of the standard 3 seconds.
    await interaction.response.defer(thinking=True)
    
    # Package the interaction object and the text together, and place them in the queue
    await client.message_queue.put((interaction, prompt))

# 5. EXECUTION
# Starts the event loop. No code written beneath this line will ever execute.
client.run(TOKEN)