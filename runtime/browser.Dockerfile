# Build context: copy the trusted host Playwright chrome-linux directory beside this file.
FROM nousresearch/hermes-sandbox:desktop
COPY chrome-linux/ /opt/hermes-chromium/
RUN ln -s /opt/hermes-chromium/chrome /usr/bin/chromium
