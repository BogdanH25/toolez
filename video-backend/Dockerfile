FROM python:3.10

# install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg

WORKDIR /app

COPY . .

RUN pip install -r requirements.txt

CMD ["python", "app.py"]