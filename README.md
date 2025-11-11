# 🤖 Telegram Bot Hosting Service

A powerful Telegram bot that allows users to deploy their Python Telegram bots 24/7 with automatic token integration, health monitoring, and tier-based deployment limits.

## 🌟 Features

### 🤖 Bot Deployment
- **Automatic Token Extraction**: Automatically detects and integrates bot tokens from Python code
- **Secure Code Wrapping**: Creates secure wrappers around user code with health monitoring
- **Requirements Handling**: Automatically installs dependencies from requirements.txt
- **24/7 Deployment**: Keeps your bots running continuously

### 👤 User Management
- **Tier System**: 
  - **Regular Users**: 1 bot deployment
  - **Premium Users**: Up to 20 bot deployments
  - **Admins**: Unlimited deployments (free)
- **Payment System**: Star-based payment system for deployments
- **Balance Management**: Track your star balance and usage

### 🛡️ Security & Monitoring
- **Malicious Code Detection**: Blocks potentially dangerous code patterns
- **Health Checks**: Continuous monitoring of deployed bots
- **Automatic Cleanup**: Removes expired bot deployments
- **Keep-Alive System**: Prevents service sleep on Render

### 📊 Analytics
- **Real-time Stats**: Monitor active bots, users, and deployments
- **Bot Status Tracking**: Check status of all your deployed bots
- **System Health**: Comprehensive service monitoring

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- Render account (for deployment)

### Local Development

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/telegram-bot-hosting.git
cd telegram-bot-hosting
