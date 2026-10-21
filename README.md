# Marl-Kingdom-DS-Audio-Patcher


A high-quality audio replacement tool for **Rhapsody: A Musical Adventure (Nintendo DS)**, also known as **Marl Kingdom / マール王国の人形姫**.

Replaces all 37 music tracks with higher quality audio from the PC and PS2 releases.

### Features

- Replaces 37 BGM/SONG tracks and one sound effect
- Encodes lossless FLAC sources into CRI ADX
- Optional resampling to 37,800 Hz or 32,768 Hz
- Optional trimming and timing adjustments
- Loop point preservation with 2KB alignment
- Supports all ROMs
- Has been together for around four hours, 100% by copying and pasting files through the ChatGPT web interface.

### Usage

Requires Python 3 and FFmpeg.

On Windows, drag your `.nds` file onto `Patch_ROM.cmd`, or run:

```bash
python marl_universal_patcher.py game.nds
```

Choose your preferred sample rate and whether to apply timing adjustments when prompted.

The patched ROM is saved as a separate file. Your original ROM remains untouched.

### Testing

Completed a full playthrough on DSi with the patch running at the original sample rates only.


### Notes

Unofficial fan project. No game ROM is included.
