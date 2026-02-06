"""
Keep Alive Module - Giữ bot Discord chạy 24/7 trên free hosting
Dùng cho: Replit, Render, Glitch, Railway

Cách hoạt động:
1. Tạo web server đơn giản (Flask)
2. UptimeRobot ping server mỗi 5 phút
3. Hosting không sleep vì có traffic liên tục
"""

from flask import Flask
from threading import Thread
import os
import logging

# Tắt Flask logging (giảm spam)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask('')

@app.route('/')
def home():
    """Homepage - hiển thị status bot"""
    return """
    <html>
    <head>
        <title>Discord Translation Bot</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }
            .container {
                text-align: center;
                background: rgba(255,255,255,0.1);
                padding: 40px;
                border-radius: 20px;
                backdrop-filter: blur(10px);
            }
            h1 { font-size: 3em; margin: 0; }
            p { font-size: 1.2em; }
            .status { 
                color: #4ade80; 
                font-weight: bold;
                font-size: 1.5em;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Discord Translation Bot</h1>
            <p class="status">✅ ONLINE & RUNNING</p>
            <p>Bot is active and translating messages!</p>
            <p>🌍 Supporting 70+ languages</p>
        </div>
    </body>
    </html>
    """

@app.route('/ping')
def ping():
    """Endpoint cho UptimeRobot"""
    return "pong", 200

@app.route('/health')
def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "discord-translation-bot",
        "uptime": "running"
    }, 200

@app.route('/status')
def status():
    """Status endpoint - JSON response"""
    return {
        "bot": "Discord Translation Bot",
        "status": "online",
        "version": "2.0",
        "languages": "70+",
        "free": True
    }, 200

def run():
    """Chạy Flask web server"""
    # Lấy port từ environment variable (Render, Railway cần này)
    port = int(os.getenv('PORT', 8080))
    
    # Chạy server
    # host='0.0.0.0' để accessible từ bên ngoài
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,  # Tắt debug mode
        use_reloader=False  # Tắt auto-reload
    )

def keep_alive():
    """
    Khởi động web server trong background thread
    
    Gọi hàm này TRƯỚC khi chạy bot:
    
    from keep_alive import keep_alive
    
    if __name__ == "__main__":
        keep_alive()  # Bật web server
        bot.run(TOKEN)  # Chạy bot
    """
    # Tạo thread riêng để chạy web server
    # daemon=True để thread tự động tắt khi main program tắt
    t = Thread(target=run)
    t.daemon = True
    t.start()
    
    print("=" * 60)
    print("✅ Keep-Alive web server started!")
    print(f"🌐 Listening on port {os.getenv('PORT', 8080)}")
    print("🔔 Setup UptimeRobot to ping this URL every 5 minutes")
    print("=" * 60)

# Test standalone
if __name__ == "__main__":
    print("Testing keep_alive module...")
    keep_alive()
    
    # Keep main thread alive
    import time
    while True:
        time.sleep(60)
