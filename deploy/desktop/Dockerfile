FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DISPLAY=:99
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 xvfb xdotool xclip openbox imagemagick tesseract-ocr fonts-liberation fonts-noto-cjk dbus-x11 \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ore && useradd --uid 10001 --gid ore --create-home ore \
    && mkdir -p /state/profile /state/downloads /etc/opt/chrome/policies/managed \
    && chown -R ore:ore /state
COPY deploy/desktop/control.py deploy/desktop/entrypoint.py /opt/ore-desktop/
USER ore
WORKDIR /state
ENTRYPOINT ["python3", "/opt/ore-desktop/entrypoint.py"]
