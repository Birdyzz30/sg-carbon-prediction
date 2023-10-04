FROM python:3.8.12-slim

WORKDIR /home/birdyzz/code/AnnK89/data

COPY /gcp/le-wagon-bootcamp-396204-71c574a50ee8.json /home/birdyzz/code/AnnK89/data/le-wagon-bootcamp-396204-71c574a50ee8.json

# First, pip install dependencies
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# Then only, install taxifare!
COPY interface interface
COPY params.py params.py
COPY ml_logic ml_logic
COPY setup.py setup.py

RUN pip install .

# We already have a make command for that!
COPY Makefile Makefile

# Set the GOOGLE_APPLICATION_CREDENTIALS environment variable
ENV GOOGLE_APPLICATION_CREDENTIALS=/home/birdyzz/code/AnnK89/data/le-wagon-bootcamp-396204-71c574a50ee8.json

# RUN make reset_local_files
CMD uvicorn interface.fast:app --host 0.0.0.0 --port 8000 --reload