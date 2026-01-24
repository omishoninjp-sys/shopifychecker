FROM python:3.11-slim

WORKDIR /app

# 複製依賴檔案
COPY requirements.txt .

# 安裝依賴
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY . .

# 設定環境變數
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# 暴露端口（Zeabur 會自動分配）
EXPOSE 8080

# 啟動命令（使用 shell 讀取 PORT 環境變數）
CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 --timeout 120 app:app
