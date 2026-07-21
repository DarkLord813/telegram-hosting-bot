# Telegram Bot Hosting Platform

A self-hosted platform that lets users deploy their own bots through a Telegram bot interface.

## Features
- Deploy Python & node js bots
- Premium subscriptions with Stars/Coins
- Free tier (3 deployments, 24h each)
- GitHub deployment 
- Environment variable support
- Persistent storage

## Environment Variables
- `BOT_TOKEN`: Your Telegram bot token
- `ADMIN_IDS`: Comma-separated admin user IDs
- `REQUIRED_CHANNEL`: Channel users must join
- `PRICE_MONTHLY_STARS`: Monthly price in Stars
- `PRICE_YEARLY_STARS`: Yearly price in Stars

## Deploy to Render
1. Fork this repo
2. Create new Web Service on Render
3. Connect your repo
4. Add environment variables
5. Deploy!
