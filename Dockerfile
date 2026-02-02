# 1. 使用 Python 3.12 輕量版
FROM python:3.12-slim

# 2. 安裝 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 3. 進入工作目錄
WORKDIR /app

# 4. 只複製依賴相關檔案（利用 Docker 快取機制）
COPY pyproject.toml uv.lock ./

# 5. 直接將依賴安裝到系統中，不建立虛擬環境 (更快，容器內不需要 venv)
RUN uv pip install --system --no-cache -r pyproject.toml

# 6. 複製程式碼（把程式碼放在後面，這樣改程式碼時不用重新安裝套件）
COPY . .

# 7. 環境變數：強制輸出 Log，方便你在 Zeabur 看到即時訊息
ENV PYTHONUNBUFFERED=1

# 8. 直接執行程式（不再需要透過 uv run，減少啟動開銷）
CMD ["python", "bot.py"]