#!/usr/bin/env python
"""Perform speech enhancement for audio stored in WAV files.

This script performs speech enhancement of audio using a deep-learning based
enhancement model (Lei et al, 2018; Gao et al, 2018; Lei et al, 2017). To perform
enhancement for all WAV files under the directory ``wav_dir/`` and write the
enhanced audio to ``se_wav_dir/`` as WAV files:

    python main_denoising.py --wav-dir wav_dir --output-dir se_wav_dir

For each file with the ``.wav`` extension under ``wav_dir/``, there will now be
a corresponding enhanced version under ``se_wav_dir``.

Alternately, you may specify the files to process via a script file of paths to
WAV files with one path per line:

    /path/to/file1.wav
    /path/to/file2.wav
    /path/to/file3.wav
    ...

This functionality is enabled via the ``-S`` flag, as in the following:

   python main_denoising.py -S some.scp --output-dir se_wav_dir/

As this model is computationally demanding, use of a GPU is recommended, which
may be enabled via the ``--use-gpu`` and ``--gpu-id`` flags. The ``--use-gpu`` flag
indicates whether or not to use a GPU with possible values being ``false`` and ``true``.
The ``--gpu-id`` flag specifies the device id of the GPU to use. For instance:

   python main_denoising.py --use-gpu true --gpu-id 0 -S some.scp --output-dir se_wav_dir/

will perform enhancement using the GPU with device id 0.

If you find that you have insufficient available GPU memory to run the model, try
adjusting the flag ``--truncate-minutes``, which controls the length of audio
chunks processed. Smaller values of ``--truncate-minutes`` will lead to a smaller
memory footprint. For instance:

   python main_denoising.py --truncate-minutes 10 --use-gpu true --gpu-id 0 -S some.scp --output-dir se_wav_dir/

will perform enhancement on the GPU using chunks that are 10 minutes in duration. This should use at
most 8 GB of GPU memory.

References
----------
- Sun, Lei, et al. (2018). "Speaker diarization with enhancing speech for the First DIHARD
 Challenge." Proceedings of INTERSPEECH 2018. 2793-2797.
- Gao, Tian, et al. (2018). "Densely connected progressive learning for LSTM-based speech
  enhancement." Proceedings of ICASSP 2018.
- Sun, Lei, et al. (2017). "Multiple-target deep learning for LSTM-RNN based speech enhancement."
  Proceedings of the Fifth Joint Workshop on Hands-free Speech Communication and Microphone
  Arrays.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
import argparse
import math
import os
import shutil
import sys
import tempfile
import subprocess
import traceback

from functools import wraps
from multiprocessing import Process, Queue

import numpy as np
import scipy.io.wavfile as wav_io
import scipy.io as sio
import librosa

from decode_model import decode_model
import utils

HERE = os.path.abspath(os.path.dirname(__file__))
GLOBAL_MEAN_VAR_MATF = os.path.join(HERE, 'model', 'global_mvn_stats.mat')


SR = 16000 # Expected sample rate (Hz) of input WAV.
BITDEPTH = 16 # Expected bitdepth of input WAV.
WL = 512 # Analysis window length in samples for feature extraction.
WL2 = WL // 2
NFREQS = 257 # Number of positive frequencies in FFT output.


def processify(func):
    '''Decorator to run a function as a process.
    Be sure that every argument and the return value
    is *picklable*.
    The created process is joined, so the code does not
    run in parallel.
    '''

    def process_func(q, *args, **kwargs):
        try:
            ret = func(*args, **kwargs)
        except Exception:
            ex_type, ex_value, tb = sys.exc_info()
            error = ex_type, ex_value, ''.join(traceback.format_tb(tb))
            ret = None
        else:
            error = None

        q.put((ret, error))

    # register original function with different name
    # in sys.modules so it is picklable
    process_func.__name__ = func.__name__ + 'processify_func'
    setattr(sys.modules[__name__], process_func.__name__, process_func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        q = Queue()
        p = Process(target=process_func, args=[q] + list(args), kwargs=kwargs)
        p.start()
        ret, error = q.get()
        p.join()

        if error:
            ex_type, ex_value, tb_str = error
            message = '%s (in subprocess)\n%s' % (ex_value, tb_str)
            raise ex_type(message)

        return ret
    return wrapper

decode_model = processify(decode_model)


def denoise_wav(src_wav_file, dest_wav_file, global_mean, global_var, use_gpu,
                gpu_id, truncate_minutes):
    """Apply speech enhancement to audio in WAV file.

    Parameters
    ----------
    src_wav_file : str
        Path to WAV to denosie.

    dest_wav_file : str
        Output path for denoised WAV.

    global_mean : ndarray, (n_feats,)
        Global mean for LPS features. Used for CMVN.

    global_var : ndarray, (n_feats,)
        Global variances for LPS features. Used for CMVN.

    use_gpu : bool, optional
        If True and GPU is available, perform all processing on GPU.
        (Default: True)

    gpu_id : int, optional
         Id of GPU on which to do computation.
         (Default: 0)

    truncate_minutes: float
        Maximimize size in minutes to process at a time. The enhancement will
        be done on chunks of audio no greather than ``truncate_minutes``
        minutes duration.
    """
    # Read noisy audio WAV file. As scipy.io.wavefile.read is FAR faster than
    # librosa.load, we use the former.
    # rate, wav_data = wav_io.read(src_wav_file)
    y, rate = librosa.core.load(src_wav_file, sr=None)
    wav_data = y * 2**15

    # Apply peak-normalization.
    wav_data = utils.peak_normalization(wav_data)

    # Perform denoising in chunks of size chunk_length samples.
    chunk_length = int(truncate_minutes*rate*60)
    total_chunks = int(
        math.ceil(wav_data.size / chunk_length))
    data_se = [] # Will hold enhanced audio data for each chunk.
    for i in range(1, total_chunks + 1):
        tmp_dir = tempfile.mkdtemp()
        try:
            # Get samples for this chunk.
            bi = (i-1)*chunk_length # Index of first sample of this chunk.
            ei = bi + chunk_length # Index of last sample of this chunk + 1.
            temp = wav_data[bi:ei]
            print('Processing file: %s, segment: %d/%d.' %
                  (src_wav_file, i, total_chunks))

            # Skip denoising if chunk is too short.
            if temp.shape[0] < WL2:
                data_se.append(temp)
                continue

            # Determine paths to the temporary files to be created.
            noisy_normed_lps_fn = os.path.join(
                tmp_dir, 'noisy_normed_lps.htk')
            noisy_normed_lps_scp_fn = os.path.join(
                tmp_dir, 'noisy_normed_lps.scp')
            irm_fn = os.path.join(
                tmp_dir, 'irm.mat')

            # Extract LPS features from waveform.
            noisy_htkdata = utils.wav2logspec(temp, window=np.hamming(WL))

            # Do MVN before decoding.
            normed_noisy = (noisy_htkdata - global_mean) / global_var

            # Write features to HTK binary format making sure to also
            # create a script file.
            utils.write_htk(
                noisy_normed_lps_fn, normed_noisy, samp_period=SR,
                parm_kind=9)
            cntk_len = noisy_htkdata.shape[0] - 1
            with open(noisy_normed_lps_scp_fn, 'w') as f:
                f.write('irm=%s[0,%d]\n' % (noisy_normed_lps_fn, cntk_len))

            # Apply CNTK model to determine ideal ratio mask (IRM), which will
            # be output to the temp directory as irm.mat. In order to avoid a
            # memory leak, must do this in a separate process which we then
            # kill.
            decode_model(noisy_normed_lps_scp_fn, tmp_dir, NFREQS, use_gpu, gpu_id)

            # Read in IRM and directly mask the original LPS features.
            irm = sio.loadmat(irm_fn)['IRM']
            masked_lps = noisy_htkdata + np.log(irm)

            # Reconstruct audio.
            wave_recon = utils.logspec2wav(
                masked_lps, temp, window=np.hamming(WL), n_per_seg=WL,
                noverlap=WL2)
            data_se.append(wave_recon)
        finally:
            shutil.rmtree(tmp_dir)
    data_se = [x.astype(np.int16, copy=False) for x in data_se]
    data_se = np.concatenate(data_se)
    wav_io.write(dest_wav_file, SR, data_se)


def main_denoising(wav_files, output_dir, wav_dir=None, verbose=False, **kwargs):
    """Perform speech enhancement for WAV files in ``wav_dir``.

    Parameters
    ----------
    wav_files : list of str
        Paths to WAV files to enhance.

    output_dir : str
        Path to output directory for enhanced WAV files.

    wav_dir : str, optional
        Path to root input directory. If provided, output_dir will
        mirror the subdirectory structure of wav_dir.

    verbose : bool, optional
        If True, print full stacktrace to STDERR for files with errors.

    kwargs
        Keyword arguments to pass to ``denoise_wav``.
    """

    # Load global MVN statistics.
    global_mean_var = sio.loadmat(GLOBAL_MEAN_VAR_MATF)
    global_mean = global_mean_var['global_mean']
    global_var = global_mean_var['global_var']

    # Perform speech enhancement.
    for src_wav_file in wav_files:
        # Capture input filename and extension.
        if wav_dir:
            filename, ext = os.path.splitext(src_wav_file.replace(wav_dir, '', 1).lstrip('/'))
        else:
            filename, ext = os.path.splitext(os.path.basename(src_wav_file))

        # Perform basic checks of input WAV.
        if not os.path.exists(src_wav_file):
            raise Exception('File "%s" does not exist.' % src_wav_file)

        if not utils.is_wav(src_wav_file):
            try:
                fp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                # run WAV conversion
                cmdline = "ffmpeg -y -i {} -flags bitexact -acodec pcm_s16le {}".format(src_wav_file, fp.name)
                print("run: {}".format(cmdline))
                r = subprocess.run(cmdline.split(), stdout=sys.stdout, stderr=sys.stderr)
                if r.returncode != 0:
                    print("run failed: {}".format(cmdline))
                    return
                src_wav_file = fp.name
            except:
                raise Exception('File "%s" could not be converted to valid WAV. Type: %s' %
                                (src_wav_file, utils.get_file_type(src_wav_file)))

        if utils.get_sr(src_wav_file) != SR:
            utils.warn('Sample rate of file "%s" is %d Hz. Will convert to %d Hz.' %
                       (src_wav_file, utils.get_sr(src_wav_file), SR))

        if utils.get_bitdepth(src_wav_file) != BITDEPTH:
            utils.warn('Bitdepth of file "%s" is %d. Will convert to %d.' %
                       (src_wav_file, utils.get_bitdepth(src_wav_file), BITDEPTH))

        channels = utils.get_num_channels(src_wav_file)
        if channels < 1:
            raise Exception('File "%s" does not have a valid channel layout.' %
                            src_wav_file)

        with tempfile.TemporaryDirectory(prefix="denoise_in_") as tempindir, \
        tempfile.TemporaryDirectory(prefix="denoise_out_") as tempoutdir:

            # split WAV file into individual channel files, convert to 16-bit SR kHz (16 kHz)
            cmdline = "ffmpeg -i {}".format(src_wav_file) + "".join(
                " -map_channel 0.0.{} -acodec pcm_s16le -ar {} {}".format(
                    n, SR, os.path.join(tempindir, "ch{}.wav".format(n))
                    ) for n in range(channels)
            )
            print("run: {}".format(cmdline))
            r = subprocess.run(cmdline.split(), stdout=sys.stdout, stderr=sys.stderr)
            if r.returncode != 0:
                print("run failed: {}".format(cmdline))
                return

            # Perform denoising on individual channel files, write to temporary output dir
            for ch_file in utils.listdir(tempindir):
                try:
                    ch_filename, ch_ext = os.path.splitext(os.path.basename(ch_file))
                    dest_ch_file = "{}_enhanced{}".format(os.path.join(tempoutdir, ch_filename), ch_ext)
                    denoise_wav(ch_file, dest_ch_file, global_mean, global_var, **kwargs)
                    print('Finished processing file "%s".' % ch_file)
                except Exception as e:
                    msg = 'Problem encountered while processing file "%s":' % ch_file
                    utils.error(msg)
                    raise e

            # merge denoised channels into single WAV, write to persistent output dir
            subdir_path = os.path.join(output_dir, os.path.dirname(filename))
            if not os.path.exists(subdir_path):
                os.makedirs(subdir_path)

            dest_wav_file = "{}_enhanced{}".format(os.path.join(output_dir, filename), ext)

            cmdline = "ffmpeg" + "".join(" -i {}".format(ch_file) for ch_file in utils.listdir(tempoutdir)) + \
                " -flags bitexact -filter_complex " + "".join("[{}:a]".format(n) for n in range(channels)) + \
                "join=inputs={0}:channel_layout={0}c[a] -map [a] {1}".format(channels, dest_wav_file)
            print("run: {}".format(cmdline))
            r = subprocess.run(cmdline.split(), stdout=sys.stdout, stderr=sys.stderr)
            if r.returncode != 0:
                print("run failed: {}".format(cmdline))
                return


# TODO: Logging is getting complicated. Consider adding a custom logger...
def main():
    """Main."""
    parser = argparse.ArgumentParser(
        description='Denoise WAV files.', add_help=True)
    parser.add_argument(
        '--wav-dir', nargs=None, type=str, metavar='STR',
        help='directory containing WAV files to denoise '
             '(default: %(default)s')
    parser.add_argument(
        '--output-dir', nargs=None, type=str, metavar='STR',
        help='output directory for denoised WAV files (default: %(default)s)')
    parser.add_argument(
        '-S', dest='scpf', nargs=None, type=str, metavar='STR',
        help='script file of paths to WAV files to denoise (default: %(default)s)')
    parser.add_argument(
        '--use-gpu', nargs=None, default='true', type=str, metavar='STR',
        choices=['true', 'false'],
        help='whether or not to use GPU (default: %(default)s)')
    parser.add_argument(
        '--gpu-id', nargs=None, default=0, type=int, metavar='INT',
        help='device id of GPU to use (default: %(default)s)')
    parser.add_argument(
        '--truncate-minutes', nargs=None, default=10, type=float,
        metavar='FLOAT',
        help='maximum chunk size in minutes (default: %(default)s)')
    parser.add_argument(
        '--verbose', default=False, action='store_true',
        help='print full stacktrace for files with errors')
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()
    if not utils.xor(args.wav_dir, args.scpf):
        parser.error('Exactly one of --wav-dir and -S must be set.')
        sys.exit(1)
    use_gpu = args.use_gpu == 'true'

    # Determine files to denoise.
    if args.scpf is not None:
        wav_files = utils.load_script_file(args.scpf, '.wav')
    else:
        wav_files = utils.listdir_walk(args.wav_dir, ext='.wav')

    # Determine output directory for denoised audio.
    if args.output_dir is None and args.wav_dir is not None:
        utils.warn('Output directory not specified. Defaulting to "%s"' %
                   args.wav_dir)
        args.output_dir = args.wav_dir

    # Perform denoising.
    main_denoising(
        wav_files, args.output_dir, args.wav_dir, args.verbose, use_gpu=use_gpu,
        gpu_id=args.gpu_id, truncate_minutes=args.truncate_minutes)


if __name__ == '__main__':
    main()
