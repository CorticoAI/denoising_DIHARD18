# Starting from the official CNTK docker image (based on
# Ubuntu-16.04)
FROM mcr.microsoft.com/cntk/release:2.7-gpu-python3.5-cuda10.0-cudnn7.3

# Update the Ubuntu distribution and install some text editors
RUN apt-get update && apt-get upgrade -y && apt-get install -y nano vim emacs libsndfile1 ffmpeg

# Add conda in the PATH and update it to the last version
ENV PATH=/root/anaconda3/bin:$PATH
RUN conda update -y -n root -c defaults conda

# Install dependencies in a virtual environment
RUN conda create --name dihard18 --clone cntk-py35
RUN bash -c "source activate dihard18 && \
        conda install -c anaconda scipy && \
        pip install --upgrade pip && \
        pip install librosa webrtcvad && \
        pip install wurlitzer joblib"
RUN rm -rf /root/anaconda3/envs/cntk-py35

# Automatically activate the virtual environment when running a docker
# bash session
RUN head -n-14 /root/.bashrc > /tmp/.bashrc && mv /tmp/.bashrc /root/.bashrc
RUN echo "source activate dihard18" >> /root/.bashrc

# Copy the repository inside the docker in /dihard18
WORKDIR /dihard18
COPY . .

# Make the eval scripts executable
RUN chmod +x ./run_eval.sh
RUN chmod +x ./run_denoising.sh
RUN chmod +x ./run_denoising_batch.sh
RUN chmod +x ./run_vad.sh
