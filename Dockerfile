FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Fail the image build if the pinned MCP SDK no longer exposes the server API.
RUN python -c "from mcp.server.fastmcp import FastMCP; print('FastMCP import OK')"

COPY server.py /app/server.py

CMD ["python", "/app/server.py"]
