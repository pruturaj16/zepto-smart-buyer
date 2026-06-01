# Setup Instructions

## Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/pruturaj16/zepto-buyer.git
   cd zepto-buyer
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure API credentials:**
   ```bash
   cp config.example.py config.py
   ```
   
   Then edit `config.py` and add your actual API keys:
   
   - **Telegram Bot Token**: Get from [BotFather](https://t.me/botfather) on Telegram
   - **Telegram Chat ID**: Your personal chat ID for notifications
   - **Anthropic API Key**: Get from [Anthropic Console](https://console.anthropic.com)
   - **Gemini API Key**: Get from [Google AI Studio](https://aistudio.google.com)

4. **Run the application:**
   ```bash
   python bot.py
   ```

## ⚠️ IMPORTANT: Never Commit config.py

The `config.py` file contains sensitive credentials and is automatically excluded from git (see `.gitignore`). 

**Never:**
- Commit `config.py` to version control
- Share your API keys
- Push credentials to any public repository

**Always:**
- Use `config.example.py` as a template
- Keep API keys in local `config.py` only
- Rotate keys if they're accidentally exposed

## Environment Variables (Alternative)

Instead of `config.py`, you can use environment variables:

```bash
export TELEGRAM_TOKEN="your-token"
export CHAT_ID="your-chat-id"
export ANTHROPIC_API_KEY="your-key"
export GEMINI_API_KEY="your-key"
```

Then modify the code to read from `os.environ` instead of `config.py`.

## Testing

See `TESTING_GUIDE.md` for comprehensive testing instructions.
