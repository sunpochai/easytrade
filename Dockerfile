FROM python:3.11-slim
WORKDIR /app
COPY bot.py backtest.py providers.py strategy.py events.json index.html app.js style.css favicon.svg sw.js manifest.webmanifest icon-192.png icon-512.png apple-touch-icon.png ./
ENV PORT=7860
EXPOSE 7860
USER nobody
CMD ["python", "bot.py", "--host", "0.0.0.0", "--source", "api"]
