#!/bin/bash
# This script demonstrates how to run speech enhancement and VAD. For full documentation,
# please consult the docstrings of ``main_denoising.py`` and ``main_get_vad.py``.


###################################
# Run speech enhancement
###################################
WAV_DIR=$1  # Directory of WAV files (16 kHz, 16 bit) to enhance.
SE_WAV_DIR="$1"_enhanced  # Output directory for enhanced WAV.
USE_GPU=true  # Use GPU instead of CPU. To instead use CPU, set to 'false'.
GPU_DEVICE_ID=0  # Use GPU with device id 0. Irrelevant if using CPU.
TRUNCATE_MINUTES=10  # Duration in minutes of chunks for enhancement. If you experience
                     # OOM errors with your GPU, try reducing this.

#echo $WAV_DIR
#echo $SE_WAV_DIR
python3 main_denoising.py \
       --verbose \
       --wav_dir $WAV_DIR --output_dir $SE_WAV_DIR \
       --use_gpu $USE_GPU --gpu_id $GPU_DEVICE_ID \
       --truncate_minutes $TRUNCATE_MINUTES || exit 1

exit 0
