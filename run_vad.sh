#!/bin/bash
# This script demonstrates how to run speech enhancement and VAD. For full documentation,
# please consult the docstrings of ``main_denoising.py`` and ``main_get_vad.py``.

SE_WAV_DIR=$1/audio_enhanced  # Output directory for enhanced WAV.

###################################
# Perform VAD using enhanced audio
###################################
SCP_DIR=/media/store/wjkang_store/data/amicorpus/ES_vad.scp
VAD_DIR=/media/store/wjkang_store/data/amicorpus/ES_vad  # Output directory for label files containing VAD output.
HOPLENGTH=30  # Duration in milliseconds of frames for VAD. Also controls step size.
MODE=3        # WebRTC aggressiveness. 0=least agressive and  3=most aggresive.
NJOBS=1       # Number of parallel processes to use.
python main_get_vad.py \
       --verbose \
       --S $SCP_DIR \
       --output_dir $VAD_DIR \
       --mode $MODE \
       --hoplength $HOPLENGTH \
       --n_jobs $NJOBS || exit 1

exit 0
