FROM python:3.12-slim
RUN pip install --no-cache-dir paho-mqtt Pillow
WORKDIR /app
COPY t20_map_render.py /app/
ENTRYPOINT ["python", "t20_map_render.py"]
